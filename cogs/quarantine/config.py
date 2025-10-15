"""
Configuration for Quarantine and Appeal System
"""

# Appeal System Configuration
APPEAL_COOLDOWN_HOURS = 24  # Hours between appeal submissions
MAX_APPEAL_LENGTH = 1000  # Maximum characters in appeal message
APPEAL_REVIEW_TIMEOUT_DAYS = 7  # Days before appeal auto-expires

# Appeal Status
class AppealStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"

# Appeal Types
class AppealType:
    MUTE = "mute"
    BAN = "ban"
    KICK = "kick"

# Embed Colors
class Colors:
    SUCCESS = 0x51CF66
    ERROR = 0xFF6B6B
    WARNING = 0xFFD93D
    INFO = 0x9775FA
    PENDING = 0x74C0FC
