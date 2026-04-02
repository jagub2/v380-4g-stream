"""
V380 Alarm-Triggered Live Recorder

Connects AlarmRecorder (cloud polling) to StreamRecorder (live camera stream).
When an alarm fires, starts a live recording session for a configurable duration.

Design:
  - AlarmRecorder runs in a background daemon thread polling the cloud API.
  - AlarmTriggeredRecorder.run() blocks the main thread, owns signal handling.
  - On each new alarm the callback fires instantly (before cloud clip download),
    setting a threading.Event that wakes the main thread.
  - The main thread calls StreamRecorder.record() with total_duration.
  - Alarms that arrive during an active recording extend the deadline.
  - Alarms that arrive after recording ends start a fresh session.

Usage:
    client = V380Client(device_id, password)
    client.register(); client.connect(); client.login()

    alarm_rec = AlarmRecorder(access_token, user_id, device_id, password)
    triggered = AlarmTriggeredRecorder(
        client=client,
        alarm_recorder=alarm_rec,
        record_secs=300,          # 5 minutes per alarm
        segment_secs=60,          # 1-minute MP4 segments
        output_dir="recordings",
    )
    triggered.run()               # blocks; Ctrl-C to exit
"""

import logging
import threading
import time
from typing import Optional

from .alarm_recorder import AlarmRecorder, ALARM_TYPE_NAMES
from .stream import StreamRecorder

log = logging.getLogger(__name__)


class AlarmTriggeredRecorder:
    """
    Watches for alarm events and starts a live recording for each trigger.

    Args:
        client:          Authenticated V380Client instance.
        alarm_recorder:  AlarmRecorder instance (not yet started).
        record_secs:     How long to record after the last alarm (default 300 = 5 min).
                         If another alarm arrives while recording, the deadline
                         extends by record_secs from that alarm's arrival time.
        segment_secs:    Length of each MP4 segment within a session (default 60s).
        output_dir:      Directory for live recording output files.
        output_prefix:   Filename prefix for live recordings (default "alarm_live").
        no_audio:        Disable audio recording.
    """

    def __init__(self,
                 client,
                 alarm_recorder: AlarmRecorder,
                 record_secs: int = 300,
                 segment_secs: int = 60,
                 output_dir: str = "recordings",
                 output_prefix: str = "alarm_live",
                 no_audio: bool = False,
                 max_alarm_age_secs: int = 90):
        self.client           = client
        self.alarm_recorder   = alarm_recorder
        self.record_secs      = record_secs
        self.segment_secs     = segment_secs
        self.output_dir       = output_dir
        self.output_prefix    = output_prefix
        self.no_audio         = no_audio
        # Alarms older than this are downloaded as clips but do NOT trigger
        # live recording. Set to poll_interval * 3 by default, which means
        # only alarms that arrived within the last 3 poll cycles are "fresh".
        # Historical alarms fetched on startup (covering max_clip_age_hours)
        # are silently skipped for live triggering.
        self.max_alarm_age_secs = max_alarm_age_secs

        # Thread communication
        self._trigger   = threading.Event()   # set by alarm callback
        self._stop      = threading.Event()   # set by stop() / Ctrl-C
        self._deadline  = 0.0                 # time.time() when recording should end
        self._lock      = threading.Lock()    # protects _deadline

        # Wire our callback into the alarm recorder
        alarm_recorder.on_alarm_callback = self._on_alarm

    # ------------------------------------------------------------------
    # Alarm callback (called from AlarmRecorder background thread)
    # ------------------------------------------------------------------

    def _on_alarm(self, alarm: dict):
        atype = ALARM_TYPE_NAMES.get(alarm.get("type", 0), "?")
        ts_ms = alarm.get("itime", int(time.time() * 1000))
        age_s = time.time() - ts_ms / 1000

        # Ignore historical alarms fetched on startup — they are downloaded
        # as cloud clips but should not trigger a live recording session now.
        if age_s > self.max_alarm_age_secs:
            log.debug(
                "[trigger] ignoring stale alarm  type=%s  age=%.0fs  (threshold=%ds)",
                atype, age_s, self.max_alarm_age_secs,
            )
            return

        log.info("[trigger] alarm received  type=%s  itime=%d  age=%.0fs",
                 atype, ts_ms, age_s)

        with self._lock:
            # Extend (or set) the recording deadline
            new_deadline = time.time() + self.record_secs
            if new_deadline > self._deadline:
                self._deadline = new_deadline

        # Wake main thread
        self._trigger.set()

    # ------------------------------------------------------------------
    # Main loop (call from main thread)
    # ------------------------------------------------------------------

    def run(self):
        """
        Block until Ctrl-C, recording live video after each alarm.

        Call this from the main thread after client.connect() + client.login().
        Starts the AlarmRecorder background thread automatically.
        """
        self.alarm_recorder.start()
        log.info(
            "[trigger] ready — record_secs=%d  segment_secs=%d  output=%s",
            self.record_secs, self.segment_secs, self.output_dir,
        )
        print(f"[*] Alarm-triggered recorder active")
        print(f"    Will record {self.record_secs}s of live video per alarm trigger")
        print(f"    Waiting for alarms (poll every {self.alarm_recorder.poll_interval}s)...")
        print("[*] Press Ctrl-C to stop\n")

        try:
            while not self._stop.is_set():
                # Wait for an alarm signal (check stop flag every second)
                if not self._trigger.wait(timeout=1.0):
                    continue
                self._trigger.clear()

                if self._stop.is_set():
                    break

                with self._lock:
                    deadline = self._deadline

                remaining = max(0, deadline - time.time())
                if remaining < 1:
                    continue

                print(f"[!] Alarm triggered — recording for {remaining:.0f}s")
                self._record_session()
                print("[*] Recording ended — waiting for next alarm\n")

        except KeyboardInterrupt:
            pass
        finally:
            self.alarm_recorder.stop()
            log.info("[trigger] stopped")

    def stop(self):
        """Signal run() to exit cleanly."""
        self._stop.set()
        self._trigger.set()

    # ------------------------------------------------------------------
    # Recording session
    # ------------------------------------------------------------------

    def _record_session(self):
        """
        Record until the deadline, using segment_secs-length MP4 segments.
        The deadline can be extended mid-session by new alarms arriving.
        """
        # StreamRecorder.record() is a blocking loop with total_duration support.
        # We drive it in short bursts so we can check for deadline extensions.
        BURST = self.segment_secs  # one segment per burst

        while not self._stop.is_set():
            with self._lock:
                remaining = self._deadline - time.time()

            if remaining <= 0:
                break

            burst = min(BURST, remaining)
            log.info("[trigger] recording burst  remaining=%.0fs  burst=%.0fs",
                     remaining, burst)

            try:
                recorder = StreamRecorder(
                    self.client,
                    enable_audio=not self.no_audio,
                )
                recorder.record(
                    duration=int(burst),
                    output_dir=self.output_dir,
                    output_prefix=self.output_prefix,
                    total_duration=int(burst),
                )
            except Exception as e:
                log.error("[trigger] recording error: %s", e)
                # Brief pause before retry to avoid tight error loop
                time.sleep(2)
                # Reconnect if needed
                if not self.client.is_connected:
                    log.info("[trigger] reconnecting...")
                    try:
                        self.client.disconnect()
                        if not self.client.connect() or not self.client.login():
                            log.error("[trigger] reconnection failed — aborting session")
                            break
                    except Exception as re:
                        log.error("[trigger] reconnect error: %s", re)
                        break
