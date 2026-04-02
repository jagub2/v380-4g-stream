"""V380 4G Camera Tools"""

from .client import V380Client, discover_stream_server
from .crypto import generate_aes_key, encrypt_password
from .alarm_recorder import AlarmRecorder
from .triggered_recorder import AlarmTriggeredRecorder

__version__ = "1.2.0"
__all__ = [
    "V380Client", "discover_stream_server",
    "generate_aes_key", "encrypt_password",
    "AlarmRecorder", "AlarmTriggeredRecorder",
]
