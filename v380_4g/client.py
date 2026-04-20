"""
V380 Cloud Client

Core client for connecting to V380 cloud servers.
Handles authentication, session management, and JSON-RPC communication.
"""

import socket
import struct
import json
import hashlib
import secrets
import time
import urllib.request
from typing import Optional
from Crypto.Cipher import AES

from .crypto import generate_aes_key, generate_random_key, encrypt_password

# Default server configuration
DEFAULT_API_SERVER = "194.195.251.29"
DEFAULT_API_PORT = 8089
DEFAULT_REGISTER_PORT = 8900
DEFAULT_STREAM_PORT = 8800

# HTTP dispatch endpoint — returns streaming relay IPs ordered by proximity.
# Confirmed from decompiled DispatchUtils.java.
_DISPATCH_URL = "http://dispatch.av380.net:8001/api/v1/get_stream_server"
_DISPATCH_SALT = "hsdata2022"


def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def discover_stream_server(device_id: int, timeout: int = 5) -> Optional[str]:
    """
    Query the V380 HTTP dispatch server to find the closest streaming relay IP.

    Posts to dispatch.av380.net:8001/api/v1/get_stream_server signed with
    SHA-1 + hsdata2022 salt (from decompiled DispatchUtils.java).
    Returns the first relay domain/IP, or None on failure.

    Usage:
        server = discover_stream_server(device_id) or DEFAULT_API_SERVER
        client = V380Client(device_id, password, server=server)
    """
    ts = int(time.time())
    platform = 10001
    canonical = f"dev_id={device_id}&platform={platform}&timestamp={ts}{_DISPATCH_SALT}"
    payload = json.dumps({
        "dev_id":    device_id,
        "platform":  platform,
        "timestamp": ts,
        "sign":      _sha1_hex(canonical),
    }, separators=(",", ":")).encode()

    try:
        req = urllib.request.Request(
            _DISPATCH_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
        data = json.loads(body)
        if data.get("code") != 2000:
            return None
        entries = data.get("data") or []
        if not entries:
            return None
        first = entries[0]
        return first.get("domain") or first.get("ip") or None
    except Exception:
        return None


class V380Client:
    """
    V380 Cloud Camera Client

    Handles connection to V380 cloud servers and authentication.
    Can be extended for streaming, playback, and control features.
    """

    def __init__(self, device_id: int, password: str,
                 server: str = DEFAULT_API_SERVER,
                 api_port: int = DEFAULT_API_PORT,
                 register_port: int = DEFAULT_REGISTER_PORT,
                 stream_port: int = DEFAULT_STREAM_PORT,
                 hd: bool = False,
                 debug: bool = False):
        self.device_id     = device_id
        self.password      = password
        self.server        = server
        self.api_port      = api_port
        self.register_port = register_port
        self.stream_port   = stream_port
        self.hd            = hd
        self.debug         = debug

        # Session state
        self.session: Optional[int] = None
        self.handle: Optional[int] = None
        self.aes_key: Optional[bytes] = None
        self.cipher: Optional[AES] = None
        self.socket: Optional[socket.socket] = None

        # Camera capabilities
        self.audio_supported = False
        self.battery_level: Optional[int] = None

    @property
    def domain(self) -> str:
        """Camera domain name"""
        return f"{self.device_id}.nvdvr.net"

    @property
    def is_connected(self) -> bool:
        """Check if connected and authenticated"""
        return self.session is not None and self.handle is not None

    def connect(self) -> bool:
        """Connect to API server"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(15)
            self.socket.connect((self.server, self.api_port))
            print(f"[+] Connected to API server {self.server}:{self.api_port}")
            return True
        except Exception as e:
            print(f"[!] Connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from server"""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        self.session = None
        self.handle = None

    def register(self) -> bool:
        """
        Register device with cloud routing server.

        This prepares the cloud infrastructure to route streams for this device.
        Should be called before connect()/login(), especially for idle 4G cameras.
        """
        reg_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reg_sock.settimeout(10)

        try:
            reg_sock.connect((self.server, self.register_port))
            print(f"[+] Connected to register server {self.server}:{self.register_port}")

            domain = self.domain.encode().ljust(48, b'\x00')
            packet = struct.pack('<II', 0x00ac, 0x03f4) + domain
            packet += struct.pack('<I', self.stream_port)
            packet += struct.pack('<I', self.device_id)
            packet = packet.ljust(64, b'\x00')

            reg_sock.sendall(packet)
            print("[*] Sent register request...")

            response = reg_sock.recv(256)
            if len(response) >= 8:
                status = struct.unpack('<I', response[4:8])[0]
                if status == 1:
                    print("[+] Registration successful")
                    return True
                else:
                    print(f"[!] Registration failed (status={status})")
                    return False

        except Exception as e:
            print(f"[!] Registration error: {e}")
            return False
        finally:
            reg_sock.close()

        return False

    def login(self) -> bool:
        """Login to V380 cloud server"""
        if not self.socket:
            if not self.connect():
                return False

        random_key = generate_random_key()
        encrypted_pass = encrypt_password(self.password, random_key)

        # accountId and connectType: these are cloud API fields that do NOT
        # affect stream quality. Quality is set at the stream protocol level
        # in create_stream_socket() via the quality byte in the 0x12d packet.
        params = {
            "version": 31,
            "phoneType": 1012,
            "deviceId": self.device_id,
            "domain": self.domain,
            "port": self.stream_port,
            "accountId": 11,
            "username": str(self.device_id),
            "password": encrypted_pass,
            "randomKey": random_key,
            "connectType": 0,
            "securityLevel": 1,
            "agora": 0,
            "ectx": int(time.time()),
            "p2pIdx": 0
        }

        print("[*] Sending login request...")
        response = self._send_json_rpc("login", params)

        if response and 'v380' in response:
            v380 = response['v380']
            self.session = v380.get('session')
            self.handle = v380.get('handle')

            self.aes_key = generate_aes_key(self.handle)
            self.cipher = AES.new(self.aes_key, AES.MODE_ECB)

            print(f"[+] Login successful!")
            print(f"    Session: {self.session}")
            print(f"    Handle: {self.handle}")
            print(f"    Quality: {'HD' if self.hd else 'SD'}")
            if self.debug:
                print(f"    AES Key: {self.aes_key.hex()}")

            if 'pri' in v380:
                pri = v380['pri']
                self.battery_level = pri.get('battery')
                self.audio_supported = pri.get('audio', 0) == 1
                print(f"    Battery: {self.battery_level}%")
                print(f"    Audio: {'supported' if self.audio_supported else 'not supported'}")

            return True

        print("[!] Login failed")
        if response and self.debug:
            print(f"    Response: {json.dumps(response, indent=2)}")
        return False

    def set_handle(self, handle: int):
        """Override encryption handle (for cameras with fixed handles)"""
        self.handle = handle
        self.aes_key = generate_aes_key(handle)
        self.cipher = AES.new(self.aes_key, AES.MODE_ECB)
        if self.debug:
            print(f"[*] Handle override: {handle}")
            print(f"    AES Key: {self.aes_key.hex()}")

    def _send_json_rpc(self, method: str, params: dict) -> dict:
        """Send JSON-RPC request to server"""
        payload = {
            "id": secrets.randbelow(100000000),
            "method": method,
            "params": params
        }

        json_bytes = json.dumps(payload, separators=(',', ':')).encode()
        length = len(json_bytes)
        packet = bytes([0x00, 0x03, 0x00, 0xfe]) + struct.pack('<H', length) + bytes([0x00, 0x00]) + json_bytes

        self.socket.sendall(packet)
        response = self.socket.recv(8192)

        try:
            text = response.decode('utf-8', errors='ignore')
            start = text.find('{')
            if start >= 0:
                depth = 0
                for i, c in enumerate(text[start:]):
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            result = json.loads(text[start:start+i+1])
                            if 'result' in result and 'code' in result.get('result', {}):
                                code = result['result']['code']
                                if code != 0:
                                    print(f"[!] Server error: {json.dumps(result['result'], indent=2)}")
                            return result
        except Exception as e:
            if self.debug:
                print(f"[!] Parse error: {e}")
        return {}

    def create_stream_socket(self) -> Optional[socket.socket]:
        """Create and authenticate a stream socket"""
        if not self.is_connected:
            print("[!] Not logged in")
            return None

        stream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stream_sock.settimeout(30)

        try:
            stream_sock.connect((self.server, self.stream_port))
            print(f"[+] Connected to stream port {self.stream_port}")

            # Build stream request (0x12d) packet.
            # Confirmed from Wireshark captures of the Android app (SD vs HD):
            #   byte 74 (uint32 LE): 0 = SD sub-stream, 1 = HD main stream
            #   byte 84 (uint32 LE): 0x00000000 = SD, 0x00010000 = HD
            # Both must be set together for HD to take effect.
            quality      = 1 if self.hd else 0
            quality_hi   = 0x00010000 if self.hd else 0x00000000

            domain = self.domain.encode().ljust(48, b'\x00')
            packet = struct.pack('<II', 0x012d, 0x03ea) + domain
            packet += struct.pack('<HHH', 0x0000, 0x13ba, 0x0000)
            packet += struct.pack('<III', self.device_id, self.handle, self.session)
            # Quality fields at bytes 74 and 82 (verified from capture)
            packet += struct.pack('<I', quality)        # [74:78] HD flag
            packet += struct.pack('<I', 0x00100015)     # [78:82] constant observed in capture
            packet += struct.pack('<I', quality_hi)     # [82:86] HD resolution flag
            packet += struct.pack('<I', 0x01010000)     # [86:90] constant observed in capture
            packet += struct.pack('<I', 0x00000001)     # [90:94] constant observed in capture
            packet = packet.ljust(256, b'\x00')
            stream_sock.sendall(packet)

            # Check response
            response = stream_sock.recv(256)
            if len(response) < 12 or response[:2] != b'\x91\x01':
                print(f"[!] Unexpected stream response")
                stream_sock.close()
                return None

            status = struct.unpack('<i', response[8:12])[0]
            if status != 4:
                print(f"[!] Stream authentication failed (status={status})")
                stream_sock.close()
                return None

            print("[+] Stream handshake successful")

            # Send init packet
            init_packet = bytes.fromhex("2f010000013000000000000000000000")
            init_packet += b'\x00' * (256 - len(init_packet))
            stream_sock.sendall(init_packet)

            # Send keepalive
            keepalive = bytes.fromhex("01210000000000000010000000000000")
            stream_sock.sendall(keepalive)

            return stream_sock

        except Exception as e:
            print(f"[!] Stream connection error: {e}")
            stream_sock.close()
            return None
