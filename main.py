import discord
from discord.ext import commands
import logging
import os
from dotenv import load_dotenv
import asyncio
import aiohttp
import sys
import signal
from typing import Optional, List, Set
from logging.handlers import TimedRotatingFileHandler
from pyfiglet import Figlet
from discord import HTTPException
import time
from logging import StreamHandler

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Directory constants
LOGS_DIR = "logs"
DATABASE_DIR = "database"
COGS_DIR = "cogs"

def setup_directories() -> None:
    for directory in (LOGS_DIR, DATABASE_DIR, COGS_DIR):
        os.makedirs(directory, exist_ok=True)

def setup_logging() -> None:
    """Configure logging with file rotation - errors only."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.ERROR)

    # File handler with rotation - errors only
    file_handler = TimedRotatingFileHandler(
        os.path.join(LOGS_DIR, "bot.log"),
        when="midnight",
        backupCount=7,
        encoding='utf-8',
        utc=True
    )
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    logger.addHandler(file_handler)

def validate_environment() -> None:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

def print_banner(bot_name: str = "Discord Bot") -> None:
    f = Figlet(font='slant')
    banner = f.renderText(bot_name)
    print("\033[36m" + banner + "\033[0m")
    print("\033[33m" + "=" * 50 + "\033[0m")
    print("\033[32m" + "Bot is starting up..." + "\033[0m")
    print("\033[33m" + "=" * 50 + "\033[0m\n")

class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True

        super().__init__(command_prefix=".", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self._ready_once = False
        self._synced_commands: List[discord.app_commands.Command] = []
        self._shutdown_requested = False
        self._cleanup_task: Optional[asyncio.Task] = None
        
        # Add logger for cogs to use
        self.logger = logging.getLogger('discord.bot')
        
        # Global cooldown mapping
        self._cd_mapping = commands.CooldownMapping.from_cooldown(1, 0.2, commands.BucketType.user)
        
        # Response tracking to prevent duplicates
        self._response_tracker: Set[str] = set()
        self._tracker_cleanup_time = time.time()

        # Prefix command
        @self.command()
        async def ping(ctx):
            await ctx.send(f'<a:sukoon_greendot:1322894177775783997> Latency: {self.latency*1000:.2f}ms')

    async def _should_respond(self, ctx) -> bool:
        """Check if bot should respond to prevent duplicates"""
        response_id = f"{ctx.channel.id}:{ctx.message.id}:{ctx.command.name}"
        
        # Clean up old entries every 5 minutes
        current_time = time.time()
        if current_time - self._tracker_cleanup_time > 300:
            self._response_tracker.clear()
            self._tracker_cleanup_time = current_time
        
        if response_id in self._response_tracker:
            return False
            
        self._response_tracker.add(response_id)
        return True

    async def invoke(self, ctx):
        """Override invoke to add duplicate prevention for all commands"""
        if await self._should_respond(ctx):
            await super().invoke(ctx)

    async def on_command_error(self, ctx, error):
        """Handle command errors"""
        if isinstance(error, (commands.CommandOnCooldown, commands.CheckFailure)):
            return
        else:
            logging.error(f"Command error in {ctx.command}: {error}")

    async def process_commands(self, message):
        """Process commands with global cooldown"""
        if message.author.bot:
            return
            
        ctx = await self.get_context(message)
        if ctx.command is None:
            return
            
        # Check global cooldown
        bucket = self._cd_mapping.get_bucket(message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            return
            
        await self.invoke(ctx)

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        await self.load_cogs()
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        await asyncio.sleep(1)

        # Sync slash commands
        backoff = 1
        max_retries = 3
        retries = 0
        
        while retries < max_retries:
            try:
                self._synced_commands = await self.tree.sync()
                break
            except HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    logging.error(f"Failed to sync slash commands: {e}")
                    retries += 1
                    if retries >= max_retries:
                        break
                    await asyncio.sleep(backoff)

    async def _periodic_cleanup(self) -> None:
        """Clean up resources periodically"""
        while not self.is_closed():
            try:
                await asyncio.sleep(300)
                import gc
                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in periodic cleanup: {e}")

    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True

        print("\033[2J\033[H")
        print_banner(self.user.name)
        print(f"\033[32mLogged in as {self.user.name} ({self.user.id})\033[0m")
        print(f"\033[33mSynced {len(self._synced_commands)} slash commands\033[0m")
        
        # Display synced slash command names
        if self._synced_commands:
            print("\033[36m" + "=" * 50 + "\033[0m")
            print("\033[36mSynced Slash Commands:\033[0m")
            for cmd in self._synced_commands:
                print(f"  \033[32m/{cmd.name}\033[0m - {cmd.description}")
            print("\033[36m" + "=" * 50 + "\033[0m\n")
        else:
            print()

    async def close(self) -> None:
        if self.is_closed():
            return
            
        print("\n\033[33m" + "=" * 50 + "\033[0m")
        print("\033[31mBot is shutting down...\033[0m")
        print("\033[33m" + "=" * 50 + "\033[0m\n")

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self.session and not self.session.closed:
            await self.session.close()

        await super().close()

    async def load_cogs(self) -> None:
        """Load all cogs from the cogs directory and subdirectories."""
        if not os.path.isdir(COGS_DIR):
            return

        # Files to skip (not cogs)
        skip_files = {'config.py', '__init__.py'}
        
        # Walk through cogs directory and subdirectories
        for root, dirs, files in os.walk(COGS_DIR):
            # Get relative path from cogs directory
            rel_path = os.path.relpath(root, COGS_DIR)
            
            for filename in files:
                # Skip non-Python files and special files
                if not filename.endswith('.py') or filename in skip_files:
                    continue
                
                # Build module path
                if rel_path == '.':
                    # File is directly in cogs folder
                    module = f"{COGS_DIR}.{filename[:-3]}"
                else:
                    # File is in a subfolder
                    rel_module = rel_path.replace(os.sep, '.')
                    module = f"{COGS_DIR}.{rel_module}.{filename[:-3]}"
                
                try:
                    await self.load_extension(module)
                except Exception as e:
                    logging.error(f"Failed to load cog {module}: {e}")

    async def send_error_report(self, error_message: str) -> None:
        if not self.session or self.session.closed or self.is_closed():
            return
        try:
            async with self.session.post(WEBHOOK_URL, json={"content": error_message}) as resp:
                resp.raise_for_status()
        except Exception as e:
            logging.error(f"Failed to send error report: {e}")

def setup_signal_handlers(bot: DiscordBot) -> None:
    def shutdown_handler(signum=None, frame=None):
        bot._shutdown_requested = True
        if not bot.is_closed():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(bot.close())
            except RuntimeError:
                pass
    
    if sys.platform != "win32":
        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGTERM, shutdown_handler)
            loop.add_signal_handler(signal.SIGINT, shutdown_handler)
        except NotImplementedError:
            signal.signal(signal.SIGTERM, shutdown_handler)
            signal.signal(signal.SIGINT, shutdown_handler)
    else:
        signal.signal(signal.SIGINT, shutdown_handler)

async def main():
    bot = None
    try:
        setup_directories()
        setup_logging()
        validate_environment()
    except ValueError as e:
        print(f"\033[31mStartup error: {e}\033[0m")
        sys.exit(1)

    try:
        bot = DiscordBot()
        setup_signal_handlers(bot)

        async with bot:
            async def shutdown_checker():
                while not bot.is_closed():
                    if bot._shutdown_requested:
                        await bot.close()
                        break
                    await asyncio.sleep(1)
            
            await asyncio.gather(
                bot.start(DISCORD_TOKEN),
                shutdown_checker(),
                return_exceptions=True
            )
            
    except KeyboardInterrupt:
        if bot and not bot.is_closed():
            await bot.close()
    except Exception as e:
        logging.error(f"Fatal error in main: {e}")
        if bot and bot.session and not bot.session.closed and not bot.is_closed():
            try:
                await bot.send_error_report(f"Fatal error: {e}")
            except:
                pass
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\033[31mBot shutdown by keyboard interrupt\033[0m")
    except Exception as e:
        print(f"\n\033[31mFatal error during startup: {e}\033[0m")
        logging.error(f"Fatal error during startup: {e}")
        sys.exit(1)