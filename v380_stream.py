#!/usr/bin/env python3
"""
V380 4G Stream - Live Video Recording

Download and decrypt live video streams from V380 4G cameras.

Usage:
    python v380_stream.py -d DEVICE_ID -p PASSWORD
    python v380_stream.py -d DEVICE_ID -p PASSWORD --duration 30 --rtsp
"""

import argparse
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
  %(prog)s -d 12345678 -p 'password' --duration 30
  %(prog)s -d 12345678 -p 'password' --rtsp
  %(prog)s -d 12345678 -p 'password' --no-audio --no-mp4

Output:
  recordings/v380_YYYYMMDD_HHMMSS.mp4 (default)

Press Ctrl-C to stop recording early.
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
                       help="Segment duration in seconds (default: 60). "
                            "Use --segment-mins for minutes.")
    parser.add_argument("--segment-mins", type=float, metavar="MINS",
                       help="Segment duration in minutes (overrides --duration)")
    parser.add_argument("--total-mins", type=float, metavar="MINS",
                       help="Total recording time in minutes, then stop automatically"
                            " (default: record indefinitely until Ctrl-C)")
    parser.add_argument("--output-dir", "-o", default="recordings", metavar="DIR",
                       help="Output directory (default: recordings)")
    parser.add_argument("--server", metavar="IP",
                       help="Override API server IP")
    parser.add_argument("--auto-server", action="store_true",
                       help="Auto-discover closest server via MQTT dispatch "
                            "(lower latency than the default hardcoded IP)")
    parser.add_argument("--handle", type=int, metavar="NUM",
                       help="Override encryption handle")
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
                       help="Enable debug output and raw stream saving")

    # Show help if no arguments provided
    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()

    # Resolve server IP
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

    # Create client
    client_kwargs = {"debug": args.debug}
    if server:
        client_kwargs["server"] = server

    client = V380Client(args.device_id, args.password, **client_kwargs)

    try:
        # Register with cloud routing
        if not client.register():
            print("[!] Registration failed, continuing anyway...")

        # Connect and login
        if not client.connect():
            return 1

        if not client.login():
            return 1

        # Handle override
        if args.handle:
            client.set_handle(args.handle)

        # RTSP server
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

        # Resolve segment duration
        segment_secs = int(args.segment_mins * 60) if args.segment_mins else args.duration
        total_secs   = int(args.total_mins * 60)   if args.total_mins   else None

        if args.segment_mins:
            print(f"[*] Segment duration: {args.segment_mins:.1f} min ({segment_secs}s)")
        if args.total_mins:
            print(f"[*] Total recording:  {args.total_mins:.1f} min ({total_secs}s)")

        # Record stream
        recorder = StreamRecorder(client, enable_audio=not args.no_audio)
        video_file = recorder.record(
            duration=segment_secs,
            output_dir=args.output_dir,
            rtsp_server=rtsp_server,
            total_duration=total_secs,
        )

        # Stop RTSP
        if rtsp_server:
            rtsp_server.stop()

        # Convert to MP4
        if not args.no_mp4 and video_file:
            try:
                from v380_4g.mp4_muxer import MP4Muxer
                audio_file = video_file.replace('.h265', '.aac')
                mp4_file = video_file.replace('.h265', '.mp4')

                audio_path = audio_file if os.path.exists(audio_file) and os.path.getsize(audio_file) > 0 else None

                print(f"\n[*] Converting to MP4...")
                muxer = MP4Muxer(video_file, audio_path)
                if muxer.mux(mp4_file):
                    print(f"[+] MP4 saved: {mp4_file}")

                    # Clean up raw files unless --keep-raw
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


if __name__ == "__main__":
    exit(main())
