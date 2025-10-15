import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import re
import random
from pymongo import MongoClient, ASCENDING
from pymongo.collection import ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing from environment (.env)")

# Connect to MongoDB with proper connection pooling
mongo = MongoClient(
    MONGO_URL,
    maxPoolSize=50,
    minPoolSize=10,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000
)
db = mongo.get_database("discord_mute_system")

guild_configs = db.guild_configs
mutes_col = db.mutes
jail_messages = db.jail_messages
guild_counters = db.guild_counters
pending_dm_deletes = db.pending_dm_deletes

# Ensure indexes (safe wrapped)
try:
    guild_configs.create_index([("guild_id", ASCENDING)], unique=True)
    mutes_col.create_index([("guild_id", ASCENDING), ("user_id", ASCENDING)])
    guild_counters.create_index([("guild_id", ASCENDING)], unique=True)
    # TTL on jail messages for 7 days
    jail_messages.create_index([("created_at", ASCENDING)], expireAfterSeconds=7 * 24 * 3600)
    # TTL for DM delete tasks: expire exactly at expires_at (expireAfterSeconds: 0)
    pending_dm_deletes.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
except Exception:
    # If index creation fails, continue; bot can still operate
    pass

# parse durations like 10m, 2h, 1d, 30s
DUR_RE = re.compile(r"^(\d+)([smhd])$")

def parse_duration(s: str) -> Optional[timedelta]:
    m = DUR_RE.match(s)
    if not m:
        return None
    v, u = m.groups()
    v = int(v)
    # Validate positive duration
    if v <= 0:
        return None
    # Validate reasonable limits (max 365 days)
    if u == "d" and v > 365:
        return None
    if u == "s": return timedelta(seconds=v)
    if u == "m": return timedelta(minutes=v)
    if u == "h": return timedelta(hours=v)
    if u == "d": return timedelta(days=v)
    return None

def utc_now():
    return datetime.now(timezone.utc)

class ImprovedMuteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = getattr(bot, "logger", None)
        self._startup_task = self.bot.loop.create_task(self._startup_work())
        # background loop managed via tasks.loop
        self._auto_unmute_loop.start()
        self._overwrite_locks: Dict[int, asyncio.Lock] = {}

        # Intents sanity warnings
        intents = getattr(bot, "intents", None)
        if intents:
            if not intents.message_content and self.logger:
                self.logger.info("Warning: message_content intent is disabled; on_message moderation will not see content.")
            if not intents.members and self.logger:
                self.logger.info("Warning: members intent is disabled; member resolution may fail in large guilds.")

    # ---------------------------
    # Logging helper
    # ---------------------------
    def _log(self, *args, **kwargs):
        if self.logger:
            self.logger.info(*args, **kwargs)

    # ---------------------------
    # DB helpers
    # ---------------------------
    def _next_case(self, guild_id: int) -> int:
        """Atomic increment returning next case (1-based)."""
        try:
            res = guild_counters.find_one_and_update(
                {"guild_id": guild_id},
                {"$inc": {"case_id": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            return int(res.get("case_id", 1))
        except PyMongoError:
            # fallback: try to fetch and compute; not ideal but safe
            doc = guild_counters.find_one({"guild_id": guild_id})
            if not doc:
                guild_counters.insert_one({"guild_id": guild_id, "case_id": 1})
                return 1
            else:
                nxt = doc.get("case_id", 0) + 1
                guild_counters.update_one({"guild_id": guild_id}, {"$set": {"case_id": nxt}})
                return nxt

    async def _startup_work(self):
        """Schedule pending DM deletions persisted across restarts."""
        await self.bot.wait_until_ready()
        try:
            docs = list(pending_dm_deletes.find({}))
            now = utc_now()
            for doc in docs:
                try:
                    expires = doc.get("expires_at")
                    if not expires:
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                        continue
                    delay = (expires - now).total_seconds()
                    if delay <= 0:
                        await self._delete_dm_by_doc(doc)
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    else:
                        self.bot.loop.create_task(self._schedule_delete_dm(doc["_id"], delay))
                except Exception:
                    try:
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    except Exception:
                        pass
        except Exception:
            pass

    async def _schedule_delete_dm(self, doc_id, delay: float):
        await asyncio.sleep(delay)
        try:
            doc = pending_dm_deletes.find_one({"_id": doc_id})
            if not doc:
                return
            await self._delete_dm_by_doc(doc)
        finally:
            try:
                pending_dm_deletes.delete_one({"_id": doc_id})
            except Exception:
                pass

    async def _delete_dm_by_doc(self, doc):
        """Delete DM message if accessible. Use user.create_dm() to get DM channel."""
        try:
            user_id = doc.get("user_id")
            msg_id = doc.get("dm_message_id")
            if not user_id or not msg_id:
                return
            user = await self.bot.fetch_user(user_id)
            if not user:
                return
            dm = user.dm_channel
            if dm is None:
                dm = await user.create_dm()
            try:
                msg = await dm.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                # Message already deleted
                pass
            except discord.Forbidden:
                # Can't access DMs
                pass
            except Exception:
                pass
        except Exception:
            pass

    # ---------------------------
    # Permission helpers
    # ---------------------------
    def _has_overwrite_perms(self, guild: discord.Guild) -> Tuple[bool, str]:
        me = guild.me
        if not me:
            return False, "Bot user not present in guild."
        # Manage Channels governs editing channel permission overwrites
        if not me.guild_permissions.manage_channels:
            return False, "Bot lacks Manage Channels permission required to edit permission overwrites."
        return True, ""

    async def _can_manage_member(self, guild: discord.Guild, target: discord.Member) -> Tuple[bool, Optional[str]]:
        me = guild.me
        if not me:
            return False, "Bot user not present in guild."
        if not me.guild_permissions.manage_roles:
            return False, "Bot lacks Manage Roles permission."
        if target.id == guild.owner_id:
            return False, "Cannot moderate the server owner."
        if target.top_role >= me.top_role:
            return False, "Target's top role is equal or higher than the bot's top role."
        return True, None

    def _actor_can_target(self, guild: discord.Guild, actor: discord.Member, target: discord.Member) -> Tuple[bool, Optional[str]]:
        if actor.id == guild.owner_id:
            return True, None
        if target.id == guild.owner_id:
            return False, "Cannot moderate the server owner."
        if target.top_role >= actor.top_role:
            return False, "You cannot act on a member with an equal or higher top role."
        return True, None

    async def _apply_muted_overwrites(
        self,
        guild: discord.Guild,
        muted_role: discord.Role,
        jail_channel_id: int,
        base_sleep: float = 0.25,
        max_retries: int = 3,
    ):
        """Apply overwrites to categories first (inheritance), then leaf channels, with backoff."""
        ok, why = self._has_overwrite_perms(guild)
        if not ok:
            return [why]

        failed = []
        channels = list(guild.channels)
        categories = [c for c in channels if isinstance(c, discord.CategoryChannel)]
        others = [c for c in channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel))]

        async def apply_one(ch: discord.abc.GuildChannel):
            allow_in_jail = ch.id == jail_channel_id
            for attempt in range(1, max_retries + 1):
                try:
                    if allow_in_jail:
                        await ch.set_permissions(
                            muted_role,
                            view_channel=True,
                            send_messages=True,
                            add_reactions=False,
                            reason="Mute system: allow in jail",
                        )
                    else:
                        await ch.set_permissions(
                            muted_role,
                            view_channel=False,
                            send_messages=False,
                            reason="Mute system: hide from muted",
                        )
                    await asyncio.sleep(base_sleep)
                    return
                except discord.HTTPException:
                    # exponential backoff with jitter
                    backoff = min(4.0, (2 ** (attempt - 1)) * base_sleep) + random.uniform(0, 0.25)
                    await asyncio.sleep(backoff)
                except Exception:
                    break
            failed.append(getattr(ch, "name", str(ch.id)))

        # categories then others; guard with per-guild lock to avoid concurrent sweeps
        lock = self._overwrite_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            for ch in categories:
                await apply_one(ch)
            for ch in others:
                await apply_one(ch)

        return failed

    # ---------------------------
    # Slash commands: setup, check, reset, reapply
    # ---------------------------
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="setup-mute", description="Create Muted role, jail, and punishment-logs channel.")
    @app_commands.describe(category="Category to place punishment-logs (optional)")
    async def setup_mute(self, interaction: discord.Interaction, category: Optional[discord.CategoryChannel] = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            muted_role = discord.utils.get(guild.roles, name="Muted")
            if muted_role is None:
                muted_role = await guild.create_role(name="Muted", reason="Mute system setup")

            jail_name = "jail"
            jail_channel = discord.utils.get(guild.text_channels, name=jail_name)
            if jail_channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    muted_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, add_reactions=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
                }
                jail_channel = await guild.create_text_channel(jail_name, overwrites=overwrites, reason="Jail for muted members")
                try:
                    await jail_channel.send("You have been muted. Please wait for staff to review your case.")
                except Exception:
                    pass

            log_name = "punishment-logs"
            log_channel = discord.utils.get(guild.text_channels, name=log_name)
            if log_channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
                }
                if category:
                    log_channel = await guild.create_text_channel(log_name, category=category, overwrites=overwrites, reason="Logs for mute/unmute")
                else:
                    log_channel = await guild.create_text_channel(log_name, overwrites=overwrites, reason="Logs for mute/unmute")

            # apply overwrites across categories then channels
            failed = await self._apply_muted_overwrites(guild, muted_role, jail_channel.id)

            # save config
            cfg_doc = {
                "guild_id": guild.id,
                "muted_role_id": muted_role.id,
                "jail_channel_id": jail_channel.id,
                "log_channel_id": log_channel.id,
                "mod_role_id": None,
                "setup_by": interaction.user.id,
                "setup_at": utc_now()
            }
            guild_configs.update_one({"guild_id": guild.id}, {"$set": cfg_doc}, upsert=True)

            embed = discord.Embed(
                title="✅ Mute System Setup Complete",
                description="The quarantine system has been successfully configured!",
                color=0x51CF66,
                timestamp=utc_now()
            )
            embed.add_field(name="🔇 Muted Role", value=muted_role.mention, inline=True)
            embed.add_field(name="🔒 Jail Channel", value=jail_channel.mention, inline=True)
            embed.add_field(name="📝 Log Channel", value=log_channel.mention, inline=True)
            if failed:
                embed.add_field(
                    name="⚠️ Warnings",
                    value=f"Could not update permissions for: {', '.join(failed[:10])}",
                    inline=False
                )
            embed.set_footer(text=f"Setup by {interaction.user}", icon_url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send("An error occurred during setup. Check bot permissions and try again.", ephemeral=True)
            if self.logger:
                self.logger.exception("setup-mute failed", exc_info=e)

    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="check-muteperms", description="Check if mute role and permissions are correct.")
    async def check_muteperms(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            await interaction.response.send_message("Mute system not configured. Run /setup-mute first.", ephemeral=True)
            return
        problems = []
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        if muted_role is None:
            problems.append("Muted role missing.")
        else:
            me = guild.me
            if not me.guild_permissions.manage_roles:
                problems.append("Bot lacks Manage Roles permission.")
            elif muted_role >= me.top_role:
                problems.append("Muted role is equal/higher than bot's top role.")
        jail = guild.get_channel(cfg.get("jail_channel_id"))
        logch = guild.get_channel(cfg.get("log_channel_id"))
        if jail is None:
            problems.append("Jail channel missing.")
        if logch is None:
            problems.append("Log channel missing.")
        # Overwrite edit capability
        ok, why = self._has_overwrite_perms(guild)
        if not ok:
            problems.append(why)
        if problems:
            embed = discord.Embed(
                title="⚠️ Configuration Issues Found",
                description="The following problems were detected with your mute system:",
                color=0xFF6B6B,
                timestamp=utc_now()
            )
            for i, p in enumerate(problems, 1):
                embed.add_field(name=f"Issue #{i}", value=f"❌ {p}", inline=False)
            embed.set_footer(text="Please resolve these issues to ensure proper functionality")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="✅ Configuration Check Passed",
                description="All basic configuration checks passed successfully!",
                color=0x51CF66,
                timestamp=utc_now()
            )
            embed.add_field(name="🔇 Muted Role", value="✓ Configured", inline=True)
            embed.add_field(name="🔒 Jail Channel", value="✓ Configured", inline=True)
            embed.add_field(name="📝 Log Channel", value="✓ Configured", inline=True)
            embed.add_field(name="🛡️ Permissions", value="✓ Valid", inline=True)
            embed.set_footer(text="Your mute system is ready to use!")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="reset-muteconfig", description="Reset mute configuration (admin only).")
    @app_commands.describe(confirm="Type CONFIRM to actually reset")
    async def reset_muteconfig(self, interaction: discord.Interaction, confirm: Optional[str] = None):
        if confirm != "CONFIRM":
            await interaction.response.send_message("To confirm, run: /reset-muteconfig CONFIRM", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        # Remove config and mutes; keep channels/roles as-is
        guild_configs.delete_one({"guild_id": guild.id})
        result = mutes_col.delete_many({"guild_id": guild.id})
        embed = discord.Embed(
            title="🔄 Configuration Reset",
            description="The mute system configuration has been reset.",
            color=0xFFD93D,
            timestamp=utc_now()
        )
        embed.add_field(name="📋 Records Cleared", value=f"{result.deleted_count} mute records removed", inline=True)
        embed.add_field(name="⚠️ Note", value="Channels and roles remain intact", inline=False)
        embed.set_footer(text=f"Reset by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="reapply-mute-perms", description="Reapply Muted role overwrites across the server.")
    async def reapply_mute_perms(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            await interaction.response.send_message("Mute system not configured. Run /setup-mute first.", ephemeral=True)
            return
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        if not muted_role or not jail_ch:
            await interaction.response.send_message("Configuration invalid or incomplete. Re-run /setup-mute.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        failed = await self._apply_muted_overwrites(guild, muted_role, jail_ch.id)
        embed = discord.Embed(
            title="✅ Permissions Reapplied",
            description="Muted role overwrites have been reapplied across all categories and channels.",
            color=0x51CF66,
            timestamp=utc_now()
        )
        if failed:
            embed.add_field(
                name="⚠️ Failed Channels",
                value=f"Could not update: {', '.join(failed[:10])}",
                inline=False
            )
            embed.color = 0xFFD93D
        else:
            embed.add_field(name="📊 Status", value="All channels updated successfully", inline=False)
        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------
    # Prefix commands: setmodrole, qmute, qunmute, mutelist, clearmutes, jailhistory, case
    # ---------------------------
    @commands.command(name="setmodrole")
    @commands.has_permissions(administrator=True)
    async def setmodrole(self, ctx: commands.Context, role: discord.Role):
        if not ctx.guild:
            return await ctx.send("Use this command inside a server.")
        guild_configs.update_one({"guild_id": ctx.guild.id}, {"$set": {"mod_role_id": role.id}}, upsert=True)
        embed = discord.Embed(
            title="✅ Moderator Role Updated",
            description=f"The moderator role has been set to {role.mention}",
            color=0x51CF66,
            timestamp=utc_now()
        )
        embed.add_field(name="🛡️ Role", value=role.mention, inline=True)
        embed.add_field(name="📋 Role ID", value=f"`{role.id}`", inline=True)
        embed.set_footer(text=f"Updated by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="qmute", aliases=["mute"])
    @commands.guild_only()
    async def qmute(self, ctx: commands.Context, member: discord.Member, *args):
        """
        Usage:
         !qmute @user [--silent] [duration] <reason...>
        Examples:
         !qmute @user spam
         !qmute @user 10m spamming links
         !qmute @user --silent 1h raid cleanup
        """
        guild = ctx.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured. Ask an admin to run /setup-mute.")

        # permission check (admin or mod role)
        allowed = ctx.author.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")

        # Prevent self-mute
        if member.id == ctx.author.id:
            return await ctx.send("⚠️ You cannot mute yourself.")
        
        # Prevent bot mute
        if member.id == self.bot.user.id:
            return await ctx.send("⚠️ I cannot mute myself.")
        
        # Prevent muting other bots (optional safety)
        if member.bot:
            return await ctx.send("⚠️ Cannot mute bots.")

        # actor vs target hierarchy
        ok, why = self._actor_can_target(guild, ctx.author, member)
        if not ok:
            return await ctx.send(f"⚠️ You cannot mute that member: {why}")

        # bot feasibility
        can_manage, why = await self._can_manage_member(guild, member)
        if not can_manage:
            return await ctx.send(f"⚠️ I cannot mute that member: {why}")

        # parse flags
        silent = False
        args_list = list(args)
        if args_list and args_list[0] == "--silent":
            silent = True
            args_list = args_list[1:]

        # optional duration and reason
        expires_at = None
        reason = "No reason provided"
        if args_list:
            first = args_list[0]
            dur = parse_duration(first)
            if dur:
                reason = " ".join(args_list[1:]) if len(args_list) > 1 else "No reason provided"
                expires_at = utc_now() + dur
            else:
                reason = " ".join(args_list)

        already = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if already:
            return await ctx.send(f"{member.mention} is already muted.")

        # config objects
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role or not jail_ch or not log_ch:
            return await ctx.send("Configuration invalid or incomplete. Re-run /setup-mute.")

        # ensure bot can assign Muted role (Muted must be below bot's top role)
        me = guild.me
        if muted_role >= me.top_role:
            return await ctx.send("Muted role is equal/higher than my top role. Move my highest role above Muted.")

        # add role
        try:
            await member.add_roles(muted_role, reason=f"Muted by {ctx.author} | {reason[:100]}")
        except discord.Forbidden:
            return await ctx.send("Failed to add Muted role — check bot role hierarchy and Manage Roles permission.")
        except discord.HTTPException as e:
            return await ctx.send(f"Failed to add Muted role: {str(e)[:100]}")
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error adding muted role", exc_info=e)
            return await ctx.send("Failed to add Muted role due to an unexpected error.")

        # apply overwrites (ensure jail visible)
        await self._apply_muted_overwrites(guild, muted_role, jail_ch.id)

        # case id
        case = self._next_case(guild.id)

        # persist mute doc
        doc = {
            "guild_id": guild.id,
            "user_id": member.id,
            "muted_by_id": ctx.author.id,
            "reason": reason[:2000],  # Limit reason length for DB
            "muted_at": utc_now(),
            "active": True,
            "case_id": case,
            "silent": bool(silent)
        }
        if expires_at:
            doc["expires_at"] = expires_at
        try:
            mutes_col.insert_one(doc)
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to insert mute doc: {e}")
            # Continue anyway - role was already added
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error inserting mute doc", exc_info=e)

        # DM the user unless silent
        dm_was_sent = False
        if not silent:
            try:
                dm = await member.create_dm()
                dm_embed = discord.Embed(
                    title="🔇 You Have Been Muted",
                    description=f"You have been muted in **{guild.name}**",
                    color=0xFF6B6B,
                    timestamp=utc_now()
                )
                dm_embed.add_field(name="🛡️ Moderator", value=f"{ctx.author}", inline=True)
                dm_embed.add_field(name="📋 Case ID", value=f"#{case}", inline=True)
                dm_embed.add_field(name="📋 Reason", value=f"```{reason[:1000]}```", inline=False)
                if expires_at:
                    try:
                        timestamp = int(expires_at.timestamp())
                        dm_embed.add_field(name="⏰ Duration", value=f"Expires <t:{timestamp}:R>", inline=False)
                    except (AttributeError, ValueError, OSError):
                        dm_embed.add_field(name="⏰ Duration", value="Until manually unmuted", inline=False)
                else:
                    dm_embed.add_field(name="⏰ Duration", value="Until manually unmuted", inline=False)
                dm_embed.set_footer(text="This message will be automatically deleted in 10 minutes")
                dm_msg = await dm.send(embed=dm_embed)
                # persist deletion: schedule delete in 10 minutes
                expires = utc_now() + timedelta(minutes=10)
                ins = pending_dm_deletes.insert_one({
                    "guild_id": guild.id,
                    "user_id": member.id,
                    "dm_message_id": dm_msg.id,
                    "expires_at": expires
                })
                # schedule in-memory deletion too
                self.bot.loop.create_task(self._schedule_delete_dm(ins.inserted_id, 10 * 60))
                dm_was_sent = True
            except Exception:
                dm_was_sent = False

        # log embed
        embed = discord.Embed(
            title=f"🔇 Member Muted — Case #{case}",
            description=f"**{member.mention}** has been muted and moved to the quarantine zone.",
            color=0xFF6B6B,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="🛡️ Moderator", value=f"{ctx.author.mention}\n`{ctx.author} ({ctx.author.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="📋 Reason", value=f"```{reason[:1000]}```", inline=False)
        if expires_at:
            try:
                timestamp = int(expires_at.timestamp())
                embed.add_field(name="⏰ Expires", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=False)
            except (AttributeError, ValueError, OSError):
                embed.add_field(name="⏰ Expires", value=str(expires_at), inline=False)
        else:
            embed.add_field(name="⏰ Duration", value="⚠️ Manual unmute required", inline=False)
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        
        if silent:
            embed.set_footer(text="🔕 Muted silently (no DM sent)", icon_url=footer_icon)
        elif dm_was_sent:
            embed.set_footer(text="✉️ User notified via DM (auto-deletes in 10 minutes)", icon_url=footer_icon)
        else:
            embed.set_footer(text="⚠️ Could not send DM to user", icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        confirm_embed = discord.Embed(
            title="✅ Member Muted Successfully",
            description=f"{member.mention} has been muted and moved to {jail_ch.mention}",
            color=0x51CF66,
            timestamp=utc_now()
        )
        confirm_embed.add_field(name="📋 Case ID", value=f"#{case}", inline=True)
        confirm_embed.add_field(name="👤 Member", value=member.mention, inline=True)
        if expires_at:
            try:
                timestamp = int(expires_at.timestamp())
                confirm_embed.add_field(name="⏰ Expires", value=f"<t:{timestamp}:R>", inline=True)
            except (AttributeError, ValueError, OSError):
                pass
        try:
            footer_icon = ctx.author.display_avatar.url
        except Exception:
            footer_icon = None
        confirm_embed.set_footer(text=f"Muted by {ctx.author}", icon_url=footer_icon)
        await ctx.send(embed=confirm_embed)

    @commands.command(name="qunmute", aliases=["unmute"])
    @commands.guild_only()
    async def qunmute(self, ctx: commands.Context, member: discord.Member):
        guild = ctx.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")

        # permission check (admin or mod role)
        allowed = ctx.author.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                role = guild.get_role(mod_role_id)
                if role and role in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        
        # Prevent self-unmute
        if member.id == ctx.author.id and not ctx.author.guild_permissions.administrator:
            return await ctx.send("⚠️ You cannot unmute yourself.")

        # Optional: prevent moderators from unmuting targets above/equal to them
        ok, why = self._actor_can_target(guild, ctx.author, member)
        if not ok:
            return await ctx.send(f"⚠️ You cannot unmute that member: {why}")

        muted_role = guild.get_role(cfg.get("muted_role_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role:
            return await ctx.send("Muted role missing. Re-run /setup-mute.")

        doc = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if not doc:
            return await ctx.send(f"{member.mention} is not currently muted.")

        # bot feasibility not strictly needed to update DB, but try role removal
        try:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
        except discord.Forbidden:
            # Log but continue - DB will be updated
            if self.logger:
                self.logger.warning(f"Failed to remove muted role from {member.id} - Forbidden")
        except discord.HTTPException as e:
            if self.logger:
                self.logger.warning(f"Failed to remove muted role from {member.id} - HTTPException: {e}")
        except Exception as e:
            if self.logger:
                self.logger.exception(f"Unexpected error removing muted role from {member.id}", exc_info=e)

        # mark as inactive
        try:
            result = mutes_col.update_many({"guild_id": guild.id, "user_id": member.id, "active": True}, {"$set": {
                "active": False, "unmuted_at": utc_now(), "unmuted_by_id": ctx.author.id
            }})
            if result.modified_count == 0 and self.logger:
                self.logger.warning(f"Unmute: No active mute records found for {member.id} in guild {guild.id}")
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to update mute record: {e}")
        except Exception as e:
            if self.logger:
                self.logger.exception("Unexpected error updating mute record", exc_info=e)

        case = doc.get("case_id", "N/A")
        embed = discord.Embed(
            title=f"🔊 Member Unmuted — Case #{case}",
            description=f"**{member.mention}** has been unmuted and can now access the server.",
            color=0x51CF66,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="👤 Member", value=f"{member.mention}\n`{member} ({member.id})`", inline=True)
        embed.add_field(name="🛡️ Moderator", value=f"{ctx.author.mention}\n`{ctx.author} ({ctx.author.id})`", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        muted_at = doc.get("muted_at")
        if muted_at and isinstance(muted_at, datetime):
            try:
                timestamp = int(muted_at.timestamp())
                embed.add_field(name="📅 Originally Muted", value=f"<t:{timestamp}:F>", inline=False)
            except (AttributeError, ValueError, OSError):
                pass
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        embed.set_footer(text="✅ Mute successfully removed", icon_url=footer_icon)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        # DM user quietly
        try:
            dm = await member.create_dm()
            dm_embed = discord.Embed(
                title="🔊 You Have Been Unmuted",
                description=f"You have been unmuted in **{guild.name}**",
                color=0x51CF66,
                timestamp=utc_now()
            )
            dm_embed.add_field(name="🛡️ Moderator", value=f"{ctx.author}", inline=True)
            dm_embed.add_field(name="📋 Case ID", value=f"#{case}", inline=True)
            dm_embed.set_footer(text="You can now access all server channels again")
            await dm.send(embed=dm_embed)
        except Exception:
            pass

        confirm_embed = discord.Embed(
            title="✅ Member Unmuted Successfully",
            description=f"{member.mention} has been unmuted and can now access the server.",
            color=0x51CF66,
            timestamp=utc_now()
        )
        confirm_embed.add_field(name="📋 Case ID", value=f"#{case}", inline=True)
        confirm_embed.add_field(name="👤 Member", value=member.mention, inline=True)
        try:
            footer_icon = ctx.author.display_avatar.url
        except Exception:
            footer_icon = None
        confirm_embed.set_footer(text=f"Unmuted by {ctx.author}", icon_url=footer_icon)
        await ctx.send(embed=confirm_embed)

    @commands.command(name="mutelist")
    @commands.guild_only()
    async def mutelist(self, ctx: commands.Context):
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        docs = list(mutes_col.find({"guild_id": ctx.guild.id, "active": True}).sort("muted_at", -1).limit(100))
        if not docs:
            return await ctx.send("No members are currently muted.")

        def make_embed(batch):
            embed = discord.Embed(
                title="🔇 Currently Muted Members",
                description=f"Showing **{len(batch)}** of **{len(docs)}** total muted members",
                color=0x9775FA,
                timestamp=utc_now()
            )
            # Discord has a 25 field limit, so cap at 10 per page
            for d in batch[:10]:
                uid = d["user_id"]
                member = ctx.guild.get_member(uid)
                mention = member.mention if member else f"<@{uid}>"
                by = d.get("muted_by_id")
                muter = ctx.guild.get_member(by)
                muter_s = muter.mention if muter else f"<@{by}>"
                reason = d.get("reason", "No reason")
                at = d.get("muted_at")
                case = d.get("case_id", "N/A")
                expires = d.get("expires_at")
                
                field_value = f"**Moderator:** {muter_s}\n"
                field_value += f"**Reason:** {reason[:100]}\n"
                if isinstance(at, datetime):
                    try:
                        field_value += f"**Muted:** <t:{int(at.timestamp())}:R>\n"
                    except (AttributeError, ValueError, OSError):
                        pass
                if isinstance(expires, datetime):
                    try:
                        field_value += f"**Expires:** <t:{int(expires.timestamp())}:R>"
                    except (AttributeError, ValueError, OSError):
                        field_value += f"**Expires:** ⚠️ Manual unmute required"
                else:
                    field_value += f"**Expires:** ⚠️ Manual unmute required"
                
                embed.add_field(name=f"Case #{case} — {mention}", value=field_value, inline=False)
            
            footer_icon = None
            try:
                if ctx.guild and ctx.guild.icon:
                    footer_icon = ctx.guild.icon.url
            except Exception:
                pass
            embed.set_footer(text=f"Total muted members: {len(docs)}", icon_url=footer_icon)
            return embed

        # Send in batches of 10 to respect Discord's 25 field limit
        for i in range(0, len(docs), 10):
            await ctx.send(embed=make_embed(docs[i:i+10]))

    @commands.command(name="clearmutes")
    @commands.has_permissions(administrator=True)
    async def clearmutes(self, ctx: commands.Context, days: Optional[int] = 30):
        # Validate days parameter
        if days <= 0:
            return await ctx.send("⚠️ Days must be a positive number.")
        if days > 3650:  # 10 years max
            return await ctx.send("⚠️ Days cannot exceed 3650 (10 years).")
        
        cutoff = utc_now() - timedelta(days=days)
        try:
            res = mutes_col.delete_many({"active": False, "muted_at": {"$lt": cutoff}})
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to delete mute records: {e}")
            return await ctx.send("⚠️ Failed to clear mute records. Check logs for details.")
        embed = discord.Embed(
            title="🧹 Database Cleanup Complete",
            description=f"Cleared old inactive mute records from the database.",
            color=0x51CF66,
            timestamp=utc_now()
        )
        embed.add_field(name="📊 Records Deleted", value=f"**{res.deleted_count}** records", inline=True)
        embed.add_field(name="📅 Older Than", value=f"**{days}** days", inline=True)
        embed.set_footer(text=f"Cleanup by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="jailhistory")
    @commands.guild_only()
    async def jailhistory(self, ctx: commands.Context, user: discord.User, limit: Optional[int] = 10):
        """Moderator-only: fetch recent messages for a muted user from jail (default last 10)."""
        # Validate limit
        if limit <= 0:
            return await ctx.send("⚠️ Limit must be a positive number.")
        if limit > 50:
            limit = 50
            await ctx.send("⚠️ Limit capped at 50 messages.")
        
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        # permission check: admin or mod role
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = ctx.guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        try:
            docs = list(jail_messages.find({"guild_id": ctx.guild.id, "user_id": user.id}).sort("created_at", -1).limit(min(50, max(1, limit))))
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to fetch jail messages: {e}")
            return await ctx.send("⚠️ Failed to retrieve jail history. Try again later.")
        if not docs:
            embed = discord.Embed(
                title="💭 No Messages Found",
                description=f"No jail messages found for {user.mention} in the last 7 days.",
                color=0xFFD93D,
                timestamp=utc_now()
            )
            embed.set_footer(text="Messages are automatically deleted after 7 days")
            return await ctx.send(embed=embed)
        
        embed = discord.Embed(
            title=f"📝 Jail History for {user.name}",
            description=f"Showing last **{min(len(docs), limit)}** messages from {user.mention}",
            color=0x9775FA,
            timestamp=utc_now()
        )
        try:
            embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        
        # Discord has a 25 field limit
        max_messages = min(limit, 20)
        for i, d in enumerate(docs[:max_messages], 1):
            ts = d.get("created_at")
            content = d.get("content", "")[:200]
            if isinstance(ts, datetime):
                try:
                    time_str = f"<t:{int(ts.timestamp())}:R>"
                except (AttributeError, ValueError, OSError):
                    time_str = str(ts)
            else:
                time_str = str(ts)
            # Escape backticks in content to prevent breaking code block
            if content:
                content = content.replace("```", "'''")
                # Also escape other potential markdown issues
                content = content.replace("\n", " ")  # Replace newlines with spaces
            embed.add_field(
                name=f"Message #{i} — {time_str}",
                value=f"```{content}```" if content else "*Empty message*",
                inline=False
            )
        
        embed.set_footer(text=f"Requested by {ctx.author} | Messages auto-delete after 7 days", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="case")
    @commands.guild_only()
    async def case(self, ctx: commands.Context, case_id: int):
        """Show details for a case id (moderator-only)."""
        # Validate case_id
        if case_id <= 0:
            return await ctx.send("⚠️ Case ID must be a positive number.")
        
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        # permission check
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = ctx.guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")
        try:
            doc = mutes_col.find_one({"guild_id": ctx.guild.id, "case_id": case_id})
        except PyMongoError as e:
            if self.logger:
                self.logger.error(f"Failed to fetch case: {e}")
            return await ctx.send("⚠️ Failed to retrieve case information. Try again later.")
        
        if not doc:
            return await ctx.send(f"No case found for Case #{case_id}.")
        member_repr = f"<@{doc.get('user_id')}>"
        muted_by_repr = f"<@{doc.get('muted_by_id')}>"
        reason = doc.get("reason", "No reason")
        muted_at = doc.get("muted_at")
        muted_at_s = muted_at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(muted_at, datetime) else str(muted_at)
        active = doc.get("active", False)
        status_emoji = "🔴" if active else "🟢"
        status_text = "Active" if active else "Resolved"
        embed = discord.Embed(
            title=f"📋 Case #{case_id} — {status_text}",
            description=f"Detailed information for mute case **#{case_id}**",
            color=0xFF6B6B if active else 0x51CF66,
            timestamp=utc_now()
        )
        embed.add_field(name="👤 Member", value=member_repr, inline=True)
        embed.add_field(name="🛡️ Muted By", value=muted_by_repr, inline=True)
        embed.add_field(name="📊 Status", value=f"{status_emoji} **{status_text}**", inline=True)
        embed.add_field(name="📋 Reason", value=f"```{reason[:1000]}```", inline=False)
        if isinstance(muted_at, datetime):
            try:
                timestamp = int(muted_at.timestamp())
                embed.add_field(name="📅 Muted At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
            except (AttributeError, ValueError, OSError):
                embed.add_field(name="📅 Muted At", value=muted_at_s, inline=True)
        else:
            embed.add_field(name="📅 Muted At", value=muted_at_s, inline=True)
        if doc.get("expires_at"):
            exp_ts = doc["expires_at"]
            if isinstance(exp_ts, datetime):
                try:
                    timestamp = int(exp_ts.timestamp())
                    embed.add_field(name="⏰ Expires At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
                except (AttributeError, ValueError, OSError):
                    embed.add_field(name="⏰ Duration", value="⚠️ Manual unmute", inline=True)
            else:
                embed.add_field(name="⏰ Duration", value="⚠️ Manual unmute", inline=True)
        else:
            embed.add_field(name="⏰ Duration", value="⚠️ Manual unmute", inline=True)
        if doc.get("unmuted_at"):
            unmute_ts = doc["unmuted_at"]
            if isinstance(unmute_ts, datetime):
                try:
                    timestamp = int(unmute_ts.timestamp())
                    embed.add_field(name="✅ Unmuted At", value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>", inline=True)
                except (AttributeError, ValueError, OSError):
                    pass
            if doc.get("unmuted_by_id"):
                embed.add_field(name="🛡️ Unmuted By", value=f"<@{doc['unmuted_by_id']}>", inline=True)
        footer_icon = None
        try:
            if ctx.guild and ctx.guild.icon:
                footer_icon = ctx.guild.icon.url
        except Exception:
            pass
        embed.set_footer(text=f"Case ID: {case_id}", icon_url=footer_icon)
        await ctx.send(embed=embed)

    # ---------------------------
    # Background auto-unmute loop (temporary mutes) with tasks.loop
    # ---------------------------
    @tasks.loop(seconds=20)
    async def _auto_unmute_loop(self):
        try:
            now = utc_now()
            docs = list(mutes_col.find({"active": True, "expires_at": {"$lte": now}}))
            for doc in docs:
                try:
                    guild = self.bot.get_guild(doc["guild_id"])
                    if not guild:
                        mutes_col.update_one({"_id": doc["_id"]}, {"$set": {"active": False}})
                        continue
                    cfg = guild_configs.find_one({"guild_id": guild.id})
                    if not cfg:
                        mutes_col.update_one({"_id": doc["_id"]}, {"$set": {"active": False}})
                        continue
                    muted_role = guild.get_role(cfg.get("muted_role_id"))
                    log_ch = guild.get_channel(cfg.get("log_channel_id"))

                    member = guild.get_member(doc["user_id"])
                    if member is None:
                        try:
                            member = await guild.fetch_member(doc["user_id"])
                        except Exception:
                            member = None

                    if member and muted_role and guild.me and guild.me.guild_permissions.manage_roles:
                        try:
                            await member.remove_roles(muted_role, reason="Temporary mute expired")
                        except discord.Forbidden:
                            if self.logger:
                                self.logger.warning(f"Auto-unmute: Failed to remove role from {member.id} - Forbidden")
                        except discord.HTTPException:
                            # Rate limited or other HTTP error, will retry next loop
                            continue
                        except Exception as e:
                            if self.logger:
                                self.logger.error(f"Auto-unmute: Unexpected error removing role: {e}")

                    # atomic update so we only log once
                    result = mutes_col.update_one(
                        {"_id": doc["_id"], "active": True},
                        {"$set": {"active": False, "unmuted_at": utc_now(), "unmuted_by_id": None}}
                    )
                    if result.modified_count > 0 and log_ch:
                        case_id = doc.get("case_id", "N/A")
                        embed = discord.Embed(
                            title=f"🔊 Auto-Unmute — Case #{case_id}",
                            description=f"Temporary mute has expired and been automatically removed.",
                            color=0x51CF66,
                            timestamp=utc_now()
                        )
                        uid = doc.get("user_id")
                        embed.add_field(name="👤 Member", value=f"<@{uid}>", inline=True)
                        embed.add_field(name="⏰ Reason", value="⏱️ Temporary mute expired", inline=True)
                        embed.add_field(name="🤖 Action", value="Automatic", inline=True)
                        muted_at = doc.get("muted_at")
                        if muted_at and isinstance(muted_at, datetime):
                            try:
                                timestamp = int(muted_at.timestamp())
                                embed.add_field(name="📅 Originally Muted", value=f"<t:{timestamp}:R>", inline=False)
                            except (AttributeError, ValueError, OSError):
                                pass
                        footer_icon = None
                        try:
                            if guild and guild.icon:
                                footer_icon = guild.icon.url
                        except Exception:
                            pass
                        embed.set_footer(text="✅ Automatic unmute completed", icon_url=footer_icon)
                        try:
                            await log_ch.send(embed=embed)
                        except Exception:
                            pass
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Error in auto-unmute for doc {doc.get('_id')}: {e}")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error in auto-unmute loop: {e}")

    @_auto_unmute_loop.before_loop
    async def _before_auto_unmute_loop(self):
        await self.bot.wait_until_ready()

    # ---------------------------
    # Listener: jail message logging and mention enforcement
    # ---------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            return
        cfg = guild_configs.find_one({"guild_id": message.guild.id})
        if not cfg:
            return
        muted_role_id = cfg.get("muted_role_id")
        jail_id = cfg.get("jail_channel_id")
        if not muted_role_id or not jail_id:
            return
        if message.channel.id != jail_id:
            return
        member = message.author
        if muted_role_id not in [r.id for r in member.roles]:
            return
        # log message (store content, id, timestamp)
        try:
            jail_messages.insert_one({
                "guild_id": message.guild.id,
                "user_id": member.id,
                "message_id": message.id,
                "content": message.content,
                "created_at": utc_now()
            })
        except Exception:
            pass
        # enforce no mentions
        has_mentions = bool(message.mentions or message.role_mentions or message.mention_everyone)
        if has_mentions:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                warn = await message.channel.send(f"{member.mention} — you may not mention anyone in this channel. Your message was removed.")
                await asyncio.sleep(12)
                try:
                    await warn.delete()
                except Exception:
                    pass
            except Exception:
                pass

    # ---------------------------
    # Errors & unload
    # ---------------------------
    @qmute.error
    async def _on_qmute_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!qmute @user [--silent] [duration] <reason>`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Couldn't find that user. Mention them like `@user`.")
        else:
            await ctx.send("An error occurred while executing the command.")
            if self.logger:
                self.logger.exception("qmute command error", exc_info=error)

    @qunmute.error
    async def _on_qunmute_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!qunmute @user`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Couldn't find that user. Mention them like `@user`.")
        else:
            await ctx.send("An error occurred while executing the command.")
            if self.logger:
                self.logger.exception("qunmute command error", exc_info=error)

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        try:
            self._startup_task.cancel()
        except Exception:
            pass
        try:
            self._auto_unmute_loop.cancel()
        except Exception:
            pass
        # Clear lock dictionary to prevent memory leaks
        try:
            self._overwrite_locks.clear()
        except Exception:
            pass
        # Close MongoDB connection
        try:
            mongo.close()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(ImprovedMuteCog(bot))
