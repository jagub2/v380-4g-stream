"""
V380 Cloud Alarm Recorder

Polls the V380 cloud alarm API for motion/AI detection events and
downloads the associated short video clips.

Reuses MP4Muxer from this package for muxing raw H.265 streams into
playable MP4 files.

Alarm video format: 16-byte V380 proprietary header + raw H.265 stream.
No encryption — the data is playable as-is after stripping the header.

Credentials are obtained from Wireshark capture of the Windows V380 Pro
app (plain HTTP on port 8002 to cloud.av380.net):
  - access_token, user_id  → POST /user/pc-login response
  - device_id, device_password (base64-decode it), rand_key
                           → POST /device/list response

Using the Windows app token does NOT log out the mobile app —
/user/pc-login and /user/login are completely separate session endpoints.
"""

import base64
import hashlib
import json
import logging
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Optional

import requests

from .mp4_muxer import MP4Muxer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints (verified from Wireshark captures of PC + Android apps)
# ---------------------------------------------------------------------------

# Alarm list — Android/newer endpoint at port 443 with ftime-based query
_ALARM_LIST_HOST = "pushregcheck.nvcam.net"
_ALARM_LIST_PORT = 443
_ALARM_LIST_PATH = "/GetSSAlarmMsg/NVSSGetAlarmNewMessageList"

# Video — host comes from the alarm event's domain/ip field
_ALARM_VIDEO_PATH = "/api/v2/data/alarm/video"
_ALARM_VIDEO_PORT = 443

# Billing / device list (plain HTTP, captured via Wireshark)
_CLOUD_HOST = "cloud.av380.net"
_CLOUD_PORT = 8002

# Signing salts extracted from decompiled SDK, verified against captures
_SALT_LOGIN       = "hsshop2016"    # /device/list + /user/pc-login
_SALT_ALARM_VIDEO = "hsavdata2023"  # /api/v2/data/alarm/video

# Alarm type constants (AlarmMessageInfo.java)
ALARM_TYPE_ALL         = 0
ALARM_TYPE_SMOKE       = 1
ALARM_TYPE_MOTION      = 2
ALARM_TYPE_PIR         = 3
ALARM_TYPE_ACCESS_CTRL = 4
ALARM_TYPE_GAS         = 5
ALARM_TYPE_WARN        = 6
ALARM_TYPE_PWD_CHANGED = 7
ALARM_TYPE_HUMAN       = 8

ALARM_TYPE_NAMES = {
    0: "normal",   1: "smoke",      2: "motion",   3: "PIR",
    4: "access",   5: "gas",        6: "warn",
    7: "pwd_chg",  8: "human",
}


# ---------------------------------------------------------------------------
# Signing helpers (all verified against live captures)
# ---------------------------------------------------------------------------

def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _encode_usr(device_id: int) -> str:
    """base64(str(device_id)) — confirmed from PC and Android captures."""
    return base64.b64encode(str(device_id).encode()).decode().rstrip("=")


def _hash_pwd(password: str) -> str:
    """md5(plaintext_password) — verified from Wireshark."""
    return _md5(password)


def _sign_device_list(token: str, ts: int) -> str:
    return _md5(
        f"accesstoken={token}&timestamp={ts}&type=all&updatetimestamp={ts}"
        f"{_SALT_LOGIN}"
    )


def _sign_video_legacy(username: str, password: str, did: int,
                        alarm_time_ms: int, bucket_id: int,
                        image_only: bool = False, option: str = "") -> str:
    """
    Sign for legacy (platform==0) video request.
    alarm_time_ms stays in milliseconds — verified from Android capture.
    """
    return _md5(
        f"alarm_time={alarm_time_ms}"
        f"&bucket_id={bucket_id}"
        f"&dev_id={did}"
        f"&image_only={1 if image_only else 0}"
        f"&option={option}"
        f"&password={_hash_pwd(password)}"
        f"&username={username}"
        f"{_SALT_ALARM_VIDEO}"
    )


def _sign_video_iot(token: str, did: int, alarm_time_ms: int,
                     bucket_id: int, image_only: bool = False,
                     option: str = "") -> str:
    """Sign for IoT (platform==1) video request."""
    return _md5(
        f"alarm_time={alarm_time_ms}"
        f"&bucket_id={bucket_id}"
        f"&dev_id={did}"
        f"&image_only={1 if image_only else 0}"
        f"&option={option}"
        f"&token={token}"
        f"{_SALT_ALARM_VIDEO}"
    )


# ---------------------------------------------------------------------------
# Video decoding
# ---------------------------------------------------------------------------

def decode_alarm_video(data: bytes) -> bytes:
    """
    Strip the 16-byte V380 proprietary header from alarm video binary.

    Format confirmed from live captures:
        [0:16]  proprietary header (magic + device IDs)
        [16:]   raw H.265 stream starting with NALU start code 00 00 00 01

    The data is NOT encrypted — ffplay can play the raw bytes directly.
    This function just strips the header so the output is a clean H.265
    stream that MP4Muxer can handle.
    """
    H265_START = b"\x00\x00\x00\x01"
    HEADER_LEN = 16

    if len(data) < HEADER_LEN + 4:
        return data

    if data[HEADER_LEN:HEADER_LEN + 4] == H265_START:
        return data[HEADER_LEN:]

    # Scan in case header length varies
    for offset in range(4, min(64, len(data) - 4)):
        if data[offset:offset + 4] == H265_START:
            log.debug("stripped %d-byte header", offset)
            return data[offset:]

    log.warning("no H.265 start code found — returning raw data (%d bytes)", len(data))
    return data


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TokenExpiredError(Exception):
    """Server rejected the access_token — recapture from Wireshark."""


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def get_device_list(token: str) -> list[dict]:
    """
    Fetch all devices for this account.

    Automatically base64-decodes device_password into device_password_decoded.
    Useful for populating config: device_id, device_password, rand_key.
    """
    url = f"http://{_CLOUD_HOST}:{_CLOUD_PORT}/device/list"
    ts = int(time.time())
    payload = {
        "accesstoken":     token,
        "sign":            _sign_device_list(token, ts),
        "timestamp":       str(ts),
        "type":            "all",
        "updatetimestamp": str(ts),
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.error("device list failed: %s", e)
        return []

    if data.get("result") == 401 or data.get("error_code") == 401:
        raise TokenExpiredError("device list: token expired")
    if data.get("result", 0) <= 0:
        log.warning("device list result=%d", data.get("result", 0))
        return []

    devices = data.get("data", [])
    for d in devices:
        raw = d.get("device_password", "")
        if raw:
            try:
                d["device_password_decoded"] = base64.b64decode(raw + "==").decode()
            except Exception:
                d["device_password_decoded"] = raw
    return devices


def get_alarm_list(token: str, device_id: int, device_password: str,
                   since_ms: int, alarm_type: int = 0,
                   count: int = 50) -> list[dict]:
    """
    Fetch alarm events since since_ms (unix milliseconds).

    Uses the Android NVSSGetAlarmNewMessageList endpoint at port 443,
    confirmed from Android Wireshark capture.

    Returns list of alarm dicts newest-first.
    Raises TokenExpiredError on auth failure.

    Key alarm fields:
        id / aid  — unique alarm ID
        did       — device ID
        type      — alarm type (see ALARM_TYPE_* constants)
        itime     — unix ms timestamp when alarm fired
        vid       — video ID (0 = no clip for this alarm)
        bidx      — bucket index for video download
        domain    — alarm server domain (e.g. hkalarm.ak380.com)
        ip        — alarm server IP (fallback)
        platform  — 0=legacy pw auth, 1=IoT token auth
    """
    url = f"https://{_ALARM_LIST_HOST}:{_ALARM_LIST_PORT}{_ALARM_LIST_PATH}"
    body = {
        "count":          count,
        "did":            device_id,
        "filter_summary": "",
        "filter_tag":     [],
        "ftime":          since_ms,
        "key":            "",
        "pwd":            _hash_pwd(device_password),
        "type":           alarm_type,
        "usr":            _encode_usr(device_id),
        "version":        0,
    }
    encoded = base64.b64encode(
        json.dumps(body, separators=(",", ":")).encode()
    ).decode()

    try:
        r = requests.post(
            url,
            data=f"param={encoded}&type=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.error("alarm list failed: %s", e)
        return []
    except json.JSONDecodeError:
        log.error("alarm list response not JSON")
        return []

    result = data.get("result", 0)
    if result == 401 or data.get("error_code") == 401:
        raise TokenExpiredError("alarm list: token expired")
    if result <= 0:
        log.debug("alarm list result=%d desc=%d", result, data.get("desc", 0))
        return []

    return data.get("value") or []


def get_alarm_video(alarm: dict, token: str,
                    device_password: str) -> Optional[bytes]:
    """
    Download and decode the clip for an alarm event.

    alarm_time stays in MILLISECONDS in both sign and payload —
    verified against all three Android Wireshark captures.

    Returns decoded H.265 bytes (header stripped), or None on failure.
    """
    did           = alarm.get("did") or alarm.get("dev_id")
    alarm_time_ms = alarm.get("itime", 0)
    bucket_id     = alarm.get("bidx", 0)
    is_iot        = alarm.get("platform", 0) == 1
    username      = str(did)

    domain = alarm.get("domain")
    ip     = alarm.get("ip")
    if domain:
        host = f"https://{domain}:{_ALARM_VIDEO_PORT}"
    elif ip:
        host = f"http://{ip}:8883"
    else:
        host = f"https://{_ALARM_LIST_HOST}:{_ALARM_LIST_PORT}"

    url = f"{host}{_ALARM_VIDEO_PATH}"

    if is_iot:
        payload = {
            "dev_id":     did,
            "alarm_time": alarm_time_ms,
            "bucket_id":  bucket_id,
            "image_only": 0,
            "option":     "",
            "token":      token,
            "sign":       _sign_video_iot(
                token=token, did=did,
                alarm_time_ms=alarm_time_ms, bucket_id=bucket_id,
            ),
        }
    else:
        payload = {
            "dev_id":     did,
            "platform":   0,
            "username":   username,
            "password":   _hash_pwd(device_password),
            "alarm_time": alarm_time_ms,
            "bucket_id":  bucket_id,
            "image_only": 0,
            "option":     "",
            "sign":       _sign_video_legacy(
                username=username, password=device_password,
                did=did, alarm_time_ms=alarm_time_ms, bucket_id=bucket_id,
            ),
        }

    try:
        r = requests.post(url, json=payload, timeout=60, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("video request failed: %s", e)
        return None

    if "json" in r.headers.get("Content-Type", ""):
        try:
            body = r.json()
        except Exception:
            body = r.text
        log.warning("video returned JSON (clip may be on S3 — not yet implemented): %s", body)
        return None

    raw = r.content
    if len(raw) < 16:
        log.warning("video response too small (%d bytes)", len(raw))
        return None

    return decode_alarm_video(raw)


# ---------------------------------------------------------------------------
# AlarmRecorder
# ---------------------------------------------------------------------------

class AlarmRecorder:
    """
    Polls the V380 cloud for alarm events and saves decrypted MP4 clips.

    Designed to run alongside V380Client/StreamRecorder — alarm polling
    is purely HTTP and does not interact with the camera TCP connection.

    Usage:
        recorder = AlarmRecorder(
            access_token="...",
            user_id=93643276,
            device_id=104007820,
            device_password="plaintext_password",
        )
        recorder.start()   # background daemon thread
        # ... your main loop ...
        recorder.stop()
    """

    def __init__(self,
                 access_token: str,
                 user_id: int,
                 device_id: int,
                 device_password: str,
                 output_dir: str = "recordings",
                 poll_interval: int = 15,
                 alarm_types: Optional[list[int]] = None,
                 max_clip_age_hours: int = 24,
                 debug: bool = False):
        self.token         = access_token
        self.uid           = int(user_id)
        self.did           = int(device_id)
        self.device_pwd    = device_password
        self.output_dir    = Path(output_dir)
        self.poll_interval = poll_interval
        self.alarm_types   = alarm_types or [ALARM_TYPE_ALL]
        self.max_clip_age  = max_clip_age_hours * 3600
        self.debug         = debug

        if debug:
            log.setLevel(logging.DEBUG)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._seen_ids: set = set()
        self._last_seen_ms: int = int((time.time() - self.max_clip_age) * 1000)
        self._stop = Event()
        self._thread: Optional[Thread] = None

    def _filename(self, alarm: dict) -> Path:
        ts_ms = alarm.get("itime", int(time.time() * 1000))
        dt    = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        atype = ALARM_TYPE_NAMES.get(alarm.get("type", 0), "unknown")
        aid   = alarm.get("id") or alarm.get("aid") or ts_ms
        return self.output_dir / f"alarm_{dt:%Y%m%d_%H%M%S}_{atype}_{aid}.mp4"

    def _save_clip(self, h265_data: bytes, out_path: Path):
        """
        Save H.265 data as MP4.

        Tries ffmpeg first (reliable, handles any valid H.265 stream).
        Falls back to the pure-Python MP4Muxer if ffmpeg is not installed.
        If both fail, saves as raw .h265 playable with: ffplay -f hevc <file>
        """
        import shutil
        import subprocess

        h265_path = out_path.with_suffix(".h265")
        h265_path.write_bytes(h265_data)

        # --- Try ffmpeg first ---
        if shutil.which("ffmpeg"):
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-f", "hevc",
                        "-i", str(h265_path),
                        "-c:v", "copy",
                        str(out_path),
                    ],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    h265_path.unlink()
                    log.info("  saved %s  (%d KB)", out_path.name,
                             len(h265_data) // 1024)
                    return
                else:
                    log.warning("  ffmpeg failed (rc=%d): %s",
                                result.returncode,
                                result.stderr[-200:].decode(errors="replace"))
            except Exception as e:
                log.warning("  ffmpeg error: %s", e)

        # --- Fall back to pure-Python MP4Muxer ---
        log.debug("  falling back to MP4Muxer")
        muxer = MP4Muxer(video_path=str(h265_path), audio_path=None)
        ok = muxer.mux(str(out_path))
        if ok:
            try:
                h265_path.unlink()
            except OSError:
                pass
            log.info("  saved %s  (%d KB)", out_path.name,
                     len(h265_data) // 1024)
            return

        # --- Both failed: keep raw .h265 ---
        log.warning(
            "  muxing failed — keeping raw H.265: %s  "
            "Play with: ffplay -f hevc %s",
            h265_path.name, h265_path.name,
        )

    def _process(self, alarm: dict):
        aid   = alarm.get("id") or alarm.get("aid")
        atype = alarm.get("type", 0)
        ts_ms = alarm.get("itime", 0)
        vid   = alarm.get("vid", 0)

        log.info(
            "alarm  id=%-12s  type=%s (%s)  time=%s",
            aid, atype,
            ALARM_TYPE_NAMES.get(atype, "?"),
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
        )

        # vtype=0 means no cloud clip attached to this alarm.
        # vid can legitimately be 0 (it's a video ID, not a boolean flag)
        # so we don't use it to decide whether to attempt download.
        vtype = alarm.get("vtype", 0)
        if vtype == 0 and alarm.get("vid", -1) == 0:
            # Double-check: if both vtype=0 and vid=0 it's likely no video.
            # But still attempt the download — the server will return an error
            # response (JSON) rather than binary if there's truly no clip.
            log.debug("  vtype=0 vid=0 — attempting download anyway")

        out = self._filename(alarm)
        if out.exists():
            log.debug("  already saved: %s", out.name)
            return

        log.info("  downloading...")
        data = get_alarm_video(alarm=alarm, token=self.token,
                               device_password=self.device_pwd)
        if data is None:
            log.error("  download failed")
            return

        self._save_clip(data, out)

    def check_and_record(self):
        """
        Single poll cycle — fetch new alarms and download clips.
        Safe to call from your own loop instead of using start().
        """
        fetch_types = ([ALARM_TYPE_ALL] if ALARM_TYPE_ALL in self.alarm_types
                       else self.alarm_types)
        newest_ms = self._last_seen_ms

        for atype in fetch_types:
            try:
                alarms = get_alarm_list(
                    token=self.token,
                    device_id=self.did,
                    device_password=self.device_pwd,
                    since_ms=self._last_seen_ms,
                    alarm_type=atype,
                )
            except TokenExpiredError as e:
                log.error("TOKEN EXPIRED: %s", e)
                log.error(
                    "Refresh: open V380 Pro Windows app, Wireshark filter "
                    "tcp.port == 8002, log in, copy access_token from "
                    "/user/pc-login response. Does NOT log out the mobile app."
                )
                self._stop.set()
                return

            for alarm in alarms:
                aid = alarm.get("id") or alarm.get("aid")
                if aid in self._seen_ids:
                    continue
                self._seen_ids.add(aid)

                ts_ms = alarm.get("itime", 0)
                if ts_ms > newest_ms:
                    newest_ms = ts_ms

                if ALARM_TYPE_ALL not in self.alarm_types:
                    if alarm.get("type") not in self.alarm_types:
                        continue

                self._process(alarm)

        if newest_ms > self._last_seen_ms:
            self._last_seen_ms = newest_ms

        if len(self._seen_ids) > 10_000:
            self._seen_ids.clear()

    def _loop(self):
        log.info(
            "[alarm] started  device=%d  poll=%ds  types=%s  output=%s",
            self.did, self.poll_interval, self.alarm_types, self.output_dir,
        )
        while not self._stop.is_set():
            try:
                self.check_and_record()
            except Exception as e:
                log.exception("[alarm] unexpected error: %s", e)
            self._stop.wait(self.poll_interval)
        log.info("[alarm] stopped")

    def start(self) -> Thread:
        """Start polling in a background daemon thread. Returns the thread."""
        self._thread = Thread(target=self._loop, daemon=True,
                              name="alarm-recorder")
        self._thread.start()
        return self._thread

    def stop(self):
        """Signal the background thread to stop."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @classmethod
    def from_config(cls, config: dict, **kwargs) -> "AlarmRecorder":
        """Construct from a config dict (e.g. loaded from JSON file)."""
        return cls(
            access_token=config["access_token"],
            user_id=config["user_id"],
            device_id=config["device_id"],
            device_password=config["device_password"],
            output_dir=config.get("output_dir", "recordings"),
            poll_interval=config.get("poll_interval", 15),
            alarm_types=config.get("alarm_types", [ALARM_TYPE_ALL]),
            max_clip_age_hours=config.get("max_clip_age_hours", 24),
            **kwargs,
        )

    @staticmethod
    def print_devices(token: str):
        """Print device info for all devices on the account."""
        devices = get_device_list(token)
        if not devices:
            print("[!] No devices found (or request failed)")
            return
        for d in devices:
            print(f"\n  Device: {d.get('nickname', 'unknown')}")
            print(f"    device_id:       {d.get('device_id')}")
            print(f"    device_password: {d.get('device_password_decoded', '?')}")
            print(f"    rand_key:        {d.get('rand_key')}")
            print(f"    model:           {d.get('device_model')}")
            print(f"    platform:        {d.get('platform', 0)}  (0=legacy, 1=IoT)")
