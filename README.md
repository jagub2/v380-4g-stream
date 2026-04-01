# v380-4g-stream

Record and decrypt live video from V380 (Macro Video) 4G cameras via the
cloud relay infrastructure. Designed for cameras where local LAN streaming
is not an option. Also polls the V380 cloud for motion/AI alarm events and
downloads the associated short clips.

## Features

- Continuous dashcam-style recording to rolling MP4 segments
- Automatic H.265 decryption and MP4 muxing (ffmpeg if available, pure Python fallback)
- Alarm-triggered recording: starts a live session instantly when motion is detected
- Cloud alarm clip download (short clips attached to each alarm event)
- RTSP server for live viewing in VLC or Home Assistant
- Auto-discovery of the closest regional streaming server
- No proprietary SDK — all protocols reverse-engineered from the official app

## Installation

```bash
pip install pycryptodome requests
```

ffmpeg is optional but recommended for reliable MP4 output:
```bash
# Arch/Manjaro
sudo pacman -S ffmpeg
```

Python 3.10+ required (uses `match`-free code, but `X | Y` type hints).

## Credentials

Two sets of credentials are used:

**Camera password** (`-p` / `--password`)
The device password printed on the camera label or set in the V380 Pro app.
Used for the direct camera TCP connection (`v380_stream.py`).

**Cloud token** (`--alarm-token` / `access_token` in config)
Required for alarm polling and alarm clip download only. Obtained once via
Wireshark on the Windows V380 Pro app:

1. Install [Wireshark](https://www.wireshark.org/) and the V380 Pro Windows app
2. Start capture, filter: `tcp.port == 8002`
3. Log in to the Windows app
4. Find `POST /user/pc-login` → copy `access_token` and `user_id` from the response
5. Find `POST /device/list` → copy `device_id`, base64-decode `device_password`, copy `rand_key`

Using a Windows app token **does not** log out the mobile app — they use
separate session endpoints (`/user/pc-login` vs `/user/login`).

## Usage

### Continuous recording

```bash
# Record indefinitely, 60s segments (default)
python v380_stream.py -d DEVICE_ID -p PASSWORD

# 2-minute segments, stop after 1 hour
python v380_stream.py -d DEVICE_ID -p PASSWORD --segment-mins 2 --total-mins 60

# Auto-discover the lowest-latency regional server
python v380_stream.py -d DEVICE_ID -p PASSWORD --auto-server

# RTSP live view (open rtsp://localhost:8554/stream in VLC)
python v380_stream.py -d DEVICE_ID -p PASSWORD --rtsp
```

### Alarm-triggered recording

Polls the cloud alarm API in the background. When motion or AI detection
fires, immediately starts a live recording session for `--record-mins`
minutes (default 5). If another alarm arrives before the session ends,
the deadline extends.

```bash
# Inline credentials
python v380_stream.py -d DEVICE_ID -p PASSWORD \
    --alarm-trigger \
    --alarm-token ACCESS_TOKEN \
    --alarm-user-id USER_ID

# Using a config file
python v380_stream.py -d DEVICE_ID -p PASSWORD \
    --alarm-trigger --alarm-config alarm_config.json

# Custom duration (3 minutes per trigger)
python v380_stream.py -d DEVICE_ID -p PASSWORD \
    --alarm-trigger --alarm-config alarm_config.json --record-mins 3
```

`alarm_config.json` example:
```json
{
  "access_token": "cc546d1f...",
  "user_id": 93643276,
  "device_id": 104007820,
  "device_password": "plaintext_camera_password",
  "output_dir": "recordings",
  "poll_interval": 15,
  "alarm_types": [0],
  "max_clip_age_hours": 24
}
```

### Alarm clip download only

Downloads the short cloud clips attached to each alarm event (independent
of live recording):

```bash
# Run with a config file
python v380_alarm.py --config alarm_config.json

# Discover devices for a token
python v380_alarm.py --token ACCESS_TOKEN --devices

# Motion and human detection only
python v380_alarm.py --config alarm_config.json --motion-only

# Write a config template
python v380_alarm.py --init
```

## v380_stream.py options

| Option | Default | Description |
|--------|---------|-------------|
| `-d, --device-id` | required | Camera device ID (from QR code or app) |
| `-p, --password` | required | Camera device password |
| `-t, --duration` | 60 | Segment duration in seconds |
| `--segment-mins` | — | Segment duration in minutes (overrides `-t`) |
| `--total-mins` | — | Stop automatically after this many minutes |
| `-o, --output-dir` | `recordings` | Output directory |
| `--server` | — | Override API server IP |
| `--auto-server` | — | Auto-discover closest server via HTTP dispatch |
| `--handle` | — | Override AES encryption handle |
| `--no-audio` | — | Disable audio |
| `--no-mp4` | — | Keep raw H.265/AAC, skip MP4 conversion |
| `--keep-raw` | — | Keep raw files alongside MP4 |
| `--rtsp` | — | Start local RTSP server |
| `--rtsp-port` | 8554 | RTSP port |
| `--alarm-trigger` | — | Enable alarm-triggered recording mode |
| `--alarm-token` | — | Cloud access_token for alarm polling |
| `--alarm-user-id` | — | Cloud user_id |
| `--alarm-config` | — | Path to alarm JSON config file |
| `--record-mins` | 5 | Live recording duration per alarm trigger |
| `--debug` | — | Verbose logging |

## Output files

```
recordings/
  v380_20260101_120000.mp4          continuous recording segments
  alarm_live_20260101_120000.mp4    alarm-triggered live recording segments
  alarm_20260101_120000_motion_42.mp4   downloaded alarm cloud clips
```

## Server discovery

By default the tool connects to a hardcoded relay IP (`194.195.251.29`).
`--auto-server` queries `dispatch.av380.net:8001/api/v1/get_stream_server`
with your device ID (signed with SHA-1 + `hsdata2022`) and picks the
closest regional relay. This typically gives lower latency than the default.

## Project structure

```
v380_stream.py                  CLI — live recording + alarm-triggered mode
v380_alarm.py                   CLI — alarm clip download only
v380_4g/
├── __init__.py
├── client.py                   TCP client (connect, auth, discover_stream_server)
├── crypto.py                   AES key derivation and selective decryption
├── stream.py                   Live streaming and dashcam segment recording
├── alarm_recorder.py           Cloud alarm polling and clip download
├── triggered_recorder.py       Wires AlarmRecorder → StreamRecorder
├── mp4_muxer.py                Pure Python H.265 → MP4 muxer (ffmpeg fallback)
└── rtsp_server.py              Local RTSP server for VLC / HA integration
```

## Requirements

- Python 3.10+
- pycryptodome
- requests
- ffmpeg (optional, recommended for MP4 output)

---

All code Generated by A.I. tools

## License

MIT License
