#!/usr/bin/env python3
"""
V380 4G Stream - Live Video Recording

Download and decrypt live video streams from V380 4G cameras.

Usage:
    python v380_stream.py -d DEVICE_ID -p PASSWORD
    python v380_stream.py -d DEVICE_ID -p PASSWORD --segment-mins 2 --total-mins 30
    python v380_stream.py -d DEVICE_ID -p PASSWORD --alarm-trigger \
        --alarm-token TOKEN --alarm-user-id UID
"""

import argparse
import json
import sys
import os

from v380_4g import __version__
from v380_4g.client import V380Client, DEFAULT_API_SERVER, discover_stream_server
from v380_4g.stream import StreamRecorder


def main():
    parser = argparse.ArgumentParser(
        description="V380 4G Stream - Download live video from V380 4G cameras",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -d 12345678 -p 'password'
  %(prog)s -d 12345678 -p 'password' --segment-mins 2 --total-mins 30
  %(prog)s -d 12345678 -p 'password' --rtsp
  %(prog)s -d 12345678 -p 'password' --auto-server

  # Alarm-triggered mode (records 5 min of live video per alarm):
  %(prog)s -d 12345678 -p 'password' --alarm-trigger \\
      --alarm-token TOKEN --alarm-user-id 93643276
  %(prog)s -d 12345678 -p 'password' --alarm-trigger \\
      --alarm-config alarm_config.json --record-mins 5

Output:
  recordings/v380_YYYYMMDD_HHMMSS.mp4         (normal mode)
  recordings/alarm_live_YYYYMMDD_HHMMSS.mp4   (alarm-trigger mode)

Press Ctrl-C to stop.
"""
    )

    parser.add_argument("--version", "-V", action="version",
                       version=f"%(prog)s {__version__}")

    required = parser.add_argument_group('required arguments')
    required.add_argument("--device-id", "-d", required=True, type=int,
                         help="Camera device ID (from QR code)")
    required.add_argument("--password", "-p", required=True,
                         help="Device password (NOT your account password)")

    parser.add_argument("--duration", "-t", type=int, default=60, metavar="SECS",
                       help="Segment duration in seconds (default: 60)")
    parser.add_argument("--segment-mins", type=float, metavar="MINS",
                       help="Segment duration in minutes (overrides --duration)")
    parser.add_argument("--total-mins", type=float, metavar="MINS",
                       help="Total recording time in minutes, then stop automatically")
    parser.add_argument("--output-dir", "-o", default="recordings", metavar="DIR",
                       help="Output directory (default: recordings)")
    parser.add_argument("--server", metavar="IP",
                       help="Override API server IP")
    parser.add_argument("--auto-server", action="store_true",
                       help="Auto-discover closest streaming server via HTTP dispatch")
    parser.add_argument("--handle", type=int, metavar="NUM",
                       help="Override encryption handle")
    parser.add_argument("--hd", action="store_true",
                       help="Request HD (main) stream instead of SD (sub) stream. "
                            "Uses accountId=12 and connectType=1 in the login request. "
                            "Whether HD is actually delivered depends on camera firmware "
                            "and signal quality.")
    parser.add_argument("--no-audio", action="store_true",
                       help="Disable audio recording")
    parser.add_argument("--no-mp4", action="store_true",
                       help="Don't convert to MP4 (keep raw H.265/AAC)")
    parser.add_argument("--keep-raw", action="store_true",
                       help="Keep raw H.265/AAC files after MP4 conversion")
    parser.add_argument("--rtsp", action="store_true",
                       help="Start RTSP server for live viewing")
    parser.add_argument("--rtsp-port", type=int, default=8554, metavar="PORT",
                       help="RTSP server port (default: 8554)")
    parser.add_argument("--debug", action="store_true",
                       help="Enable debug output")

    alarm_grp = parser.add_argument_group(
        "alarm-triggered mode",
        "Record N minutes of live video automatically after each alarm event."
    )
    alarm_grp.add_argument("--alarm-trigger", action="store_true",
                           help="Enable alarm-triggered recording mode")
    alarm_grp.add_argument("--alarm-token", metavar="TOKEN",
                           help="Cloud access_token (from /user/pc-login via Wireshark)")
    alarm_grp.add_argument("--alarm-user-id", type=int, metavar="UID",
                           help="Cloud user_id (from /user/pc-login response)")
    alarm_grp.add_argument("--alarm-config", metavar="FILE",
                           help="JSON config file with alarm credentials")
    alarm_grp.add_argument("--record-mins", type=float, default=5.0, metavar="MINS",
                           help="Minutes of live video to record per alarm (default: 5)")
    alarm_grp.add_argument("--poll-overlap", type=int, default=4, metavar="N",
                           help="Re-query this many poll intervals into the past each "
                                "cycle to catch lagged alarms (default: 4)")

    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()

    # Resolve server
    server = None
    if args.server:
        server = args.server
    elif args.auto_server:
        print(f"[*] Discovering closest streaming server for device {args.device_id}...")
        server = discover_stream_server(args.device_id)
        if server:
            print(f"[+] Using server: {server}")
        else:
            print(f"[!] Discovery failed — falling back to {DEFAULT_API_SERVER}")

    client_kwargs = {"debug": args.debug, "hd": args.hd}
    if server:
        client_kwargs["server"] = server

    client = V380Client(args.device_id, args.password, **client_kwargs)

    try:
        if not client.register():
            print("[!] Registration failed, continuing anyway...")
        if not client.connect():
            return 1
        if not client.login():
            return 1
        if args.handle:
            client.set_handle(args.handle)

        if args.alarm_trigger:
            return _run_alarm_trigger(args, client)

        # Normal recording
        rtsp_server = None
        if args.rtsp:
            try:
                from v380_4g.rtsp_server import RTSPServer
                rtsp_server = RTSPServer(args.rtsp_port)
                rtsp_server.start()
            except ImportError:
                print("[!] rtsp_server module not found - RTSP disabled")
            except Exception as e:
                print(f"[!] Failed to start RTSP server: {e}")

        segment_secs = int(args.segment_mins * 60) if args.segment_mins else args.duration
        total_secs   = int(args.total_mins * 60)   if args.total_mins   else None

        if args.segment_mins:
            print(f"[*] Segment: {args.segment_mins:.1f} min ({segment_secs}s)")
        if args.total_mins:
            print(f"[*] Total:   {args.total_mins:.1f} min ({total_secs}s)")

        recorder = StreamRecorder(client, enable_audio=not args.no_audio)
        video_file = recorder.record(
            duration=segment_secs,
            output_dir=args.output_dir,
            rtsp_server=rtsp_server,
            total_duration=total_secs,
        )

        if rtsp_server:
            rtsp_server.stop()

        if not args.no_mp4 and video_file:
            try:
                from v380_4g.mp4_muxer import MP4Muxer
                audio_file = video_file.replace('.h265', '.aac')
                mp4_file   = video_file.replace('.h265', '.mp4')
                audio_path = (audio_file
                              if os.path.exists(audio_file)
                              and os.path.getsize(audio_file) > 0
                              else None)
                print(f"\n[*] Converting to MP4...")
                muxer = MP4Muxer(video_file, audio_path)
                if muxer.mux(mp4_file):
                    print(f"[+] MP4 saved: {mp4_file}")
                    if not args.keep_raw:
                        if os.path.exists(video_file):
                            os.remove(video_file)
                        if audio_path and os.path.exists(audio_path):
                            os.remove(audio_path)
            except ImportError:
                print("[!] mp4_muxer module not found - cannot convert to MP4")
            except Exception as e:
                print(f"[!] MP4 conversion failed: {e}")

    finally:
        client.disconnect()

    return 0


def _run_alarm_trigger(args, client) -> int:
    from v380_4g.alarm_recorder import AlarmRecorder
    from v380_4g.triggered_recorder import AlarmTriggeredRecorder
    import logging

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    alarm_cfg: dict = {}
    if args.alarm_config:
        with open(args.alarm_config) as f:
            alarm_cfg = json.load(f)

    token = args.alarm_token    or alarm_cfg.get("access_token", "")
    uid   = args.alarm_user_id  or alarm_cfg.get("user_id", 0)

    if not token:
        print("[!] --alarm-token (or access_token in --alarm-config) required",
              file=sys.stderr)
        print("    Capture via Wireshark: tcp.port == 8002, POST /user/pc-login",
              file=sys.stderr)
        return 1
    if not uid:
        print("[!] --alarm-user-id (or user_id in --alarm-config) required",
              file=sys.stderr)
        return 1

    record_secs  = int(args.record_mins * 60)
    segment_secs = min(60, record_secs)

    print(f"[*] Alarm-trigger mode")
    print(f"    device_id:   {args.device_id}")
    print(f"    record time: {record_secs}s ({args.record_mins:.1f} min per alarm)")
    print(f"    output:      {args.output_dir}/")

    alarm_rec = AlarmRecorder(
        access_token=token,
        user_id=uid,
        device_id=args.device_id,
        device_password=args.password,
        output_dir=args.output_dir,
        poll_interval=alarm_cfg.get("poll_interval", 15),
        alarm_types=alarm_cfg.get("alarm_types", [0]),
        max_clip_age_hours=alarm_cfg.get("max_clip_age_hours", 24),
        poll_overlap_intervals=alarm_cfg.get("poll_overlap_intervals", args.poll_overlap),
        debug=args.debug,
    )

    triggered = AlarmTriggeredRecorder(
        client=client,
        alarm_recorder=alarm_rec,
        record_secs=record_secs,
        segment_secs=segment_secs,
        output_dir=args.output_dir,
        no_audio=args.no_audio,
        # An alarm is "fresh" if it arrived within 3 poll cycles.
        # Everything older is a historical event and should not start recording.
        max_alarm_age_secs=alarm_cfg.get("poll_interval", 15) * 3,
    )

    triggered.run()
    return 0


if __name__ == "__main__":
    exit(main())
