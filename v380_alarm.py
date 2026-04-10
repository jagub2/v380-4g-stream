#!/usr/bin/env python3
"""
V380 Alarm Recorder - Download alarm-triggered clips from V380 cloud

Polls the cloud alarm API for motion/AI detection events and saves
decrypted MP4 clips locally.  Runs standalone or alongside v380_stream.py.

Usage:
    python v380_alarm.py --token TOKEN --user-id UID --device-id DID \\
                         --device-password PWD
    python v380_alarm.py --config config.json
    python v380_alarm.py --token TOKEN --devices
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from v380_4g import __version__
from v380_4g.alarm_recorder import (
    AlarmRecorder,
    ALARM_TYPE_MOTION,
    ALARM_TYPE_HUMAN,
    ALARM_TYPE_PIR,
)


def _load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _write_example_config(path: str):
    example = {
        "access_token":       "from_wireshark_pc_app_user_pc_login_response",
        "user_id":            0,
        "device_id":          0,
        "device_password":    "plaintext_camera_password",
        "output_dir":         "recordings",
        "poll_interval":      15,
        "alarm_types":        [0],
        "max_clip_age_hours": 24,
    }
    with open(path, "w") as f:
        json.dump(example, f, indent=2)
        f.write("\n")
    print(f"[+] Wrote example config: {path}")
    print("    Fill in access_token, user_id, device_id, device_password")
    print("    Then run:  python v380_alarm.py --config config.json --devices")
    print("    to verify the token and discover device details.")


def main():
    parser = argparse.ArgumentParser(
        description="V380 Alarm Recorder — download motion/AI alarm clips",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Obtaining credentials (via Wireshark on the Windows V380 Pro app):
  1. Wireshark filter: tcp.port == 8002
  2. Open V380 Pro Windows app and log in
  3. Find POST /user/pc-login response → copy access_token + user_id
  4. Find POST /device/list response   → copy device_id, device_password
     (device_password is base64-encoded — decode it first), rand_key

Using a Windows token does NOT log out the mobile app.

Examples:
  %(prog)s --token TOKEN --user-id 12345 --device-id 67890 --device-password PWD
  %(prog)s --config config.json
  %(prog)s --config config.json --motion-only
  %(prog)s --token TOKEN --devices
  %(prog)s --init
"""
    )

    parser.add_argument("--version", "-V", action="version",
                        version=f"%(prog)s {__version__}")

    # Credentials
    creds = parser.add_argument_group("credentials (or use --config)")
    creds.add_argument("--token", metavar="TOKEN",
                       help="Cloud access_token from /user/pc-login response")
    creds.add_argument("--user-id", type=int, metavar="UID",
                       help="user_id from login response")
    creds.add_argument("--device-id", type=int, metavar="DID",
                       help="device_id from /device/list response")
    creds.add_argument("--device-password", metavar="PWD",
                       help="Camera password (plaintext)")

    # Config file
    parser.add_argument("--config", "-c", metavar="FILE",
                        help="JSON config file (alternative to individual flags)")
    parser.add_argument("--init", action="store_true",
                        help="Write example config.json and exit")

    # Actions
    parser.add_argument("--devices", action="store_true",
                        help="List devices for this token and exit")

    # Recording options
    rec = parser.add_argument_group("recording options")
    rec.add_argument("--output-dir", "-o", default="recordings", metavar="DIR",
                     help="Output directory (default: recordings)")
    rec.add_argument("--poll-interval", type=int, default=15, metavar="SECS",
                     help="Seconds between cloud polls (default: 15)")
    rec.add_argument("--poll-overlap", type=int, default=4, metavar="N",
                     help="Re-query this many poll intervals into the past each cycle "
                          "to catch alarms with lagged camera clocks or delayed server "
                          "indexing (default: 4 — overlap = 4 × poll_interval seconds)")
    rec.add_argument("--alarm-types", metavar="TYPES", default="0",
                     help="Comma-separated alarm types: 0=all 2=motion 3=PIR "
                          "8=human (default: 0)")
    rec.add_argument("--motion-only", action="store_true",
                     help="Shorthand for --alarm-types 2,8,3")
    rec.add_argument("--max-age", type=int, default=24, metavar="HOURS",
                     help="Ignore alarms older than this many hours (default: 24)")

    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --init
    if args.init:
        out = "config.json"
        if os.path.exists(out):
            print(f"[!] {out} already exists — not overwriting", file=sys.stderr)
            return 1
        _write_example_config(out)
        return 0

    # Resolve config
    config: dict = {}
    if args.config:
        config = _load_config(args.config)

    # CLI args override config file
    token      = args.token          or config.get("access_token", "")
    user_id    = args.user_id        or config.get("user_id", 0)
    device_id  = args.device_id      or config.get("device_id", 0)
    device_pwd = args.device_password or config.get("device_password", "")
    output_dir = config.get("output_dir", args.output_dir)
    poll       = config.get("poll_interval", args.poll_interval)
    max_age    = config.get("max_clip_age_hours", args.max_age)

    if not token:
        print("[!] --token (or access_token in config) required", file=sys.stderr)
        return 1

    # --devices
    if args.devices:
        print("[*] Fetching device list...")
        AlarmRecorder.print_devices(token)
        return 0

    # Validate required fields for recording
    for name, val in [("user_id", user_id), ("device_id", device_id),
                      ("device_password", device_pwd)]:
        if not val:
            print(f"[!] --{name.replace('_','-')} required", file=sys.stderr)
            return 1

    # Alarm types
    if args.motion_only:
        alarm_types = [ALARM_TYPE_MOTION, ALARM_TYPE_HUMAN, ALARM_TYPE_PIR]
    else:
        raw = config.get("alarm_types") or args.alarm_types
        if isinstance(raw, list):
            alarm_types = raw
        else:
            alarm_types = [int(x.strip()) for x in str(raw).split(",")]

    recorder = AlarmRecorder(
        access_token=token,
        user_id=user_id,
        device_id=device_id,
        device_password=device_pwd,
        output_dir=output_dir,
        poll_interval=poll,
        alarm_types=alarm_types,
        max_clip_age_hours=max_age,
        poll_overlap_intervals=config.get("poll_overlap_intervals", args.poll_overlap),
        debug=args.debug,
    )

    print(f"[*] Alarm recorder started")
    print(f"    device_id:     {device_id}")
    print(f"    alarm types:   {alarm_types}")
    print(f"    poll interval: {poll}s")
    print(f"    output:        {output_dir}/")
    print(f"[*] Press Ctrl-C to stop")

    t = recorder.start()
    try:
        while t.is_alive():
            t.join(timeout=1)
    except KeyboardInterrupt:
        print()
        print("[*] Stopping...")
        recorder.stop()

    return 0


if __name__ == "__main__":
    exit(main())
