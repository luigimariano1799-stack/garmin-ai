#!/usr/bin/env python3
"""
sync_garmin.py — pull your own Garmin Connect data (activities + daily
wellness: sleep, HRV, resting HR, body battery, stress, training readiness)
into plain-English markdown notes plus a data.json, so an AI assistant can
read your recovery context.

Built on the open-source python-garminconnect library by cyberjunky:
https://github.com/cyberjunky/python-garminconnect

Security notes:
  - Your password is only ever entered at a hidden terminal prompt
    (getpass). It is never stored, never put in an environment variable,
    never logged, and never printed.
  - After --login, a session token is saved locally in .garmin_tokens/
    (also never printed). It is valid for about a year, so you should not
    need to log in again until it expires or your password changes.
  - This script is read-only: it never writes anything back to your
    Garmin account.

Usage:
  python sync_garmin.py --login                     # one-time interactive login
  python sync_garmin.py --days 3 --dry-run           # test: print, don't write
  python sync_garmin.py --days 3 --sink files        # write garmin/ notes + data.json
  python sync_garmin.py --sink supabase              # POST to your own ingest endpoint
  python sync_garmin.py --export-ci-token            # for GitHub Actions (Path A) only
"""

from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import date, timedelta
from pathlib import Path

try:
    from garminconnect import Garmin
except ImportError:
    print(
        "The 'garminconnect' library isn't installed.\n"
        "Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_DIR = SCRIPT_DIR / ".garmin_tokens"
DEFAULT_OUT = SCRIPT_DIR / "garmin"
CI_TOKEN_FILE = SCRIPT_DIR / "garmin-ci-token.txt"
CI_TOKEN_ENV = "GARMIN_TOKEN_B64"


# --------------------------------------------------------------------------
# Login / token handling
# --------------------------------------------------------------------------

def cmd_login() -> None:
    if not sys.stdin.isatty():
        print(
            "Refusing to log in: this isn't an interactive terminal, so your "
            "password prompt couldn't be hidden safely. Run this command "
            "directly in Terminal/PowerShell instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Garmin login (one-time). Your password will not be shown as you type.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    def prompt_mfa() -> str:
        return input("Enter the 2FA code Garmin just sent you: ").strip()

    try:
        garmin = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
        # Passing tokenstore here makes the library save the session token to
        # TOKEN_DIR itself (with owner-only permissions) right after login.
        garmin.login(str(TOKEN_DIR))
    finally:
        # best-effort: drop the reference to the plaintext password
        password = None  # noqa: F841
        del password

    print(f"Login successful. Session token saved to {TOKEN_DIR} (not printed, not committed to git).")
    print("You shouldn't need to log in again for about a year.")


def get_client() -> Garmin:
    """Resume a session from a saved token, or a CI-provided token bundle."""
    if not TOKEN_DIR.exists() and CI_TOKEN_ENV in os.environ:
        _restore_ci_token(os.environ[CI_TOKEN_ENV])

    if not TOKEN_DIR.exists():
        print(
            "No saved Garmin login found. Run this first:\n"
            "  python sync_garmin.py --login",
            file=sys.stderr,
        )
        sys.exit(1)

    garmin = Garmin()
    garmin.login(str(TOKEN_DIR))
    return garmin


def cmd_export_ci_token() -> None:
    """Bundle .garmin_tokens/ into a base64 blob for a GitHub Actions secret."""
    if not TOKEN_DIR.exists():
        print("No token found. Run --login first.", file=sys.stderr)
        sys.exit(1)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(TOKEN_DIR, arcname=".garmin_tokens")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    CI_TOKEN_FILE.write_text(encoded)
    try:
        os.chmod(CI_TOKEN_FILE, 0o600)
    except OSError:
        pass

    print(f"Wrote {CI_TOKEN_FILE}.")
    print("Paste its full contents into your GitHub repo's GARMIN_TOKEN_B64 secret,")
    print("then delete this file. Never commit it or paste it into a chat.")


def _restore_ci_token(encoded: str) -> None:
    data = base64.b64decode(encoded)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(SCRIPT_DIR)


# --------------------------------------------------------------------------
# Data pulling helpers
# --------------------------------------------------------------------------

def safe_call(fn, *args, label: str = "", **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - garminconnect raises many types
        print(f"  (skipped {label}: {exc})", file=sys.stderr)
        return None


def first_of(d: dict | None, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def fmt_duration(seconds) -> str | None:
    if not seconds:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_km(meters) -> str | None:
    if not meters:
        return None
    return f"{meters / 1000:.2f} km"


def secs_to_min(value):
    return round(value / 60, 1) if value is not None else None


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text or "activity").strip("-").lower()
    return text or "activity"


def collect_activities(garmin: Garmin, start: date, end: date) -> list[dict]:
    raw = safe_call(
        garmin.get_activities_by_date,
        start.isoformat(),
        end.isoformat(),
        label="activities",
    )
    return raw or []


def collect_wellness(garmin: Garmin, day: date) -> dict:
    d = day.isoformat()
    summary = safe_call(garmin.get_user_summary, d, label=f"summary {d}") or {}
    sleep = safe_call(garmin.get_sleep_data, d, label=f"sleep {d}") or {}
    hrv = safe_call(garmin.get_hrv_data, d, label=f"hrv {d}") or {}
    readiness = safe_call(garmin.get_training_readiness, d, label=f"training readiness {d}")
    battery = safe_call(garmin.get_body_battery, d, d, label=f"body battery {d}")

    sleep_summary = first_of(sleep, "dailySleepDTO", default={}) if isinstance(sleep, dict) else {}
    hrv_summary = first_of(hrv, "hrvSummary", default={}) if isinstance(hrv, dict) else {}

    readiness_score = None
    if isinstance(readiness, list) and readiness:
        readiness_score = first_of(readiness[0], "score")
    elif isinstance(readiness, dict):
        readiness_score = first_of(readiness, "score")

    battery_low = battery_high = None
    if isinstance(battery, list) and battery:
        entry = battery[0]
        battery_low = first_of(entry, "bodyBatteryLowestValue", "charged")
        battery_high = first_of(entry, "bodyBatteryHighestValue", "drained")
    elif isinstance(battery, dict):
        battery_low = first_of(battery, "bodyBatteryLowestValue")
        battery_high = first_of(battery, "bodyBatteryHighestValue")

    return {
        "date": d,
        "resting_hr": first_of(summary, "restingHeartRate"),
        "hrv_ms": first_of(hrv_summary, "lastNightAvg", "weeklyAvg", "lastNight5MinHigh"),
        "sleep_hours": round(first_of(sleep_summary, "sleepTimeSeconds", default=0) / 3600, 1)
        if first_of(sleep_summary, "sleepTimeSeconds")
        else None,
        "sleep_score": first_of(sleep_summary, "sleepScores", default={}).get("overall", {}).get("value")
        if isinstance(first_of(sleep_summary, "sleepScores"), dict)
        else None,
        "body_battery_low": battery_low,
        "body_battery_high": battery_high,
        "stress_avg": first_of(summary, "averageStressLevel"),
        "steps": first_of(summary, "totalSteps"),
        "training_readiness": readiness_score,
        # Extra detail, all pulled from calls already made above.
        "max_hr": as_int(first_of(summary, "maxHeartRate")),
        "calories_total": as_int(first_of(summary, "totalKilocalories")),
        "calories_active": as_int(first_of(summary, "activeKilocalories")),
        "floors_climbed": as_int(first_of(summary, "floorsAscended")),
        "intensity_minutes_moderate": as_int(first_of(summary, "moderateIntensityMinutes")),
        "intensity_minutes_vigorous": as_int(first_of(summary, "vigorousIntensityMinutes")),
        "spo2_avg": first_of(summary, "averageSpo2"),
        "respiration_avg": first_of(summary, "avgWakingRespirationValue"),
        "stress_high_min": secs_to_min(first_of(summary, "highStressDuration")),
        "stress_low_min": secs_to_min(first_of(summary, "lowStressDuration")),
        "body_battery_charged": as_int(first_of(summary, "bodyBatteryChargedValue")),
        "body_battery_drained": as_int(first_of(summary, "bodyBatteryDrainedValue")),
        "sleep_deep_min": secs_to_min(first_of(sleep_summary, "deepSleepSeconds")),
        "sleep_light_min": secs_to_min(first_of(sleep_summary, "lightSleepSeconds")),
        "sleep_rem_min": secs_to_min(first_of(sleep_summary, "remSleepSeconds")),
        "sleep_awake_min": secs_to_min(first_of(sleep_summary, "awakeSleepSeconds")),
        "sleep_avg_hr": as_int(first_of(sleep_summary, "avgHeartRate")),
    }


# --------------------------------------------------------------------------
# Rendering (plain-English markdown)
# --------------------------------------------------------------------------

def render_daily_md(w: dict) -> str:
    lines = [f"# Garmin wellness {w['date']}", ""]
    if w.get("resting_hr") is not None:
        lines.append(f"- Resting HR: {w['resting_hr']} bpm")
    if w.get("hrv_ms") is not None:
        lines.append(f"- HRV (overnight): {w['hrv_ms']} ms")
    if w.get("sleep_hours") is not None:
        score = f" (score {w['sleep_score']})" if w.get("sleep_score") is not None else ""
        lines.append(f"- Sleep: {w['sleep_hours']} h{score}")
    if w.get("body_battery_low") is not None or w.get("body_battery_high") is not None:
        lines.append(
            f"- Body battery: {w.get('body_battery_low', '?')} -> {w.get('body_battery_high', '?')}"
        )
    if w.get("stress_avg") is not None:
        lines.append(f"- Stress (avg): {w['stress_avg']}")
    if w.get("steps") is not None:
        lines.append(f"- Steps: {w['steps']}")
    if w.get("training_readiness") is not None:
        lines.append(f"- Training readiness: {w['training_readiness']}")
    if w.get("sleep_deep_min") is not None:
        lines.append(
            f"- Sleep stages: deep {w.get('sleep_deep_min')}m, light {w.get('sleep_light_min')}m, "
            f"REM {w.get('sleep_rem_min')}m, awake {w.get('sleep_awake_min')}m"
        )
    if w.get("calories_total") is not None:
        lines.append(f"- Calories: {round(w['calories_total'])} total ({round(w.get('calories_active') or 0)} active)")
    if w.get("floors_climbed") is not None:
        lines.append(f"- Floors climbed: {w['floors_climbed']}")
    if w.get("intensity_minutes_moderate") is not None:
        lines.append(
            f"- Intensity minutes: {w.get('intensity_minutes_moderate', 0)} moderate, "
            f"{w.get('intensity_minutes_vigorous', 0)} vigorous"
        )
    if w.get("spo2_avg") is not None:
        lines.append(f"- SpO2 (avg): {w['spo2_avg']}%")
    if w.get("respiration_avg") is not None:
        lines.append(f"- Respiration (avg): {w['respiration_avg']} brpm")
    if len(lines) == 2:
        lines.append("- No wellness data recorded for this day (watch not worn?)")
    return "\n".join(lines) + "\n"


def render_activity_md(a: dict) -> str:
    name = a.get("activityName") or "Activity"
    activity_type = first_of(a.get("activityType"), "typeKey", default="activity")
    start = a.get("startTimeLocal", "")
    lines = [f"# {name}", "", f"- Type: {activity_type}", f"- Start: {start}"]

    dist = fmt_km(a.get("distance"))
    if dist:
        lines.append(f"- Distance: {dist}")
    dur = fmt_duration(a.get("duration"))
    if dur:
        lines.append(f"- Duration: {dur}")
    if a.get("averageHR"):
        lines.append(f"- Avg HR: {a['averageHR']} bpm")
    if a.get("maxHR"):
        lines.append(f"- Max HR: {a['maxHR']} bpm")
    if a.get("calories"):
        lines.append(f"- Calories: {a['calories']}")
    if a.get("elevationGain"):
        lines.append(f"- Elevation gain: {a['elevationGain']} m")
    if a.get("averageSpeed") and dist and dur:
        lines.append(f"- Avg pace/speed: {a['averageSpeed']:.2f} m/s")
    if a.get("locationName"):
        lines.append(f"- Location: {a['locationName']}")
    if a.get("trainingEffectLabel"):
        lines.append(f"- Training effect: {a['trainingEffectLabel']}")
    if a.get("aerobicTrainingEffect") is not None:
        lines.append(
            f"- Aerobic/anaerobic effect: {a.get('aerobicTrainingEffect')} / {a.get('anaerobicTrainingEffect')}"
        )
    if a.get("activityTrainingLoad") is not None:
        lines.append(f"- Training load: {a['activityTrainingLoad']}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

def write_files_sink(activities: list[dict], wellness: list[dict], out_dir: Path) -> None:
    daily_dir = out_dir / "daily"
    activities_dir = out_dir / "activities"
    daily_dir.mkdir(parents=True, exist_ok=True)
    activities_dir.mkdir(parents=True, exist_ok=True)

    data_path = out_dir / "data.json"
    store = {"activities": {}, "wellness": {}}
    if data_path.exists():
        try:
            store = json.loads(data_path.read_text())
        except json.JSONDecodeError:
            pass
    store.setdefault("activities", {})
    store.setdefault("wellness", {})

    for w in wellness:
        (daily_dir / f"{w['date']}.md").write_text(render_daily_md(w))
        store["wellness"][w["date"]] = w

    for a in activities:
        activity_id = str(a.get("activityId", ""))
        start = (a.get("startTimeLocal") or "")[:10]
        slug = slugify(a.get("activityName", ""))
        filename = f"{start}-{slug}.md" if start else f"{activity_id}-{slug}.md"
        (activities_dir / filename).write_text(render_activity_md(a))
        if activity_id:
            store["activities"][activity_id] = a

    data_path.write_text(json.dumps(store, indent=2, default=str))
    print(f"Wrote {len(wellness)} daily note(s) and {len(activities)} activity note(s) to {out_dir}/")
    print(f"Updated {data_path}")


def as_int(value):
    return int(round(value)) if value is not None else None


def map_activity_row(a: dict) -> dict:
    return {
        "activity_id": str(a.get("activityId", "")),
        "name": a.get("activityName"),
        "activity_type": first_of(a.get("activityType"), "typeKey"),
        "start_time": a.get("startTimeGMT") or a.get("startTimeLocal"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("duration"),
        "avg_hr": as_int(a.get("averageHR")),
        "max_hr": as_int(a.get("maxHR")),
        "calories": as_int(a.get("calories")),
        "elevation_gain_m": a.get("elevationGain"),
        "training_effect_aerobic": a.get("aerobicTrainingEffect"),
        "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
        "training_effect_label": a.get("trainingEffectLabel"),
        "training_load": a.get("activityTrainingLoad"),
        "avg_speed_mps": a.get("averageSpeed"),
        "max_speed_mps": a.get("maxSpeed"),
        "elevation_loss_m": a.get("elevationLoss"),
        "max_elevation_m": a.get("maxElevation"),
        "min_elevation_m": a.get("minElevation"),
        "location_name": a.get("locationName"),
        "hr_zone1_min": secs_to_min(a.get("hrTimeInZone_1")),
        "hr_zone2_min": secs_to_min(a.get("hrTimeInZone_2")),
        "hr_zone3_min": secs_to_min(a.get("hrTimeInZone_3")),
        "hr_zone4_min": secs_to_min(a.get("hrTimeInZone_4")),
        "hr_zone5_min": secs_to_min(a.get("hrTimeInZone_5")),
    }


def _supabase_upsert(base_url: str, service_key: str, table: str, on_conflict: str, rows: list[dict]) -> None:
    import urllib.request
    import urllib.error

    if not rows:
        return

    url = f"{base_url.rstrip('/')}/rest/v1/{table}?on_conflict={on_conflict}"
    payload = json.dumps(rows, default=str).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("apikey", service_key)
    req.add_header("Authorization", f"Bearer {service_key}")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Upserted {len(rows)} row(s) into {table}: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"Failed to upsert into {table}: HTTP {exc.code} — {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Failed to reach {url}: {exc}", file=sys.stderr)
        sys.exit(1)


def write_supabase_sink(activities: list[dict], wellness: list[dict]) -> None:
    base_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not base_url or not service_key:
        print(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must both be set. "
            "Use --sink files instead if you don't have Supabase set up.",
            file=sys.stderr,
        )
        sys.exit(1)

    _supabase_upsert(base_url, service_key, "garmin_wellness", "date", wellness)

    activity_rows = [map_activity_row(a) for a in activities if a.get("activityId")]
    _supabase_upsert(base_url, service_key, "garmin_activities", "activity_id", activity_rows)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Pull your Garmin data for your AI coach.")
    parser.add_argument("--login", action="store_true", help="One-time interactive Garmin login.")
    parser.add_argument(
        "--export-ci-token",
        action="store_true",
        help="Bundle your saved login as base64 for a GitHub Actions secret (Path A only).",
    )
    parser.add_argument("--days", type=int, default=3, help="How many days back to pull (default 3).")
    parser.add_argument("--dry-run", action="store_true", help="Print results, write nothing.")
    parser.add_argument(
        "--sink",
        choices=["files", "supabase"],
        default="files",
        help="Where to send the data (default: files).",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output folder for --sink files.")
    parser.add_argument(
        "--pace",
        type=float,
        default=0.3,
        help="Seconds to wait between each day's requests, to be gentle on Garmin's servers (default 0.3).",
    )
    args = parser.parse_args()

    if args.login:
        cmd_login()
        return

    if args.export_ci_token:
        cmd_export_ci_token()
        return

    garmin = get_client()

    end = date.today()
    start = end - timedelta(days=args.days - 1)

    print(f"Pulling {args.days} day(s) of data: {start.isoformat()} to {end.isoformat()}...")

    activities = collect_activities(garmin, start, end)

    wellness = []
    for i in range(args.days):
        day = start + timedelta(days=i)
        wellness.append(collect_wellness(garmin, day))
        if args.days > 10 and (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{args.days} days pulled")
        if args.pace:
            time.sleep(args.pace)

    if args.dry_run:
        print(f"\nFound {len(activities)} activit(y/ies):")
        for a in activities:
            print(f"  - {a.get('startTimeLocal', '?')}: {a.get('activityName', 'Activity')}")
        print(f"\nWellness for {len(wellness)} day(s):")
        for w in wellness:
            print("\n" + render_daily_md(w))
        print("\n(Dry run: nothing was written.)")
        return

    if args.sink == "files":
        write_files_sink(activities, wellness, args.out)
    else:
        write_supabase_sink(activities, wellness)


if __name__ == "__main__":
    main()
