"""
Configuration constants and settings for the giveaway bot.
"""
from dataclasses import dataclass
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()

# Emoji constants
REACTION_EMOJI = "<:sukoon_taaada:1324071825910792223>"
DOT_EMOJI = "<:sukoon_blackdot:1322894649488314378>"
RED_DOT_EMOJI = "<:sukoon_redpoint:1322894737736339459>"

# Color constants
EMBED_COLOR = 0x2f3136

# Timing constants
CLEANUP_INTERVAL = 5  # seconds
ENTRIES_PER_PAGE = 20  # Number of participants to show per page

# Giveaway limits
MIN_GIVEAWAY_DURATION = 30  # seconds
MAX_GIVEAWAY_DURATION = 31536000  # 365 days in seconds (1 year) - effectively no limit
MIN_WINNERS = 1
MAX_WINNERS = 20

# Fake reactions limits
MIN_FAKE_REACTIONS = 1
MAX_FAKE_REACTIONS = 1000
MIN_FAKE_DURATION = 1  # minutes
MAX_FAKE_DURATION = 10080  # 7 days in minutes

@dataclass
class GiveawayConfig:
    """Configuration for giveaway bot behavior."""
    
    # Duration limits
    min_duration: int = MIN_GIVEAWAY_DURATION
    max_duration: int = MAX_GIVEAWAY_DURATION
    
    # Winner limits
    min_winners: int = MIN_WINNERS
    max_winners: int = MAX_WINNERS
    
    # Fake reaction limits
    max_fake_reactions: int = MAX_FAKE_REACTIONS
    fake_reaction_max_duration: int = MAX_FAKE_DURATION
    
    # Display settings
    entries_per_page: int = ENTRIES_PER_PAGE
    
    # Feature flags
    enable_dm_notifications: bool = True
    enable_winner_verification: bool = True
    enable_statistics: bool = True
    
    # Database settings
    db_path: Optional[str] = None
    
    @classmethod
    def from_env(cls) -> 'GiveawayConfig':
        """Load configuration from environment variables."""
        return cls(
            min_duration=int(os.getenv('GIVEAWAY_MIN_DURATION', MIN_GIVEAWAY_DURATION)),
            max_duration=int(os.getenv('GIVEAWAY_MAX_DURATION', MAX_GIVEAWAY_DURATION)),
            min_winners=int(os.getenv('GIVEAWAY_MIN_WINNERS', MIN_WINNERS)),
            max_winners=int(os.getenv('GIVEAWAY_MAX_WINNERS', MAX_WINNERS)),
            max_fake_reactions=int(os.getenv('MAX_FAKE_REACTIONS', MAX_FAKE_REACTIONS)),
            fake_reaction_max_duration=int(os.getenv('MAX_FAKE_DURATION', MAX_FAKE_DURATION)),
            entries_per_page=int(os.getenv('ENTRIES_PER_PAGE', ENTRIES_PER_PAGE)),
            enable_dm_notifications=os.getenv('ENABLE_DM_NOTIFICATIONS', 'true').lower() == 'true',
            enable_winner_verification=os.getenv('ENABLE_WINNER_VERIFICATION', 'true').lower() == 'true',
            enable_statistics=os.getenv('ENABLE_STATISTICS', 'true').lower() == 'true',
            db_path=os.getenv('GIVEAWAY_DB_PATH')
        )

# Duration parsing units
DURATION_UNITS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
    'w': 604800  # week support
}
