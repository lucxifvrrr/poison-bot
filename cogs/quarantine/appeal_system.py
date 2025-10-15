import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple

from .config import (
    AppealStatus, AppealType, Colors,
    APPEAL_COOLDOWN_HOURS, MAX_APPEAL_LENGTH, APPEAL_REVIEW_TIMEOUT_DAYS
)

load_dotenv()
MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise RuntimeError("MONGO_URL missing from environment (.env)")

# Connect to MongoDB
mongo = MongoClient(
    MONGO_URL,
    maxPoolSize=50,
    minPoolSize=10,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000
)
db = mongo.get_database("discord_mute_system")

# Collections
appeals_col = db.appeals
guild_configs = db.guild_configs
mutes_col = db.mutes
appeal_messages = db.appeal_messages

# Create indexes
try:
    appeals_col.create_index([("guild_id", ASCENDING), ("user_id", ASCENDING)])
    appeals_col.create_index([("appeal_id", ASCENDING)], unique=True)
    appeals_col.create_index([("status", ASCENDING)])
    appeals_col.create_index([("created_at", ASCENDING)])
    appeal_messages.create_index([("appeal_id", ASCENDING)])
except Exception:
    pass


def utc_now():
    """Get current UTC time."""
    return datetime.now(timezone.utc)


class AppealModal(discord.ui.Modal, title="Submit Appeal"):
    """Modal for submitting an appeal."""
    
    appeal_reason = discord.ui.TextInput(
        label="Why should your punishment be removed?",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why you believe your punishment should be lifted...",
        required=True,
        max_length=MAX_APPEAL_LENGTH,
        min_length=50
    )
    
    additional_info = discord.ui.TextInput(
        label="Additional Information (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Any additional context or information...",
        required=False,
        max_length=500
    )
    
    def __init__(self, cog, case_id: int):
        super().__init__()
        self.cog = cog
        self.case_id = case_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        success, message, appeal_id = await self.cog._create_appeal(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            case_id=self.case_id,
            reason=self.appeal_reason.value,
            additional_info=self.additional_info.value if self.additional_info.value else None
        )
        
        if success:
            embed = discord.Embed(
                title="✅ Appeal Submitted Successfully",
                description=f"Your appeal has been submitted and is now pending review.",
                color=Colors.SUCCESS,
                timestamp=utc_now()
            )
            embed.add_field(name="📋 Appeal ID", value=f"#{appeal_id}", inline=True)
            embed.add_field(name="📋 Case ID", value=f"#{self.case_id}", inline=True)
            embed.add_field(name="⏰ Status", value="🟡 Pending Review", inline=True)
            embed.add_field(
                name="📝 Your Appeal",
                value=f"```{self.appeal_reason.value[:500]}```",
                inline=False
            )
            embed.set_footer(text="Staff will review your appeal soon")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="❌ Appeal Submission Failed",
                description=message,
                color=Colors.ERROR,
                timestamp=utc_now()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


class AppealReviewView(discord.ui.View):
    """View for reviewing appeals with approve/deny buttons."""
    
    def __init__(self, cog, appeal_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.appeal_id = appeal_id
    
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_review(interaction, AppealStatus.APPROVED)
    
    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_review(interaction, AppealStatus.DENIED)
    
    @discord.ui.button(label="View Details", style=discord.ButtonStyle.secondary, emoji="📋")
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        appeal = appeals_col.find_one({"appeal_id": self.appeal_id})
        if not appeal:
            await interaction.followup.send("❌ Appeal not found.", ephemeral=True)
            return
        
        embed = await self.cog._create_appeal_details_embed(appeal)
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def _handle_review(self, interaction: discord.Interaction, status: str):
        cfg = guild_configs.find_one({"guild_id": interaction.guild.id})
        if not cfg:
            await interaction.response.send_message("❌ System not configured.", ephemeral=True)
            return
        
        allowed = interaction.user.guild_permissions.administrator
        if not allowed:
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                role = interaction.guild.get_role(mod_role_id)
                if role and role in interaction.user.roles:
                    allowed = True
        
        if not allowed:
            await interaction.response.send_message("❌ You don't have permission to review appeals.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        success, message = await self.cog._process_appeal_review(
            appeal_id=self.appeal_id,
            reviewer_id=interaction.user.id,
            status=status,
            guild=interaction.guild
        )
        
        if success:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label != "View Details":
                    item.disabled = True
            
            try:
                await interaction.message.edit(view=self)
            except:
                pass
            
            status_text = "✅ Approved" if status == AppealStatus.APPROVED else "❌ Denied"
            embed = discord.Embed(
                title=f"{status_text}",
                description=message,
                color=Colors.SUCCESS if status == AppealStatus.APPROVED else Colors.ERROR,
                timestamp=utc_now()
            )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)


class AppealSystem(commands.Cog):
    """Modern appeal system for quarantine/mute punishments."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.logger = getattr(bot, "logger", None)
        self._appeal_counter_lock = asyncio.Lock()
        self._cleanup_task = self.bot.loop.create_task(self._startup_work())
        self._expire_appeals_loop.start()
    
    def _log(self, *args, **kwargs):
        """Log helper."""
        if self.logger:
            self.logger.info(*args, **kwargs)
    
    async def _startup_work(self):
        """Initialize on startup."""
        await self.bot.wait_until_ready()
        self._log("Appeal system initialized")
    
    def _next_appeal_id(self, guild_id: int) -> int:
        """Generate next appeal ID for guild."""
        try:
            res = db.appeal_counters.find_one_and_update(
                {"guild_id": guild_id},
                {"$inc": {"appeal_id": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            return int(res.get("appeal_id", 1))
        except PyMongoError:
            doc = db.appeal_counters.find_one({"guild_id": guild_id})
            if not doc:
                db.appeal_counters.insert_one({"guild_id": guild_id, "appeal_id": 1})
                return 1
            else:
                nxt = doc.get("appeal_id", 0) + 1
                db.appeal_counters.update_one({"guild_id": guild_id}, {"$set": {"appeal_id": nxt}})
                return nxt
    
    async def _can_submit_appeal(self, guild_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
        """Check if user can submit an appeal."""
        cooldown_time = utc_now() - timedelta(hours=APPEAL_COOLDOWN_HOURS)
        recent = appeals_col.find_one({
            "guild_id": guild_id,
            "user_id": user_id,
            "created_at": {"$gte": cooldown_time}
        })
        
        if recent:
            time_left = (recent["created_at"] + timedelta(hours=APPEAL_COOLDOWN_HOURS) - utc_now()).total_seconds()
            hours = int(time_left // 3600)
            minutes = int((time_left % 3600) // 60)
            return False, f"You must wait {hours}h {minutes}m before submitting another appeal."
        
        pending = appeals_col.find_one({
            "guild_id": guild_id,
            "user_id": user_id,
            "status": AppealStatus.PENDING
        })
        
        if pending:
            return False, f"You already have a pending appeal (#{pending['appeal_id']}). Please wait for it to be reviewed."
        
        return True, None
    
    async def _create_appeal(
        self,
        guild_id: int,
        user_id: int,
        case_id: int,
        reason: str,
        additional_info: Optional[str] = None
    ) -> Tuple[bool, str, Optional[int]]:
        """Create a new appeal."""
        can_submit, error_msg = await self._can_submit_appeal(guild_id, user_id)
        if not can_submit:
            return False, error_msg, None
        
        case = mutes_col.find_one({"guild_id": guild_id, "case_id": case_id})
        if not case:
            return False, f"Case #{case_id} not found.", None
        
        if case["user_id"] != user_id:
            return False, "You can only appeal your own cases.", None
        
        if not case.get("active", False):
            return False, "This case is no longer active.", None
        
        appeal_id = self._next_appeal_id(guild_id)
        
        appeal_doc = {
            "appeal_id": appeal_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "case_id": case_id,
            "reason": reason,
            "additional_info": additional_info,
            "status": AppealStatus.PENDING,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "reviewed_by_id": None,
            "reviewed_at": None,
            "review_note": None
        }
        
        try:
            appeals_col.insert_one(appeal_doc)
        except PyMongoError as e:
            self._log(f"Failed to create appeal: {e}")
            return False, "Failed to create appeal. Please try again.", None
        
        await self._notify_moderators_new_appeal(guild_id, appeal_id)
        
        return True, "Appeal submitted successfully!", appeal_id
    
    async def _notify_moderators_new_appeal(self, guild_id: int, appeal_id: int):
        """Send notification to moderators about new appeal."""
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return
            
            cfg = guild_configs.find_one({"guild_id": guild_id})
            if not cfg:
                return
            
            log_channel_id = cfg.get("log_channel_id")
            if not log_channel_id:
                return
            
            log_channel = guild.get_channel(log_channel_id)
            if not log_channel:
                return
            
            appeal = appeals_col.find_one({"appeal_id": appeal_id})
            if not appeal:
                return
            
            user = await self.bot.fetch_user(appeal["user_id"])
            case_id = appeal["case_id"]
            
            embed = discord.Embed(
                title="📨 New Appeal Submitted",
                description=f"A new appeal has been submitted and requires review.",
                color=Colors.PENDING,
                timestamp=utc_now()
            )
            embed.add_field(name="📋 Appeal ID", value=f"#{appeal_id}", inline=True)
            embed.add_field(name="📋 Case ID", value=f"#{case_id}", inline=True)
            embed.add_field(name="👤 User", value=f"{user.mention}\n`{user} ({user.id})`", inline=True)
            embed.add_field(
                name="📝 Appeal Reason",
                value=f"```{appeal['reason'][:500]}```",
                inline=False
            )
            
            if appeal.get("additional_info"):
                embed.add_field(
                    name="ℹ️ Additional Info",
                    value=f"```{appeal['additional_info'][:300]}```",
                    inline=False
                )
            
            embed.set_footer(text="Use the buttons below to review this appeal")
            
            view = AppealReviewView(self, appeal_id)
            message = await log_channel.send(embed=embed, view=view)
            
            appeal_messages.insert_one({
                "appeal_id": appeal_id,
                "message_id": message.id,
                "channel_id": log_channel.id,
                "guild_id": guild_id
            })
            
        except Exception as e:
            self._log(f"Failed to notify moderators: {e}")
    
    async def _process_appeal_review(
        self,
        appeal_id: int,
        reviewer_id: int,
        status: str,
        guild: discord.Guild
    ) -> Tuple[bool, str]:
        """Process an appeal review (approve/deny)."""
        appeal = appeals_col.find_one({"appeal_id": appeal_id})
        if not appeal:
            return False, "Appeal not found."
        
        if appeal["status"] != AppealStatus.PENDING:
            return False, f"This appeal has already been {appeal['status']}."
        
        try:
            appeals_col.update_one(
                {"appeal_id": appeal_id},
                {
                    "$set": {
                        "status": status,
                        "reviewed_by_id": reviewer_id,
                        "reviewed_at": utc_now(),
                        "updated_at": utc_now()
                    }
                }
            )
        except PyMongoError as e:
            self._log(f"Failed to update appeal: {e}")
            return False, "Failed to update appeal status."
        
        if status == AppealStatus.APPROVED:
            success = await self._unmute_user_from_appeal(guild, appeal)
            if not success:
                return False, "Appeal approved but failed to unmute user. Please unmute manually."
        
        await self._notify_user_appeal_result(guild, appeal, status, reviewer_id)
        
        action = "approved" if status == AppealStatus.APPROVED else "denied"
        return True, f"Appeal #{appeal_id} has been {action}."
    
    async def _unmute_user_from_appeal(self, guild: discord.Guild, appeal: dict) -> bool:
        """Unmute user when appeal is approved."""
        try:
            cfg = guild_configs.find_one({"guild_id": guild.id})
            if not cfg:
                return False
            
            muted_role = guild.get_role(cfg.get("muted_role_id"))
            if not muted_role:
                return False
            
            member = guild.get_member(appeal["user_id"])
            if not member:
                try:
                    member = await guild.fetch_member(appeal["user_id"])
                except:
                    return False
            
            if not member:
                return False
            
            await member.remove_roles(muted_role, reason=f"Appeal #{appeal['appeal_id']} approved")
            
            mutes_col.update_many(
                {"guild_id": guild.id, "user_id": appeal["user_id"], "active": True},
                {
                    "$set": {
                        "active": False,
                        "unmuted_at": utc_now(),
                        "unmuted_by_id": appeal["reviewed_by_id"],
                        "unmute_reason": f"Appeal #{appeal['appeal_id']} approved"
                    }
                }
            )
            
            return True
        except Exception as e:
            self._log(f"Failed to unmute user from appeal: {e}")
            return False
    
    async def _notify_user_appeal_result(
        self,
        guild: discord.Guild,
        appeal: dict,
        status: str,
        reviewer_id: int
    ):
        """Notify user about appeal result."""
        try:
            user = await self.bot.fetch_user(appeal["user_id"])
            if not user:
                return
            
            reviewer = await self.bot.fetch_user(reviewer_id)
            
            if status == AppealStatus.APPROVED:
                embed = discord.Embed(
                    title="✅ Appeal Approved",
                    description=f"Your appeal for Case #{appeal['case_id']} has been **approved**!",
                    color=Colors.SUCCESS,
                    timestamp=utc_now()
                )
                embed.add_field(name="🎉 Result", value="Your punishment has been lifted.", inline=False)
            else:
                embed = discord.Embed(
                    title="❌ Appeal Denied",
                    description=f"Your appeal for Case #{appeal['case_id']} has been **denied**.",
                    color=Colors.ERROR,
                    timestamp=utc_now()
                )
                embed.add_field(
                    name="📋 Note",
                    value="Your punishment remains in effect. You may submit another appeal after the cooldown period.",
                    inline=False
                )
            
            embed.add_field(name="🏢 Server", value=guild.name, inline=True)
            embed.add_field(name="📋 Appeal ID", value=f"#{appeal['appeal_id']}", inline=True)
            embed.add_field(name="🛡️ Reviewed By", value=f"{reviewer}", inline=True)
            embed.set_footer(text=f"Reviewed at {utc_now().strftime('%Y-%m-%d %H:%M UTC')}")
            
            dm = await user.create_dm()
            await dm.send(embed=embed)
        except Exception as e:
            self._log(f"Failed to notify user: {e}")
    
    async def _create_appeal_details_embed(self, appeal: dict) -> discord.Embed:
        """Create detailed embed for an appeal."""
        user = await self.bot.fetch_user(appeal["user_id"])
        case = mutes_col.find_one({"guild_id": appeal["guild_id"], "case_id": appeal["case_id"]})
        
        status_emoji = {
            AppealStatus.PENDING: "🟡",
            AppealStatus.APPROVED: "✅",
            AppealStatus.DENIED: "❌",
            AppealStatus.EXPIRED: "⏰"
        }.get(appeal["status"], "❓")
        
        color = {
            AppealStatus.PENDING: Colors.PENDING,
            AppealStatus.APPROVED: Colors.SUCCESS,
            AppealStatus.DENIED: Colors.ERROR,
            AppealStatus.EXPIRED: Colors.WARNING
        }.get(appeal["status"], Colors.INFO)
        
        embed = discord.Embed(
            title=f"📋 Appeal #{appeal['appeal_id']} Details",
            description=f"Status: {status_emoji} **{appeal['status'].title()}**",
            color=color,
            timestamp=utc_now()
        )
        
        embed.add_field(name="👤 User", value=f"{user.mention}\n`{user}`", inline=True)
        embed.add_field(name="📋 Case ID", value=f"#{appeal['case_id']}", inline=True)
        embed.add_field(name="📅 Submitted", value=f"<t:{int(appeal['created_at'].timestamp())}:R>", inline=True)
        
        if case:
            embed.add_field(
                name="⚖️ Original Reason",
                value=f"```{case.get('reason', 'N/A')[:200]}```",
                inline=False
            )
        
        embed.add_field(
            name="📝 Appeal Reason",
            value=f"```{appeal['reason'][:500]}```",
            inline=False
        )
        
        if appeal.get("additional_info"):
            embed.add_field(
                name="ℹ️ Additional Info",
                value=f"```{appeal['additional_info'][:300]}```",
                inline=False
            )
        
        if appeal.get("reviewed_by_id"):
            reviewer = await self.bot.fetch_user(appeal["reviewed_by_id"])
            embed.add_field(name="🛡️ Reviewed By", value=f"{reviewer.mention}", inline=True)
            embed.add_field(
                name="📅 Reviewed At",
                value=f"<t:{int(appeal['reviewed_at'].timestamp())}:R>",
                inline=True
            )
        
        return embed
    
    @app_commands.command(name="appeal", description="Submit an appeal for your mute/punishment")
    @app_commands.describe(case_id="The case ID you want to appeal")
    async def appeal_command(self, interaction: discord.Interaction, case_id: int):
        """Submit an appeal for a punishment."""
        if case_id <= 0:
            await interaction.response.send_message("❌ Invalid case ID.", ephemeral=True)
            return
        
        case = mutes_col.find_one({
            "guild_id": interaction.guild.id,
            "case_id": case_id
        })
        
        if not case:
            await interaction.response.send_message(
                f"❌ Case #{case_id} not found.",
                ephemeral=True
            )
            return
        
        if case["user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "❌ You can only appeal your own cases.",
                ephemeral=True
            )
            return
        
        if not case.get("active", False):
            await interaction.response.send_message(
                "❌ This case is no longer active.",
                ephemeral=True
            )
            return
        
        can_submit, error_msg = await self._can_submit_appeal(
            interaction.guild.id,
            interaction.user.id
        )
        
        if not can_submit:
            embed = discord.Embed(
                title="❌ Cannot Submit Appeal",
                description=error_msg,
                color=Colors.ERROR,
                timestamp=utc_now()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        modal = AppealModal(self, case_id)
        await interaction.response.send_modal(modal)
    
    @app_commands.command(name="appeal-status", description="Check the status of your appeal")
    @app_commands.describe(appeal_id="The appeal ID to check (optional)")
    async def appeal_status(self, interaction: discord.Interaction, appeal_id: Optional[int] = None):
        """Check appeal status."""
        if appeal_id:
            appeal = appeals_col.find_one({
                "guild_id": interaction.guild.id,
                "appeal_id": appeal_id,
                "user_id": interaction.user.id
            })
            
            if not appeal:
                await interaction.response.send_message(
                    f"❌ Appeal #{appeal_id} not found or doesn't belong to you.",
                    ephemeral=True
                )
                return
            
            embed = await self._create_appeal_details_embed(appeal)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            appeals = list(appeals_col.find({
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id
            }).sort("created_at", DESCENDING).limit(5))
            
            if not appeals:
                await interaction.response.send_message(
                    "❌ You have no appeals in this server.",
                    ephemeral=True
                )
                return
            
            embed = discord.Embed(
                title="📋 Your Appeals",
                description=f"Showing your last {len(appeals)} appeal(s)",
                color=Colors.INFO,
                timestamp=utc_now()
            )
            
            for appeal in appeals:
                status_emoji = {
                    AppealStatus.PENDING: "🟡",
                    AppealStatus.APPROVED: "✅",
                    AppealStatus.DENIED: "❌",
                    AppealStatus.EXPIRED: "⏰"
                }.get(appeal["status"], "❓")
                
                embed.add_field(
                    name=f"{status_emoji} Appeal #{appeal['appeal_id']} - Case #{appeal['case_id']}",
                    value=f"**Status:** {appeal['status'].title()}\n**Submitted:** <t:{int(appeal['created_at'].timestamp())}:R>",
                    inline=False
                )
            
            embed.set_footer(text="Use /appeal-status <appeal_id> for detailed information")
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="appeal-list", description="[MOD] List all pending appeals")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def appeal_list(self, interaction: discord.Interaction):
        """List all pending appeals (moderator only)."""
        appeals = list(appeals_col.find({
            "guild_id": interaction.guild.id,
            "status": AppealStatus.PENDING
        }).sort("created_at", ASCENDING).limit(10))
        
        if not appeals:
            await interaction.response.send_message(
                "✅ No pending appeals!",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="📨 Pending Appeals",
            description=f"Showing {len(appeals)} pending appeal(s)",
            color=Colors.PENDING,
            timestamp=utc_now()
        )
        
        for appeal in appeals:
            user = await self.bot.fetch_user(appeal["user_id"])
            embed.add_field(
                name=f"Appeal #{appeal['appeal_id']} - Case #{appeal['case_id']}",
                value=f"**User:** {user.mention}\n**Submitted:** <t:{int(appeal['created_at'].timestamp())}:R>\n**Reason:** {appeal['reason'][:100]}...",
                inline=False
            )
        
        embed.set_footer(text="Use /appeal-review <appeal_id> to review an appeal")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="appeal-review", description="[MOD] Review a specific appeal")
    @app_commands.describe(appeal_id="The appeal ID to review")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def appeal_review(self, interaction: discord.Interaction, appeal_id: int):
        """Review a specific appeal (moderator only)."""
        appeal = appeals_col.find_one({
            "guild_id": interaction.guild.id,
            "appeal_id": appeal_id
        })
        
        if not appeal:
            await interaction.response.send_message(
                f"❌ Appeal #{appeal_id} not found.",
                ephemeral=True
            )
            return
        
        embed = await self._create_appeal_details_embed(appeal)
        
        if appeal["status"] == AppealStatus.PENDING:
            view = AppealReviewView(self, appeal_id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @tasks.loop(hours=6)
    async def _expire_appeals_loop(self):
        """Expire old pending appeals."""
        try:
            cutoff = utc_now() - timedelta(days=APPEAL_REVIEW_TIMEOUT_DAYS)
            result = appeals_col.update_many(
                {
                    "status": AppealStatus.PENDING,
                    "created_at": {"$lt": cutoff}
                },
                {
                    "$set": {
                        "status": AppealStatus.EXPIRED,
                        "updated_at": utc_now()
                    }
                }
            )
            
            if result.modified_count > 0:
                self._log(f"Expired {result.modified_count} old appeals")
        except Exception as e:
            self._log(f"Error expiring appeals: {e}")
    
    @_expire_appeals_loop.before_loop
    async def _before_expire_loop(self):
        await self.bot.wait_until_ready()
    
    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        try:
            self._cleanup_task.cancel()
        except Exception:
            pass
        try:
            self._expire_appeals_loop.cancel()
        except Exception:
            pass
        try:
            mongo.close()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AppealSystem(bot))
