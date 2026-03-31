"""V380 4G Camera Tools"""

from .client import V380Client, discover_stream_server
from .crypto import generate_aes_key, encrypt_password
from .alarm_recorder import AlarmRecorder

__version__ = "1.1.0"
__all__ = ["V380Client", "discover_stream_server", "generate_aes_key",
           "encrypt_password", "AlarmRecorder"]
