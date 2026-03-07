#!/usr/bin/env python3
"""
RTSP Server for V380 camera streams

Provides live RTSP streaming of decrypted V380 video.
Connect with: vlc rtsp://localhost:8554/stream
        or:  mpv rtsp://localhost:8554/stream
        or:  ffplay rtsp://localhost:8554/stream
"""

import socket
import threading
import struct
import time
import random
from typing import Optional


class RTPPacketizer:
    """Packetize H.265 NAL units into RTP packets"""

    def __init__(self, ssrc: int = None):
        self.ssrc = ssrc or random.randint(0, 0xFFFFFFFF)
        self.sequence = random.randint(0, 0xFFFF)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self.payload_type = 96
        self._ts_origin = random.randint(0, 0xFFFFFFFF)

    def packetize_nal(self, nal_data: bytes, is_last: bool = True) -> list:
        """Convert a NAL unit to RTP packets"""
        packets = []
        max_payload = 1400

        if len(nal_data) <= max_payload:
            packets.append(self._make_rtp_packet(nal_data, is_last))
        else:
            nal_type = (nal_data[0] >> 1) & 0x3F
            fu_header1 = (nal_data[0] & 0x81) | (49 << 1)
            fu_header2 = nal_data[1]

            offset = 2
            first = True

            while offset < len(nal_data):
                chunk_size = min(max_payload - 3, len(nal_data) - offset)
                last_fragment = (offset + chunk_size >= len(nal_data))

                fu_indicator = bytes([fu_header1, fu_header2])

                fu_type = nal_type
                if first:
                    fu_header = 0x80 | fu_type
                    first = False
                elif last_fragment:
                    fu_header = 0x40 | fu_type
                else:
                    fu_header = fu_type

                payload = fu_indicator + bytes([fu_header]) + nal_data[offset:offset + chunk_size]
                packets.append(self._make_rtp_packet(payload, is_last and last_fragment))
                offset += chunk_size

        return packets

    def _make_rtp_packet(self, payload: bytes, marker: bool) -> bytes:
        """Create an RTP packet with header"""
        byte0 = 0x80
        byte1 = (0x80 if marker else 0) | self.payload_type

        header = struct.pack('>BBHII',
            byte0,
            byte1,
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc
        )

        self.sequence = (self.sequence + 1) & 0xFFFF
        return header + payload

    def set_timestamp_from_wallclock(self, wall_seconds: float):
        """
        Derive RTP timestamp from wall clock (90 kHz H.265 clock).
        Call once per frame before packetizing so all packets in a
        frame share the same timestamp and the sequence is monotonic.
        """
        self.timestamp = int(wall_seconds * 90000 + self._ts_origin) & 0xFFFFFFFF

    def advance_timestamp(self, ticks: int = 3600):
        """Advance timestamp by fixed ticks (legacy fallback)."""
        self.timestamp = (self.timestamp + ticks) & 0xFFFFFFFF


class RTSPServer:
    """Simple RTSP server for live streaming"""

    def __init__(self, port: int = 8554):
        self.port = port
        self.server_socket = None
        self.clients = []
        self.running = False
        self.lock = threading.Lock()
        self.packetizer = RTPPacketizer()
        self.session_id = str(random.randint(10000000, 99999999))

        self.width = 640
        self.height = 720
        self.vps = None
        self.sps = None
        self.pps = None
        self._last_frame_ts = 0          # for monotonic timestamp enforcement

    def start(self):
        """Start the RTSP server"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', self.port))
        self.server_socket.listen(5)
        self.running = True

        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()

        print(f"[RTSP] Server started on rtsp://localhost:{self.port}/stream")

    def stop(self):
        """Stop the RTSP server"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        with self.lock:
            for client in self.clients:
                try:
                    client['sock'].close()
                    if client.get('rtp_sock'):
                        client['rtp_sock'].close()
                except:
                    pass
            self.clients.clear()

    def set_stream_params(self, vps: bytes, sps: bytes, pps: bytes, width: int, height: int):
        """Set stream parameters from first I-frame"""
        self.vps = vps
        self.sps = sps
        self.pps = pps
        self.width = width
        self.height = height

    def _classify_nals(self, nal_units: list) -> bool:
        """
        Cache VPS/SPS/PPS (types 32-34) from nal_units.
        Returns True if an IDR slice (types 19/20) is present.
        """
        has_idr = False
        for nal in nal_units:
            if len(nal) < 2:
                continue
            nal_type = (nal[0] >> 1) & 0x3F
            if nal_type == 32:
                self.vps = nal
            elif nal_type == 33:
                self.sps = nal
            elif nal_type == 34:
                self.pps = nal
            elif nal_type in (19, 20):
                has_idr = True
        return has_idr

    # Minimum RTP timestamp increment between frames.
    # 90000 / 25 = 3600 ticks ≈ 25 fps.  This prevents burst-delivered frames
    # (multiple frames returned by one recv() call) from receiving timestamps
    # that are only 1 tick apart, which would imply an insane frame rate to
    # the decoder.  Wall-clock values larger than this step are still used
    # when the actual inter-frame gap is realistic.
    _MIN_FRAME_TICKS = 3000   # ~30 fps lower-bound

    def _next_rtp_timestamp(self) -> int:
        """
        Return a 90 kHz RTP timestamp for the current frame.
        - Derived from the wall clock so long-term timing is accurate.
        - Enforces a minimum step of _MIN_FRAME_TICKS so burst-delivered
          frames are not given artificially close timestamps.
        - Always strictly greater than the last issued value.
        """
        wall_ts = int(time.monotonic() * 90000) & 0xFFFFFFFF
        diff = (wall_ts - self._last_frame_ts) & 0xFFFFFFFF
        if diff >= self._MIN_FRAME_TICKS and diff < 0x80000000:
            # Wall clock gave us a reasonable gap – use it.
            ts = wall_ts
        else:
            # Either a burst arrival (diff too small) or a 32-bit wrap:
            # advance by exactly the minimum step.
            ts = (self._last_frame_ts + self._MIN_FRAME_TICKS) & 0xFFFFFFFF
        self._last_frame_ts = ts
        return ts

    def send_frame(self, frame_data: bytes):
        """Send a decoded video frame to all connected clients."""
        au_nals = self._parse_nal_units(frame_data)
        if not au_nals:
            return

        # Cache param sets; detect IDR
        has_idr = self._classify_nals(au_nals)

        # Prepend cached param sets before IDR so the decoder always has them
        if has_idr and self.vps and self.sps and self.pps:
            filtered = [n for n in au_nals if (n[0] >> 1) & 0x3F not in (32, 33, 34)]
            au_nals = [self.vps, self.sps, self.pps] + filtered

        # One unique, strictly-monotonic timestamp per send_frame call
        self.packetizer.timestamp = self._next_rtp_timestamp()

        if not self.clients:
            return

        with self.lock:
            dead_clients = []

            for i, client in enumerate(self.clients):
                try:
                    for j, nal in enumerate(au_nals):
                        is_last = (j == len(au_nals) - 1)
                        packets = self.packetizer.packetize_nal(nal, is_last)
                        for packet in packets:
                            if client['tcp']:
                                # RFC 2326 §10.12 interleaved binary data:
                                # $ + 1-byte channel + 2-byte big-endian length + RTP data
                                interleave = struct.pack('>BBH', 0x24, client['interleaved_channel'], len(packet))
                                client['sock'].sendall(interleave + packet)
                            else:
                                client['rtp_sock'].sendto(packet, (client['addr'][0], client['rtp_port']))
                except Exception:
                    dead_clients.append(i)

            for i in reversed(dead_clients):
                try:
                    self.clients[i]['sock'].close()
                    if self.clients[i].get('rtp_sock'):
                        self.clients[i]['rtp_sock'].close()
                except:
                    pass
                self.clients.pop(i)


    def _parse_nal_units(self, data: bytes) -> list:
        """Parse NAL units from Annex B format"""
        nal_units = []
        i = 0

        while i < len(data) - 4:
            if data[i:i+4] == b'\x00\x00\x00\x01':
                start = i + 4
                i += 4
            elif data[i:i+3] == b'\x00\x00\x01':
                start = i + 3
                i += 3
            else:
                i += 1
                continue

            end = len(data)
            j = i
            while j < len(data) - 3:
                if data[j:j+4] == b'\x00\x00\x00\x01' or data[j:j+3] == b'\x00\x00\x01':
                    end = j
                    break
                j += 1

            if start < end:
                nal_units.append(data[start:end])
            i = end

        return nal_units

    def _accept_loop(self):
        """Accept incoming RTSP connections"""
        while self.running:
            try:
                self.server_socket.settimeout(1.0)
                client_sock, client_addr = self.server_socket.accept()
                print(f"[RTSP] Client connected from {client_addr}")

                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock, client_addr),
                    daemon=True
                )
                thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[RTSP] Accept error: {e}")
                break

    def _handle_client(self, client_sock: socket.socket, client_addr: tuple):
        """Handle RTSP client requests"""
        rtp_sock = None
        rtp_port = None
        use_tcp = False
        interleaved_channel = 0
        playing = False

        try:
            while self.running:
                if playing:
                    # After PLAY, the connection stays open indefinitely so the
                    # client can send TEARDOWN.  Use a long timeout and treat a
                    # timeout as "still alive" — only a real recv error or an
                    # empty read (connection closed) should end the session.
                    client_sock.settimeout(60.0)
                    try:
                        data = client_sock.recv(4096)
                    except socket.timeout:
                        continue   # client is alive, just not sending anything
                    if not data:
                        break
                else:
                    client_sock.settimeout(30.0)
                    data = client_sock.recv(4096)
                    if not data:
                        break

                request = data.decode('utf-8', errors='ignore')
                lines = request.split('\r\n')
                if not lines:
                    continue

                parts = lines[0].split(' ')
                if len(parts) < 2:
                    continue

                method = parts[0]
                cseq = self._get_header(lines, 'CSeq') or '0'

                if method == 'OPTIONS':
                    response = self._make_response(200, cseq, {
                        'Public': 'OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN'
                    })
                    client_sock.send(response.encode())

                elif method == 'DESCRIBE':
                    sdp = self._generate_sdp()
                    response = self._make_response(200, cseq, {
                        'Content-Type': 'application/sdp',
                        'Content-Length': str(len(sdp))
                    }, sdp)
                    client_sock.send(response.encode())

                elif method == 'SETUP':
                    transport_hdr = self._get_header(lines, 'Transport') or ''

                    # Detect TCP interleaved vs UDP — must echo back what client requested
                    if 'RTP/AVP/TCP' in transport_hdr or 'interleaved' in transport_hdr:
                        # TCP interleaved mode (ffplay/mpv default when forced with -rtsp_transport tcp)
                        use_tcp = True
                        interleaved_channel = 0
                        for part in transport_hdr.split(';'):
                            if part.startswith('interleaved='):
                                try:
                                    interleaved_channel = int(part.split('=')[1].split('-')[0])
                                except ValueError:
                                    pass
                        transport_reply = f'RTP/AVP/TCP;unicast;interleaved={interleaved_channel}-{interleaved_channel+1}'
                    else:
                        # UDP mode — must bind the socket first to get a real OS-assigned port
                        use_tcp = False
                        rtp_port = 0
                        for part in transport_hdr.split(';'):
                            if part.startswith('client_port='):
                                try:
                                    rtp_port = int(part.split('=')[1].split('-')[0])
                                except ValueError:
                                    pass

                        if rtp_port == 0:
                            rtp_port = 5000

                        rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        rtp_sock.bind(('0.0.0.0', 0))           # bind to get a real port
                        server_rtp_port = rtp_sock.getsockname()[1]  # now non-zero
                        transport_reply = (
                            f'RTP/AVP;unicast;client_port={rtp_port}-{rtp_port+1}'
                            f';server_port={server_rtp_port}-{server_rtp_port+1}'
                        )

                    response = self._make_response(200, cseq, {
                        'Transport': transport_reply,
                        'Session': self.session_id
                    })
                    client_sock.send(response.encode())

                elif method == 'PLAY':
                    response = self._make_response(200, cseq, {
                        'Session': self.session_id,
                        'Range': 'npt=0.000-'
                    })
                    client_sock.send(response.encode())

                    with self.lock:
                        self.clients.append({
                            'sock': client_sock,
                            'rtp_sock': rtp_sock if not use_tcp else None,
                            'addr': client_addr,
                            'rtp_port': rtp_port,
                            'tcp': use_tcp,
                            'interleaved_channel': interleaved_channel,
                        })
                    mode = f"TCP interleaved ch={interleaved_channel}" if use_tcp else f"UDP port={rtp_port}"
                    print(f"[RTSP] Streaming to {client_addr[0]} via {mode}")
                    playing = True

                elif method == 'TEARDOWN':
                    response = self._make_response(200, cseq, {
                        'Session': self.session_id
                    })
                    client_sock.send(response.encode())
                    break

        except Exception as e:
            print(f"[RTSP] Client error: {e}")
        finally:
            with self.lock:
                self.clients = [c for c in self.clients if c['sock'] != client_sock]
            try:
                client_sock.close()
                if rtp_sock:
                    rtp_sock.close()
            except:
                pass
            print(f"[RTSP] Client disconnected: {client_addr}")

    def _get_header(self, lines: list, name: str) -> Optional[str]:
        """Get header value from request lines"""
        for line in lines:
            if line.lower().startswith(name.lower() + ':'):
                return line.split(':', 1)[1].strip()
        return None

    def _make_response(self, code: int, cseq: str, headers: dict = None, body: str = '') -> str:
        """Create RTSP response"""
        status = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 500: 'Internal Server Error'}
        response = f'RTSP/1.0 {code} {status.get(code, "Unknown")}\r\n'
        response += f'CSeq: {cseq}\r\n'

        if headers:
            for key, value in headers.items():
                response += f'{key}: {value}\r\n'

        response += '\r\n'
        if body:
            response += body

        return response

    def _generate_sdp(self) -> str:
        """Generate SDP for the stream"""
        import base64

        vps_b64 = base64.b64encode(self.vps).decode() if self.vps else ''
        sps_b64 = base64.b64encode(self.sps).decode() if self.sps else ''
        pps_b64 = base64.b64encode(self.pps).decode() if self.pps else ''

        sprop = ''
        if vps_b64 and sps_b64 and pps_b64:
            sprop = f';sprop-vps={vps_b64};sprop-sps={sps_b64};sprop-pps={pps_b64}'

        sdp = f'''v=0
o=- {int(time.time())} 1 IN IP4 127.0.0.1
s=V380 Camera Stream
t=0 0
m=video 0 RTP/AVP 96
c=IN IP4 0.0.0.0
a=rtpmap:96 H265/90000
a=fmtp:96 profile-id=1{sprop}
a=control:streamid=0
'''
        return sdp


def create_rtsp_server(port: int = 8554) -> RTSPServer:
    """Create and return an RTSP server instance"""
    return RTSPServer(port)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='RTSP Server for V380 streams')
    parser.add_argument('--port', type=int, default=8554, help='RTSP port (default: 8554)')
    args = parser.parse_args()

    server = RTSPServer(args.port)
    server.start()

    print(f"RTSP server running. Connect with:")
    print(f"  vlc  rtsp://localhost:{args.port}/stream")
    print(f"  mpv  rtsp://localhost:{args.port}/stream")
    print(f"  ffplay rtsp://localhost:{args.port}/stream")
    print("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.stop()
