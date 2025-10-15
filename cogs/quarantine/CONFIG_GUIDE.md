# Quarantine & Appeal System Configuration Guide

## Overview

All customizable settings for the quarantine and appeal systems are centralized in `config.py`. This allows you to easily customize emojis, colors, messages, and behavior without modifying the core code.

---

## Table of Contents

1. [Basic Settings](#basic-settings)
2. [Emojis](#emojis)
3. [Colors](#colors)
4. [Embed Templates](#embed-templates)
5. [Examples](#examples)

---

## Basic Settings

### Quarantine System

```python
# Role & Channel Names
MUTED_ROLE_NAME = "Muted"           # Name of the muted role
JAIL_CHANNEL_NAME = "jail"          # Name of the jail channel
LOG_CHANNEL_NAME = "punishment-logs" # Name of the log channel

# DM Configuration
DM_AUTO_DELETE_MINUTES = 10         # Minutes before DM auto-deletes
DM_REASON_MAX_LENGTH = 80           # Max characters for reason in DM

# Permission Update Configuration
PERMISSION_BASE_SLEEP = 0.3         # Base delay between permission updates
PERMISSION_LARGE_SERVER_SLEEP = 0.4 # Delay for 100-200 channel servers
PERMISSION_HUGE_SERVER_SLEEP = 0.5  # Delay for 200+ channel servers
PERMISSION_MAX_RETRIES = 3          # Max retries for failed updates
LARGE_SERVER_THRESHOLD = 100        # Channel count for "large" server
HUGE_SERVER_THRESHOLD = 200         # Channel count for "huge" server

# Jail Message
JAIL_WELCOME_MESSAGE = "You have been muted. Please wait for staff to review your case."
```

### Appeal System

```python
# Appeal Limits
APPEAL_COOLDOWN_HOURS = 24          # Hours between appeal submissions
MAX_APPEAL_LENGTH = 1000            # Maximum characters in appeal
MIN_APPEAL_LENGTH = 50              # Minimum characters in appeal
APPEAL_REVIEW_TIMEOUT_DAYS = 7      # Days before appeal auto-expires

# Appeal Modal Configuration
APPEAL_REASON_PLACEHOLDER = "Explain why you believe your punishment should be lifted..."
APPEAL_ADDITIONAL_INFO_PLACEHOLDER = "Any additional information (optional)..."
```

---

## Emojis

### Quarantine System Emojis

```python
class QuarantineEmojis:
    MUTED = "🔇"        # Mute action
    UNMUTED = "🔊"      # Unmute action
    JAIL = "🔒"         # Jail channel
    LOG = "📝"          # Log channel
    CASE = "📋"         # Case ID
    MODERATOR = "🛡️"    # Moderator
    MEMBER = "👤"       # Member
    REASON = "📋"       # Reason
    EXPIRES = "⏰"      # Expiration time
    DURATION = "⏰"     # Duration
    TIMESTAMP = "📅"    # Timestamp
    SUCCESS = "✅"      # Success
    ERROR = "❌"        # Error
    WARNING = "⚠️"      # Warning
    INFO = "ℹ️"         # Information
    SETUP = "⚙️"        # Setup
    STATS = "📊"        # Statistics
    TIP = "💡"          # Tip/hint
    CLEANUP = "🧹"      # Cleanup
    AUTO = "🤖"         # Automatic action
    ACTIVE = "🔴"       # Active status
    RESOLVED = "🟢"     # Resolved status
```

### Appeal System Emojis

```python
class AppealEmojis:
    SUBMIT = "📝"       # Submit appeal
    APPROVED = "✅"     # Appeal approved
    DENIED = "❌"       # Appeal denied
    PENDING = "🟡"      # Appeal pending
    EXPIRED = "⏰"      # Appeal expired
    REVIEW = "👁️"       # Review
    APPEAL = "📨"       # Appeal
    RESULT = "🎉"       # Result
    NOTE = "📋"         # Note
    SERVER = "🏢"       # Server
    REVIEWER = "🛡️"     # Reviewer
    USER = "👤"         # User
```

---

## Colors

```python
class Colors:
    SUCCESS = 0x51CF66  # Green - Success messages
    ERROR = 0xFF6B6B    # Red - Error messages
    WARNING = 0xFFD93D  # Yellow - Warning messages
    INFO = 0x9775FA     # Purple - Information
    PENDING = 0x74C0FC  # Blue - Pending status
    MUTE = 0xFF6B6B     # Red - Mute embeds
    UNMUTE = 0x51CF66   # Green - Unmute embeds
```

### Color Customization Examples

```python
# Pastel theme
class Colors:
    SUCCESS = 0xA8E6CF  # Pastel green
    ERROR = 0xFFB3BA    # Pastel red
    WARNING = 0xFFDFBA  # Pastel orange
    INFO = 0xBAE1FF     # Pastel blue
    PENDING = 0xE0BBE4  # Pastel purple
    MUTE = 0xFFB3BA     # Pastel red
    UNMUTE = 0xA8E6CF   # Pastel green

# Dark theme
class Colors:
    SUCCESS = 0x2ECC71  # Dark green
    ERROR = 0xE74C3C    # Dark red
    WARNING = 0xF39C12  # Dark orange
    INFO = 0x3498DB     # Dark blue
    PENDING = 0x9B59B6  # Dark purple
    MUTE = 0xE74C3C     # Dark red
    UNMUTE = 0x2ECC71   # Dark green

# Monochrome theme
class Colors:
    SUCCESS = 0x95A5A6  # Light gray
    ERROR = 0x2C3E50    # Dark gray
    WARNING = 0x7F8C8D  # Medium gray
    INFO = 0xBDC3C7     # Very light gray
    PENDING = 0x34495E  # Slate gray
    MUTE = 0x2C3E50     # Dark gray
    UNMUTE = 0x95A5A6   # Light gray
```

---

## Embed Templates

### Quarantine Embed Titles

```python
class QuarantineTitles:
    MUTE_LOG = "{emoji} Member Muted — Case #{case}"
    UNMUTE_LOG = "{emoji} Member Unmuted — Case #{case}"
    AUTO_UNMUTE = "{emoji} Auto-Unmute — Case #{case}"
    MUTE_SUCCESS = "{emoji} Member Muted Successfully"
    UNMUTE_SUCCESS = "{emoji} Member Unmuted Successfully"
    SETUP_COMPLETE = "{emoji} Mute System Setup Complete"
    CONFIG_CHECK_PASS = "{emoji} Configuration Check Passed"
    CONFIG_CHECK_FAIL = "{emoji} Configuration Issues Found"
    PERMISSIONS_REAPPLIED = "{emoji} Permissions Reapplied"
    MODROLE_UPDATED = "{emoji} Moderator Role Updated"
    CLEANUP_COMPLETE = "{emoji} Database Cleanup Complete"
    CASE_INFO = "{emoji} Case #{case} — {status}"
    ACTIVE_MUTES = "{emoji} Active Mutes"
    JAIL_HISTORY = "{emoji} Jail Message History"
```

### Appeal Embed Titles

```python
class AppealTitles:
    APPEAL_SUBMITTED = "{emoji} Appeal Submitted Successfully"
    APPEAL_APPROVED = "{emoji} Appeal Approved"
    APPEAL_DENIED = "{emoji} Appeal Denied"
    APPEAL_DETAILS = "{emoji} Appeal #{id} Details"
    APPEAL_LIST = "{emoji} Your Appeals"
    PENDING_APPEALS = "{emoji} Pending Appeals"
    APPEAL_REVIEW = "{emoji} Review Appeal #{id}"
```

### Embed Descriptions

```python
class EmbedDescriptions:
    # Quarantine
    MUTE_LOG = "**{member}** has been muted and moved to the quarantine zone."
    UNMUTE_LOG = "**{member}** has been unmuted and can now access the server."
    AUTO_UNMUTE = "Temporary mute has expired and been automatically removed."
    MUTE_SUCCESS = "{member} has been muted and moved to {jail}"
    UNMUTE_SUCCESS = "{member} has been unmuted and can now access the server."
    SETUP_COMPLETE = "The quarantine system has been successfully configured!"
    CONFIG_CHECK_PASS = "All basic configuration checks passed successfully!"
    CONFIG_CHECK_FAIL = "The following problems were detected with your mute system:"
    PERMISSIONS_REAPPLIED = "Muted role overwrites have been reapplied across all categories and channels."
    MODROLE_UPDATED = "The moderator role has been set to {role}"
    CLEANUP_COMPLETE = "Cleared old inactive mute records from the database."
    CASE_INFO = "Detailed information for mute case **#{case}**"
    
    # Appeal
    APPEAL_SUBMITTED = "Your appeal has been submitted and is pending review by moderators."
    APPEAL_APPROVED_DM = "Your punishment has been lifted"
    APPEAL_DENIED_DM = "Punishment remains in effect"
    APPEAL_DETAILS = "Status: {emoji} **{status}**"
    APPEAL_LIST = "Showing {count} appeal(s)"
    PENDING_APPEALS = "Showing {count} pending appeal(s)"
```

### Footer Messages

```python
class FooterMessages:
    MUTE_DM_SENT = "✅ DM sent to user"
    MUTE_DM_FAILED = "⚠️ Could not send DM to user"
    MUTE_SUCCESS_REMOVED = "✅ Mute successfully removed"
    SETUP_BY = "Setup by {user}"
    REQUESTED_BY = "Requested by {user}"
    MUTED_BY = "Muted by {user}"
    UNMUTED_BY = "Unmuted by {user}"
    CLEANUP_BY = "Cleanup by {user}"
    CONFIG_CHECK = "Please resolve these issues to ensure proper functionality"
    MUTE_READY = "Your mute system is ready to use!"
    AUTO_UNMUTE = "✅ Automatic unmute completed"
    APPEAL_SUBMITTED = "Moderators will review your appeal soon"
    USE_APPEAL_STATUS = "Use /appeal-status <appeal_id> for detailed information"
    USE_APPEAL_REVIEW = "Use /appeal-review <appeal_id> to review an appeal"
```

---

## Examples

### Example 1: Custom Emoji Theme

```python
# Use custom Discord emojis
class QuarantineEmojis:
    MUTED = "<:muted:123456789>"
    UNMUTED = "<:unmuted:123456789>"
    JAIL = "<:jail:123456789>"
    # ... etc
```

### Example 2: Multilingual Support

```python
# Spanish
class EmbedDescriptions:
    MUTE_LOG = "**{member}** ha sido silenciado y movido a la zona de cuarentena."
    UNMUTE_LOG = "**{member}** ha sido desilenciado y ahora puede acceder al servidor."
    # ... etc

# French
class EmbedDescriptions:
    MUTE_LOG = "**{member}** a été réduit au silence et déplacé vers la zone de quarantaine."
    UNMUTE_LOG = "**{member}** a été réactivé et peut maintenant accéder au serveur."
    # ... etc
```

### Example 3: Professional Server Theme

```python
# Formal language
class EmbedDescriptions:
    MUTE_LOG = "User **{member}** has been restricted from server access."
    UNMUTE_LOG = "User **{member}** access restrictions have been lifted."
    SETUP_COMPLETE = "Moderation system initialization complete."
    # ... etc

# Professional emojis
class QuarantineEmojis:
    MUTED = "🚫"
    UNMUTED = "🔓"
    MODERATOR = "👮"
    # ... etc
```

### Example 4: Gaming Server Theme

```python
# Gaming language
class EmbedDescriptions:
    MUTE_LOG = "**{member}** got sent to the penalty box! 🏒"
    UNMUTE_LOG = "**{member}** is back in the game! 🎮"
    SETUP_COMPLETE = "Moderation system is now online! GG! 🎮"
    # ... etc

# Gaming emojis
class QuarantineEmojis:
    MUTED = "🔇"
    UNMUTED = "🎮"
    JAIL = "🏒"
    SUCCESS = "🏆"
    # ... etc
```

---

## How to Apply Changes

1. **Edit `config.py`** with your desired values
2. **Restart the bot** for changes to take effect
3. **Test the changes** with a test mute/unmute

**Note:** Changes to `config.py` require a bot restart. The bot does not hot-reload configuration.

---

## Best Practices

1. **Backup First**: Always backup `config.py` before making changes
2. **Test Changes**: Test in a development server before production
3. **Consistent Theme**: Keep emojis and colors consistent across your server
4. **Accessibility**: Ensure colors have good contrast for readability
5. **Language**: Keep messages clear and professional
6. **Documentation**: Comment your custom changes for future reference

---

## Troubleshooting

### Colors not showing?
- Ensure hex values are in format `0xRRGGBB`
- Check that you're using the `Colors` class correctly

### Emojis not displaying?
- Verify custom emoji IDs are correct
- Ensure the bot has access to custom emojis
- Standard Unicode emojis should work everywhere

### Messages not updating?
- Restart the bot after config changes
- Check for syntax errors in `config.py`
- Verify you're editing the correct file

---

**Last Updated:** October 2025  
**Version:** 2.0
