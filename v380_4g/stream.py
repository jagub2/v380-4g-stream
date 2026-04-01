"""
V380 Live Streaming

Stream and decrypt live video/audio from V380 cameras.
Supports dashcam-style continuous recording: saves rolling MP4 segments
of fixed duration while streaming without interruption.
"""

import struct
import socket
import signal
import time
import os
import threading
from datetime import datetime
from typing import Optional, Tuple

from .client import V380Client
from .crypto import decrypt_64_80, decrypt_audio
from .mp4_muxer import MP4Muxer

# Global flag for Ctrl-C handling
_stop_streaming = False


def _signal_handler(sig, frame):
    global _stop_streaming
    _stop_streaming = True
    print("\n[!] Ctrl-C detected - stopping stream...")


class _Segment:
    """
    Collects raw H.265 / AAC data for one dashcam segment.
    Closed and handed off to a background muxer thread when the
    segment duration elapses.
    """

    def __init__(self, h265_path: str, aac_path: Optional[str]):
        self.h265_path = h265_path
        self.aac_path = aac_path
        self.start_time = time.time()
        self._h265_f = open(h265_path, 'wb')
        self._aac_f = open(aac_path, 'wb') if aac_path else None

    def write_video(self, data: bytes):
        self._h265_f.write(data)

    def write_audio(self, data: bytes):
        if self._aac_f:
            self._aac_f.write(data)

    def close(self) -> float:
        """Close files and return elapsed seconds for this segment."""
        elapsed = time.time() - self.start_time
        self._h265_f.close()
        if self._aac_f:
            self._aac_f.close()
        return elapsed

    def mux_to_mp4(self, mp4_path: str, elapsed: float):
        """Mux raw streams to MP4. Runs in a background thread."""
        audio_path = self.aac_path if (self.aac_path and os.path.getsize(self.aac_path) > 0) else None
        muxer = MP4Muxer(
            video_path=self.h265_path,
            audio_path=audio_path,
            duration_seconds=elapsed,
        )
        ok = muxer.mux(mp4_path)
        # Clean up raw intermediates
        try:
            os.remove(self.h265_path)
            if self.aac_path:
                os.remove(self.aac_path)
        except OSError:
            pass
        if ok:
            print(f"[+] Saved segment: {mp4_path}")
        else:
            print(f"[!] Muxing failed for segment: {mp4_path}")


class StreamRecorder:
    """Stream and record live video/audio from V380 camera"""

    HEADER_SIZE = 12
    KEEPALIVE_PACKET = bytes.fromhex("01210000000000000010000000000000")

    def __init__(self, client: V380Client, enable_audio: bool = True):
        self.client = client
        self.enable_audio = enable_audio

        # Frame reassembly state
        self._frame_chunks = {}
        self._current_frame_start = None
        self._current_total = 0
        self._current_is_iframe = False

        # Last seen VPS/SPS/PPS as complete Annex B bytes.
        # Written at the start of every new segment so the muxer always
        # has parameter sets even when rolling over mid-stream.
        self._param_sets: bytes = b""

    def record(self, duration: int = 60, output_dir: str = "recordings",
               output_prefix: str = "v380", rtsp_server=None,
               total_duration: Optional[int] = None) -> None:
        """
        Stream continuously, saving rolling MP4 segments of `duration` seconds.

        Like a dashcam: each segment is muxed to MP4 in the background while
        the next segment is already being recorded. No frames are lost between
        segments.

        Args:
            duration:       Length of each MP4 segment in seconds.
            output_dir:     Directory for output files (created if needed).
            output_prefix:  Filename prefix, e.g. "v380" → "v380_20250101_120000.mp4".
            rtsp_server:    Optional RTSP server for simultaneous live viewing.
            total_duration: Total recording cap in seconds. Stream stops
                            automatically after this many seconds (in addition
                            to Ctrl-C). None = record indefinitely.
        """
        stream_sock = self.client.create_stream_socket()
        if not stream_sock:
            return None

        os.makedirs(output_dir, exist_ok=True)

        record_audio = self.client.audio_supported and self.enable_audio
        if record_audio:
            print("[*] Audio: enabled")
        elif not self.client.audio_supported:
            print("[*] Audio: not supported by camera")
        else:
            print("[*] Audio: disabled by user")

        print(f"[*] Recording {duration}s segments to {output_dir}/")
        print("[*] Press Ctrl-C to stop")

        global _stop_streaming
        _stop_streaming = False
        old_handler = signal.signal(signal.SIGINT, _signal_handler)

        def new_segment() -> _Segment:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            h265 = os.path.join(output_dir, f".{output_prefix}_{ts}.h265")
            aac  = os.path.join(output_dir, f".{output_prefix}_{ts}.aac") if record_audio else None
            return _Segment(h265, aac)

        def close_and_mux(seg: _Segment):
            elapsed = seg.close()
            # Skip muxing if the segment has no actual video data.
            # This happens when total_duration == segment duration: the
            # rollover and stop check fire in the same iteration, creating
            # a segment that only contains the param-set header (or nothing).
            h265_size = os.path.getsize(seg.h265_path) if os.path.exists(seg.h265_path) else 0
            param_only = len(self._param_sets) if self._param_sets else 0
            if h265_size <= param_only:
                # Nothing useful — clean up silently
                try:
                    os.remove(seg.h265_path)
                    if seg.aac_path and os.path.exists(seg.aac_path):
                        os.remove(seg.aac_path)
                except OSError:
                    pass
                return
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            mp4 = os.path.join(output_dir, f"{output_prefix}_{ts}.mp4")
            # Non-daemon so the process doesn't exit before muxing finishes.
            # Tracked in mux_threads so record() can join them all before returning.
            t = threading.Thread(target=seg.mux_to_mp4, args=(mp4, elapsed),
                                 daemon=False)
            mux_threads.append(t)
            t.start()

        segment = new_segment()
        segment_start = time.time()

        total_video_bytes = 0
        total_audio_bytes = 0
        session_start = time.time()
        last_keepalive = time.time()
        last_progress = time.time()
        buffer = bytearray()
        mux_threads: list = []

        try:
            while not _stop_streaming:
                try:
                    data = stream_sock.recv(65536)
                    if not data:
                        print("[!] Stream closed by server")
                        break

                    buffer.extend(data)

                    video, audio, remaining = self._process_stream_data(
                        bytes(buffer), record_audio, rtsp_server
                    )
                    buffer = bytearray(remaining)

                    if video:
                        # Update cached VPS/SPS/PPS from this chunk of video data
                        self._cache_param_sets(video)
                        segment.write_video(video)
                        total_video_bytes += len(video)

                    if audio:
                        segment.write_audio(audio)
                        total_audio_bytes += len(audio)

                    now = time.time()

                    # Stop if total recording cap reached — check BEFORE
                    # rollover so we don't create an empty segment then discard it.
                    if total_duration is not None and now - session_start >= total_duration:
                        print(f"\n[*] Reached total duration limit ({total_duration}s) — stopping")
                        _stop_streaming = True

                    # Roll over to a new segment when duration elapses,
                    # but only if we're not about to stop.
                    if not _stop_streaming and now - segment_start >= duration:
                        close_and_mux(segment)
                        segment = new_segment()
                        # Prepend cached parameter sets so the new segment is
                        # self-contained and muxable regardless of where in
                        # the GOP the rollover happened.
                        if self._param_sets:
                            segment.write_video(self._param_sets)
                        segment_start = now

                    # Keepalive every 5 s
                    if now - last_keepalive >= 5:
                        stream_sock.sendall(self.KEEPALIVE_PACKET)
                        last_keepalive = now

                    # Progress every 10 s
                    if now - last_progress >= 10:
                        elapsed = now - session_start
                        seg_elapsed = now - segment_start
                        if record_audio:
                            print(f"  {elapsed:.0f}s total | segment {seg_elapsed:.0f}/{duration}s"
                                  f" | video {total_video_bytes/1024:.0f} KB"
                                  f" | audio {total_audio_bytes/1024:.0f} KB")
                        else:
                            print(f"  {elapsed:.0f}s total | segment {seg_elapsed:.0f}/{duration}s"
                                  f" | video {total_video_bytes/1024:.0f} KB")
                        last_progress = now

                except socket.timeout:
                    stream_sock.sendall(self.KEEPALIVE_PACKET)
                    last_keepalive = time.time()

        except Exception as e:
            print(f"[!] Stream error: {e}")
            if self.client.debug:
                import traceback
                traceback.print_exc()

        finally:
            # Flush the current (incomplete) segment
            print("[*] Flushing final segment...")
            close_and_mux(segment)
            signal.signal(signal.SIGINT, old_handler)
            stream_sock.close()

        elapsed = time.time() - session_start
        print(f"[+] Session ended after {elapsed:.0f}s")

        # Wait for all background mux threads to finish before returning.
        # They are non-daemon threads, but joining explicitly ensures MP4
        # files are fully written before the caller disconnects / exits.
        pending = [t for t in mux_threads if t.is_alive()]
        if pending:
            print(f"[*] Waiting for {len(pending)} segment(s) to finish muxing...")
            for t in pending:
                t.join()

    # ------------------------------------------------------------------
    # Internal stream processing (unchanged from original)
    # ------------------------------------------------------------------

    def _process_stream_data(self, data: bytes, record_audio: bool,
                             rtsp_server=None) -> Tuple[bytes, bytes, bytes]:
        """Process and decrypt stream packets"""
        video_result = bytearray()
        audio_result = bytearray()
        pos = 0

        while pos < len(data):
            # Video packet (0x7f28 = I-frame, 0x7f29 = P-frame)
            if data[pos] == 0x7f and pos + 1 < len(data) and data[pos+1] in [0x28, 0x29]:
                if pos + self.HEADER_SIZE > len(data):
                    break

                is_iframe = (data[pos+1] == 0x28)
                total_frame = struct.unpack('<H', data[pos+3:pos+5])[0]
                cur_frame   = struct.unpack('<H', data[pos+5:pos+7])[0]
                pkt_len     = struct.unpack('<H', data[pos+7:pos+9])[0]
                packet_end  = pos + self.HEADER_SIZE + pkt_len

                if packet_end > len(data):
                    break

                payload = data[pos+12:packet_end]

                if cur_frame == 0:
                    # Flush any previously assembled complete frame
                    if self._current_frame_start is not None and 'current' in self._frame_chunks:
                        if len(self._frame_chunks['current']) >= self._current_total:
                            decrypted = self._decrypt_frame(
                                self._frame_chunks['current'],
                                self._current_is_iframe
                            )
                            video_result.extend(decrypted)
                            if rtsp_server:
                                rtsp_server.send_frame(decrypted)
                        self._frame_chunks.pop('current', None)

                    # Start new frame
                    self._current_frame_start = pos
                    self._current_total = total_frame
                    self._current_is_iframe = is_iframe
                    self._frame_chunks['current'] = [(cur_frame, payload)]
                else:
                    if 'current' in self._frame_chunks:
                        self._frame_chunks['current'].append((cur_frame, payload))

                        if len(self._frame_chunks['current']) >= self._current_total:
                            decrypted = self._decrypt_frame(
                                self._frame_chunks['current'],
                                self._current_is_iframe
                            )
                            video_result.extend(decrypted)
                            if rtsp_server:
                                rtsp_server.send_frame(decrypted)
                            self._frame_chunks.pop('current', None)
                            self._current_frame_start = None

                pos = packet_end

            # Audio packet (0x7f18)
            elif data[pos] == 0x7f and pos + 1 < len(data) and data[pos+1] == 0x18:
                if pos + self.HEADER_SIZE > len(data):
                    break

                total_frame = struct.unpack('<H', data[pos+3:pos+5])[0]
                cur_frame   = struct.unpack('<H', data[pos+5:pos+7])[0]
                pkt_len     = struct.unpack('<H', data[pos+7:pos+9])[0]
                packet_end  = pos + self.HEADER_SIZE + pkt_len

                # Sanity check for false audio headers
                if pkt_len > 1000 or total_frame > 10 or packet_end > len(data):
                    pos += 1
                    continue

                if not record_audio:
                    pos = packet_end
                    continue

                audio_payload = data[pos+12:packet_end]
                if cur_frame == 0 and len(audio_payload) > 16:
                    audio_payload = audio_payload[16:]  # Skip metadata

                decrypted = decrypt_audio(audio_payload, self.client.cipher)
                audio_result.extend(decrypted)

                pos = packet_end
            else:
                pos += 1

        # Flush any remaining complete frame
        if self._current_frame_start is not None and 'current' in self._frame_chunks:
            if len(self._frame_chunks['current']) >= self._current_total:
                decrypted = self._decrypt_frame(
                    self._frame_chunks['current'],
                    self._current_is_iframe
                )
                video_result.extend(decrypted)
                if rtsp_server:
                    rtsp_server.send_frame(decrypted)
                self._frame_chunks.pop('current', None)
                self._current_frame_start = None

        return bytes(video_result), bytes(audio_result), data[pos:]

    def _decrypt_frame(self, chunks: list, is_iframe: bool) -> bytes:
        """Decrypt and reassemble video frame"""
        chunks.sort(key=lambda x: x[0])

        frame_data = bytearray()
        for cur_frame, payload in chunks:
            if cur_frame == 0:
                frame_data.extend(payload[16:])  # Skip metadata
            else:
                frame_data.extend(payload)

        if is_iframe or len(frame_data) >= 64:
            return decrypt_64_80(bytes(frame_data), self.client.cipher)
        else:
            return bytes(frame_data)

    def _cache_param_sets(self, video: bytes):
        """
        Scan Annex B video bytes for VPS/SPS/PPS NAL units (types 32/33/34)
        and update self._param_sets so they can be prepended to new segments.
        """
        START4 = b'\x00\x00\x00\x01'
        START3 = b'\x00\x00\x01'

        found = {}   # nal_type -> nalu_bytes (without start code)

        i = 0
        while i < len(video) - 4:
            if video[i:i+4] == START4:
                start = i + 4
                i += 4
            elif video[i:i+3] == START3:
                start = i + 3
                i += 3
            else:
                i += 1
                continue

            if start >= len(video):
                break

            nal_type = (video[start] >> 1) & 0x3F
            if nal_type not in (32, 33, 34):   # VPS, SPS, PPS only
                i = start
                continue

            # Find the end of this NALU
            end = len(video)
            j = start + 1
            while j < len(video) - 3:
                if video[j:j+4] == START4 or video[j:j+3] == START3:
                    end = j
                    break
                j += 1

            found[nal_type] = video[start:end]
            i = end

        if not found:
            return

        # Rebuild _param_sets in canonical order: VPS → SPS → PPS
        buf = bytearray()
        for nal_type in (32, 33, 34):
            # Use freshly found NALU, or keep existing one
            if nal_type in found:
                buf += START4 + found[nal_type]
            elif self._param_sets:
                # Try to preserve existing param set for this type
                existing = self._extract_one_param_set(self._param_sets, nal_type)
                if existing:
                    buf += START4 + existing

        if buf:
            self._param_sets = bytes(buf)

    @staticmethod
    def _extract_one_param_set(data: bytes, nal_type: int) -> bytes:
        """Extract a single NALU of the given type from an Annex B byte string."""
        START4 = b'\x00\x00\x00\x01'
        START3 = b'\x00\x00\x01'
        i = 0
        while i < len(data) - 4:
            if data[i:i+4] == START4:
                start = i + 4; i += 4
            elif data[i:i+3] == START3:
                start = i + 3; i += 3
            else:
                i += 1; continue
            if start >= len(data):
                break
            if (data[start] >> 1) & 0x3F == nal_type:
                end = len(data)
                j = start + 1
                while j < len(data) - 3:
                    if data[j:j+4] == START4 or data[j:j+3] == START3:
                        end = j; break
                    j += 1
                return data[start:end]
            i = start
        return b""
