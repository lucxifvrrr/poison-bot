<div align="center">

![Banner](banner.gif)

# 🤖 Discord Bot - Feature-Rich Server Management

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Discord.py](https://img.shields.io/badge/discord.py-2.0+-blue.svg)](https://github.com/Rapptz/discord.py)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/yourusername/yourrepo/graphs/commit-activity)

*A powerful, modular Discord bot with extensive server management and engagement features*

[Features](#-features) • [Installation](#-installation) • [Configuration](#-configuration) • [Commands](#-commands) • [Contributing](#-contributing)

</div>

---

## 📋 Overview

This is a comprehensive Discord bot built with discord.py, featuring a modular cog-based architecture for easy maintenance and scalability. The bot includes advanced features for server management, user engagement, moderation, and entertainment.

## ✨ Features

### 🛡️ Moderation & Management
- **Verification System** - Secure member verification with customizable workflows
- **Auto Moderation** - Automated content filtering and rule enforcement
- **Purge Commands** - Bulk message deletion with advanced filters
- **Ban Management** - Enhanced ban/unban functionality with logging

### 👥 User Engagement
- **AFK System** - Automatic AFK status tracking with custom messages
- **Confession System** - Anonymous confession channel management
- **Auto Responder** - Custom automated responses to keywords
- **Sticky Messages** - Pin important messages that stay visible
- **Snipe Commands** - Recover recently deleted/edited messages

### 🎮 Entertainment & Games
- **Match Making** - Advanced matchmaking system for gaming
- **Giveaways** - Feature-rich giveaway management
- **Drops** - Random reward drops for active members
- **Bulk Ping** - Mass mention system with cooldowns

### 🎤 Voice Channel Features
- **VC Manager** - Dynamic voice channel creation and management
- **VC Roles** - Automatic role assignment based on VC activity
- **Always VC** - Persistent voice channel monitoring
- **Drag Me** - Voice channel movement commands

### 🎨 Utility & Customization
- **Avatar Commands** - Display and manipulate user avatars
- **Status Changer** - Dynamic bot status rotation
- **Translation** - Multi-language translation support
- **Media Commands** - Image and media manipulation
- **Thread Management** - Advanced thread creation and control
- **Info Commands** - Server and user information display
- **Stats Tracking** - Comprehensive server statistics
- **Steal Emojis** - Copy emojis from other servers
- **Greeting System** - Welcome and goodbye messages
- **Request Role** - User-initiated role requests

## 🚀 Installation

### Prerequisites
- Python 3.8 or higher
- pip (Python package manager)
- A Discord Bot Token ([Get one here](https://discord.com/developers/applications))

### Step 1: Clone the Repository
```bash
git clone https://github.com/yourusername/yourrepo.git
cd yourrepo
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
Create a `.env` file in the root directory:
```env
DISCORD_TOKEN=your_discord_bot_token_here
WEBHOOK_URL=your_webhook_url_here
```

### Step 4: Run the Bot
```bash
python main.py
```

## ⚙️ Configuration

### Environment Variables
| Variable | Description | Required |
|----------|-------------|----------|
| `DISCORD_TOKEN` | Your Discord bot token | ✅ Yes |
| `WEBHOOK_URL` | Webhook URL for error reporting | ✅ Yes |

### Bot Intents
The bot requires the following intents:
- `members` - For member tracking and verification
- `presences` - For status monitoring
- `message_content` - For command processing

### Directory Structure
```
├── cogs/              # Bot command modules
│   ├── giveaways/    # Giveaway system
│   └── *.py          # Individual cog files
├── logs/              # Auto-generated log files
├── database/          # Database storage
├── main.py            # Bot entry point
├── requirements.txt   # Python dependencies
├── .env              # Environment variables (create this)
└── .gitignore        # Git ignore rules
```

## 📝 Commands

### Prefix Commands
- `.ping` - Check bot latency and response time

### Slash Commands
The bot includes numerous slash commands across all cogs. Use `/` in Discord to see all available commands with descriptions.

### Command Categories
- **Moderation**: Ban, kick, purge, verification
- **Utility**: Avatar, info, stats, translate
- **Fun**: Confess, drops, giveaways
- **Voice**: VC management, roles, drag
- **Engagement**: AFK, auto-responder, sticky messages

## 🏗️ Architecture

### Modular Cog System
The bot uses a modular architecture where each feature is implemented as a separate cog. This allows for:
- Easy feature addition/removal
- Independent testing and debugging
- Clean code organization
- Hot-reloading capabilities

### Key Features
- **Global Cooldown System** - Prevents command spam
- **Duplicate Response Prevention** - Avoids double responses
- **Automatic Error Reporting** - Webhook-based error notifications
- **Graceful Shutdown** - Proper cleanup on exit
- **Periodic Resource Cleanup** - Memory management
- **Rotating Log Files** - Automatic log rotation

## 🛠️ Development

### Adding New Cogs
1. Create a new Python file in the `cogs/` directory
2. Implement your cog class extending `commands.Cog`
3. Add the `setup()` function at the end
4. The bot will automatically load it on startup

Example:
```python
from discord.ext import commands

class MyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    async def mycommand(self, ctx):
        await ctx.send("Hello!")

async def setup(bot):
    await bot.add_cog(MyCog(bot))
```

### Logging
- Only errors are logged by default
- Logs rotate daily and keep 7 days of history
- Location: `logs/bot.log`

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📜 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🐛 Bug Reports

If you encounter any bugs or issues, please:
1. Check existing issues first
2. Create a new issue with detailed information
3. Include error logs if applicable

## 💡 Support

Need help? Here's how to get support:
- 📖 Check the documentation
- 🐛 Open an issue on GitHub
- 💬 Join our Discord server (if applicable)

## 🙏 Acknowledgments

- Built with [discord.py](https://github.com/Rapptz/discord.py)
- Uses [aiohttp](https://github.com/aio-libs/aiohttp) for async HTTP requests
- Powered by Python 3.8+

---

<div align="center">

**⭐ Star this repository if you find it helpful!**

Made with ❤️ by [Your Name]

</div>
