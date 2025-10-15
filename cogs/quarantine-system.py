# improved_mute_cog.py
# Improved and bug-fixed single-file Cog for wick-style role-based mute system.
# - Fixes for ReturnDocument usage, persistent DM deletion, safer permission edits
# - Added: !jailhistory, !case, --silent mute, better logging and error handling
# - Keep all previous features: /setup-mute, /check-muteperms, /reset-muteconfig, TTL jail_messages, auto-unmute for expiries

import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import re
from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import Optional, List

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing from environment (.env)")

# Connect to MongoDB
mongo = MongoClient(MONGO_URL)
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
    pending_dm_deletes.create_index([("expires_at", ASCENDING)])
    # TTL on created_at -> 7 days
    jail_messages.create_index([("created_at", ASCENDING)], expireAfterSeconds=7 * 24 * 3600)
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
    if u == "s": return timedelta(seconds=v)
    if u == "m": return timedelta(minutes=v)
    if u == "h": return timedelta(hours=v)
    if u == "d": return timedelta(days=v)
    return None

def utc_now():
    return datetime.utcnow()

class ImprovedMuteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_task = self.bot.loop.create_task(self._startup_work())
        self._unmute_task = self.bot.loop.create_task(self._auto_unmute_loop())
        # optional logger
        self.logger = getattr(bot, "logger", None)

    # ---------------------------
    # DB helpers
    # ---------------------------
    def _log(self, *args, **kwargs):
        if self.logger:
            self.logger.info(*args, **kwargs)

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
                        # delete immediately
                        await self._delete_dm_by_doc(doc)
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    else:
                        # schedule
                        self.bot.loop.create_task(self._schedule_delete_dm(doc["_id"], delay))
                except Exception:
                    # ignore individual doc failures
                    try:
                        pending_dm_deletes.delete_one({"_id": doc["_id"]})
                    except Exception:
                        pass
        except Exception:
            # If DB unreadable at startup, continue and try later
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
            user_id = doc["user_id"]
            msg_id = doc.get("dm_message_id")
            user = await self.bot.fetch_user(user_id)
            if not user:
                return
            dm = user.dm_channel
            if dm is None:
                dm = await user.create_dm()
            if msg_id:
                try:
                    msg = await dm.fetch_message(msg_id)
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass

    # ---------------------------
    # Permission helpers
    # ---------------------------
    async def _apply_muted_overwrites(self, guild: discord.Guild, muted_role: discord.Role, jail_channel_id: int, sleep_interval: float = 0.2):
        """Apply overwrites to all channels. Adaptive sleep and retry on failure."""
        failed = []
        for ch in list(guild.channels):
            if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
                continue
            try:
                if ch.id == jail_channel_id:
                    await ch.set_permissions(muted_role, view_channel=True, send_messages=True, add_reactions=False, reason="Mute system: allow in jail")
                else:
                    await ch.set_permissions(muted_role, view_channel=False, send_messages=False, reason="Mute system: hide from muted")
                await asyncio.sleep(sleep_interval)
            except discord.HTTPException as e:
                # retry once after short sleep
                await asyncio.sleep(1.0)
                try:
                    if ch.id == jail_channel_id:
                        await ch.set_permissions(muted_role, view_channel=True, send_messages=True, add_reactions=False, reason="Mute system retry")
                    else:
                        await ch.set_permissions(muted_role, view_channel=False, send_messages=False, reason="Mute system retry")
                except Exception:
                    failed.append(ch.name)
            except Exception:
                failed.append(ch.name)
        return failed

    async def _can_manage_member(self, guild: discord.Guild, target: discord.Member) -> (bool, Optional[str]):
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return False, "Bot lacks Manage Roles permission."
        if target.top_role >= me.top_role:
            return False, "Target's top role is equal or higher than the bot's top role."
        return True, None

    # ---------------------------
    # Slash commands: setup, check, reset
    # ---------------------------
    @app_commands.command(name="setup-mute", description="Create Muted role, jail, and punishment-logs channel.")
    @app_commands.describe(category="Category to place punishment-logs (optional)")
    async def setup_mute(self, interaction: discord.Interaction, category: Optional[discord.CategoryChannel] = None):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can run this command.", ephemeral=True)
            return
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

            # apply overwrites across channels
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

            msg = f"Setup complete.\nMuted role: {muted_role.mention}\nJail: {jail_channel.mention}\nLogs: {log_channel.mention}"
            if failed:
                msg += f"\n⚠️ Could not update permissions for some channels: {', '.join(failed[:10])}"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send("An error occurred during setup. Check bot permissions and try again.", ephemeral=True)
            if self.logger:
                self.logger.exception("setup-mute failed", exc_info=e)

    @app_commands.command(name="check-muteperms", description="Check if mute role and permissions are correct.")
    async def check_muteperms(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can run this command.", ephemeral=True)
            return
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
            if not guild.me.guild_permissions.manage_roles:
                problems.append("Bot lacks Manage Roles permission.")
            elif muted_role >= guild.me.top_role:
                problems.append("Muted role is equal/higher than bot's top role.")
        jail = guild.get_channel(cfg.get("jail_channel_id"))
        logch = guild.get_channel(cfg.get("log_channel_id"))
        if jail is None:
            problems.append("Jail channel missing.")
        if logch is None:
            problems.append("Log channel missing.")
        if problems:
            await interaction.response.send_message("Problems found:\n" + "\n".join(f"- {p}" for p in problems), ephemeral=True)
        else:
            await interaction.response.send_message("Basic configuration looks good.", ephemeral=True)

    @app_commands.command(name="reset-muteconfig", description="Reset mute configuration (admin only).")
    @app_commands.describe(confirm="Type CONFIRM to actually reset")
    async def reset_muteconfig(self, interaction: discord.Interaction, confirm: Optional[str] = None):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only admins can run this.", ephemeral=True)
            return
        if confirm != "CONFIRM":
            await interaction.response.send_message("To confirm, run: /reset-muteconfig CONFIRM", ephemeral=True)
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This must be used in a server.", ephemeral=True)
            return
        # We remove config and mutes; keep channels/roles as-is to avoid destructive ops
        guild_configs.delete_one({"guild_id": guild.id})
        mutes_col.delete_many({"guild_id": guild.id})
        await interaction.response.send_message("Mute configuration and mute records cleared.", ephemeral=True)

    # ---------------------------
    # Prefix commands: setmodrole, qmute, qunmute, mutelist, clearmutes, jailhistory, case
    # ---------------------------
    @commands.command(name="setmodrole")
    @commands.has_permissions(administrator=True)
    async def setmodrole(self, ctx: commands.Context, role: discord.Role):
        if not ctx.guild:
            return await ctx.send("Use this command inside a server.")
        guild_configs.update_one({"guild_id": ctx.guild.id}, {"$set": {"mod_role_id": role.id}}, upsert=True)
        await ctx.send(f"Moderator role set to {role.mention}.")

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
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                mr = guild.get_role(mod_role_id)
                if mr and mr in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")

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

        # safety
        can_manage, why = await self._can_manage_member(guild, member)
        if not can_manage:
            return await ctx.send(f"⚠️ I cannot mute that member: {why}")

        already = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if already:
            return await ctx.send(f"{member.mention} is already muted.")

        # check config objects
        muted_role = guild.get_role(cfg.get("muted_role_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        if not muted_role or not jail_ch or not log_ch:
            return await ctx.send("Configuration invalid or incomplete. Re-run /setup-mute.")

        # add role
        try:
            await member.add_roles(muted_role, reason=f"Muted by {ctx.author} | {reason}")
        except discord.Forbidden:
            return await ctx.send("Failed to add Muted role — check bot role hierarchy and Manage Roles permission.")
        except Exception:
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
            "reason": reason,
            "muted_at": utc_now(),
            "active": True,
            "case_id": case,
            "silent": bool(silent)
        }
        if expires_at:
            doc["expires_at"] = expires_at
        try:
            mutes_col.insert_one(doc)
        except Exception:
            # note and continue
            if self.logger:
                self.logger.exception("Failed to insert mute doc")

        # DM the user unless silent
        dm_was_sent = False
        if not silent:
            try:
                dm = await member.create_dm()
                dm_msg = await dm.send(f"You have been muted in **{guild.name}** by **{ctx.author}** for the following reason:\n\n{reason}")
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
        embed = discord.Embed(title=f"Member Muted — Case #{case}", color=discord.Color.orange(), timestamp=utc_now())
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Muted by", value=f"{ctx.author} ({ctx.author.id})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        if expires_at:
            embed.add_field(name="Expires at", value=expires_at.strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        if silent:
            embed.set_footer(text="Muted silently (no DM).")
        elif dm_was_sent:
            embed.set_footer(text="User DM'd (auto-deletes in 10 minutes).")
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        await ctx.send(f"{member.mention} has been muted. Case #{case}. Moved to {jail_ch.mention}")

    @commands.command(name="qunmute", aliases=["unmute"])
    @commands.guild_only()
    async def qunmute(self, ctx: commands.Context, member: discord.Member):
        guild = ctx.guild
        cfg = guild_configs.find_one({"guild_id": guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        allowed = False
        if ctx.author.guild_permissions.administrator:
            allowed = True
        else:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                role = guild.get_role(mod_role_id)
                if role and role in ctx.author.roles:
                    allowed = True
        if not allowed:
            return await ctx.send("You don't have permission to use this command.")

        muted_role = guild.get_role(cfg.get("muted_role_id"))
        log_ch = guild.get_channel(cfg.get("log_channel_id"))
        jail_ch = guild.get_channel(cfg.get("jail_channel_id"))
        if not muted_role:
            return await ctx.send("Muted role missing. Re-run /setup-mute.")

        doc = mutes_col.find_one({"guild_id": guild.id, "user_id": member.id, "active": True})
        if not doc:
            return await ctx.send(f"{member.mention} is not currently muted.")

        can_manage, why = await self._can_manage_member(guild, member)
        if not can_manage:
            # still attempt DB update but warn
            await ctx.send(f"Warning: I may not be able to remove the role: {why}")

        # attempt removal
        try:
            await member.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")
        except Exception:
            # continue to update DB anyway
            pass

        # mark as inactive
        try:
            mutes_col.update_many({"guild_id": guild.id, "user_id": member.id, "active": True}, {"$set": {
                "active": False, "unmuted_at": utc_now(), "unmuted_by_id": ctx.author.id
            }})
        except Exception:
            pass

        case = doc.get("case_id", "N/A")
        embed = discord.Embed(title=f"Member Unmuted — Case #{case}", color=discord.Color.green(), timestamp=utc_now())
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Unmuted by", value=f"{ctx.author} ({ctx.author.id})", inline=False)
        try:
            await log_ch.send(embed=embed)
        except Exception:
            pass

        # DM user quietly
        try:
            dm = await member.create_dm()
            await dm.send(f"You have been unmuted in **{guild.name}** by **{ctx.author}**.")
        except Exception:
            pass

        await ctx.send(f"{member.mention} has been unmuted. Case #{case}")

    @commands.command(name="mutelist")
    @commands.guild_only()
    async def mutelist(self, ctx: commands.Context):
        cfg = guild_configs.find_one({"guild_id": ctx.guild.id})
        if not cfg:
            return await ctx.send("Mute system not configured.")
        docs = list(mutes_col.find({"guild_id": ctx.guild.id, "active": True}).sort("muted_at", -1).limit(50))
        if not docs:
            return await ctx.send("No members are currently muted.")
        embed = discord.Embed(title="Muted Members", color=discord.Color.dark_purple(), timestamp=utc_now())
        lines = []
        for d in docs:
            uid = d["user_id"]
            member = ctx.guild.get_member(uid)
            mention = member.mention if member else f"<@{uid}>"
            by = d.get("muted_by_id")
            muter = ctx.guild.get_member(by) or f"<@{by}>"
            reason = d.get("reason", "No reason")
            at = d.get("muted_at")
            at_s = at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(at, datetime) else str(at)
            case = d.get("case_id", "N/A")
            expires = d.get("expires_at")
            exp_s = expires.strftime("%Y-%m-%d %H:%M UTC") if isinstance(expires, datetime) else "Manual unmute required"
            lines.append(f"{mention} — by {muter} on {at_s}\nReason: {reason}\nCase: {case} | Expires: {exp_s}")
        embed.description = "\n\n".join(lines[:15])
        await ctx.send(embed=embed)

    @commands.command(name="clearmutes")
    @commands.has_permissions(administrator=True)
    async def clearmutes(self, ctx: commands.Context, days: Optional[int] = 30):
        cutoff = utc_now() - timedelta(days=days)
        res = mutes_col.delete_many({"active": False, "muted_at": {"$lt": cutoff}})
        await ctx.send(f"Deleted {res.deleted_count} inactive mute records older than {days} days.")

    @commands.command(name="jailhistory")
    @commands.guild_only()
    async def jailhistory(self, ctx: commands.Context, user: discord.User, limit: Optional[int] = 10):
        """Moderator-only: fetch recent messages for a muted user from jail (default last 10)."""
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
        docs = list(jail_messages.find({"guild_id": ctx.guild.id, "user_id": user.id}).sort("created_at", -1).limit(min(50, max(1, limit))))
        if not docs:
            return await ctx.send("No jail messages found for that user in the last 7 days.")
        lines = []
        for d in docs[:limit]:
            ts = d.get("created_at")
            ts_s = ts.strftime("%Y-%m-%d %H:%M UTC") if isinstance(ts, datetime) else str(ts)
            content = d.get("content", "")[:180]
            lines.append(f"{ts_s} — {content!r}")
        msg = f"Last {len(lines)} jail messages for {user}:\n\n" + "\n\n".join(lines)
        # If very long, send as multiple messages
        if len(msg) > 1900:
            # chunk
            chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
            for c in chunks:
                await ctx.send(f"```{c}```")
        else:
            await ctx.send(f"```{msg}```")

    @commands.command(name="case")
    @commands.guild_only()
    async def case(self, ctx: commands.Context, case_id: int):
        """Show details for a case id (moderator-only)."""
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
        doc = mutes_col.find_one({"guild_id": ctx.guild.id, "case_id": case_id})
        if not doc:
            return await ctx.send(f"No case found for Case #{case_id}.")
        member_repr = f"<@{doc.get('user_id')}>"
        muted_by_repr = f"<@{doc.get('muted_by_id')}>"
        reason = doc.get("reason", "No reason")
        muted_at = doc.get("muted_at")
        muted_at_s = muted_at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(muted_at, datetime) else str(muted_at)
        active = doc.get("active", False)
        embed = discord.Embed(title=f"Case #{case_id}", color=discord.Color.blurple(), timestamp=utc_now())
        embed.add_field(name="Member", value=member_repr, inline=False)
        embed.add_field(name="Muted by", value=muted_by_repr, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Muted at", value=muted_at_s, inline=False)
        embed.add_field(name="Active", value=str(active), inline=False)
        if doc.get("expires_at"):
            embed.add_field(name="Expires at", value=doc["expires_at"].strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        if doc.get("unmuted_at"):
            embed.add_field(name="Unmuted at", value=doc["unmuted_at"].strftime("%Y-%m-%d %H:%M UTC"), inline=False)
        await ctx.send(embed=embed)

    # ---------------------------
    # Background auto-unmute loop (temporary mutes)
    # ---------------------------
    async def _auto_unmute_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
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
                        if member and muted_role:
                            try:
                                await member.remove_roles(muted_role, reason="Temporary mute expired")
                            except Exception:
                                pass
                        mutes_col.update_one({"_id": doc["_id"]}, {"$set": {"active": False, "unmuted_at": utc_now(), "unmuted_by_id": None}})
                        # log
                        case_id = doc.get("case_id", "N/A")
                        if log_ch:
                            embed = discord.Embed(title=f"Member Unmuted (auto) — Case #{case_id}", color=discord.Color.green(), timestamp=utc_now())
                            uid = doc.get("user_id")
                            embed.add_field(name="Member", value=f"<@{uid}>", inline=False)
                            embed.add_field(name="Reason", value="Temporary mute expired", inline=False)
                            try:
                                await log_ch.send(embed=embed)
                            except Exception:
                                pass
                    except Exception:
                        pass
                await asyncio.sleep(20)
            except Exception:
                await asyncio.sleep(10)

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
        has_mentions = bool(message.mentions or message.role_mentions or "@everyone" in message.content or "@here" in message.content)
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
        await ctx.send("An error occurred while executing the command.")

    def cog_unload(self):
        try:
            self._startup_task.cancel()
            self._unmute_task.cancel()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(ImprovedMuteCog(bot))
