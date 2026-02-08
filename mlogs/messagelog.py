from __future__ import annotations

import asyncio
import discord
from redbot.core import commands, Config
from redbot.core.utils.chat_formatting import humanize_number


class MessageLog(commands.Cog):
    """Logs deleted and edited messages to a configured channel, with ignore lists.

    Includes an optional audit-log based bypass to IGNORE deletions done by proxy bots
    like Tupperbox/PluralKit (so their “delete original message” behavior doesn’t spam logs).

    NOTE: Discord audit log entries often arrive after on_message_delete fires,
    so we sleep briefly before checking.
    """

    __author__ = "ChatGPT"
    __version__ = "1.3.1"

    # Public bot user IDs (from their invite client_id)
    DEFAULT_PROXY_DELETER_BOT_IDS = [
        431544605209788416,  # Tupperbox
        466378653216014359,  # PluralKit
    ]

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA11CE5E7, force_registration=True)

        default_guild = {
            "log_channel_id": None,
            "enabled": True,
            "log_bots": False,
            "max_content": 1500,
            "ignored_roles": [],
            "ignored_channels": [],
            "ignored_categories": [],
            "auto_ignore_log_channel": True,
            "ignore_proxy_deleter_bots": True,
            "proxy_deleter_bot_ids": self.DEFAULT_PROXY_DELETER_BOT_IDS.copy(),
        }
        self.config.register_guild(**default_guild)

    # ------------------------- Helpers -------------------------

    async def _get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        cid = await self.config.guild(guild).log_channel_id()
        if not cid:
            return None
        ch = guild.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    def _truncate(self, text: str, limit: int) -> str:
        text = text or ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    def _format_attachments(self, atts: list[discord.Attachment]) -> str:
        if not atts:
            return "None"
        shown = atts[:5]
        extra = len(atts) - len(shown)
        lines = [a.url for a in shown]
        if extra > 0:
            lines.append(f"...and {extra} more")
        return "\n".join(lines)

    async def _safe_send(self, channel: discord.TextChannel, embed: discord.Embed):
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _base_embed(self, *, title: str, color: discord.Color, author: discord.abc.User) -> discord.Embed:
        e = discord.Embed(title=title, color=color)
        e.set_author(name=f"{author} ({author.id})", icon_url=getattr(author.display_avatar, "url", None))
        return e

    async def _member_has_ignored_role(self, member: discord.Member) -> bool:
        ignored = await self.config.guild(member.guild).ignored_roles()
        if not ignored:
            return False
        member_role_ids = {r.id for r in member.roles}
        return any(rid in member_role_ids for rid in ignored)

    async def _location_is_ignored(self, channel: discord.abc.GuildChannel) -> bool:
        cfg = self.config.guild(channel.guild)

        ignored_channels = set(await cfg.ignored_channels())
        ignored_categories = set(await cfg.ignored_categories())
        auto_ignore_log = await cfg.auto_ignore_log_channel()

        # Auto-ignore the configured log channel itself
        if auto_ignore_log:
            log_channel_id = await cfg.log_channel_id()
            if log_channel_id and channel.id == log_channel_id:
                return True

        # Direct channel ignore
        if channel.id in ignored_channels:
            return True

        # Threads: ignore if their parent channel is ignored
        parent = getattr(channel, "parent", None)
        if parent and getattr(parent, "id", None) in ignored_channels:
            return True

        # Category ignore (threads inherit category via parent)
        category_id = getattr(channel, "category_id", None)
        if category_id is None and parent:
            category_id = getattr(parent, "category_id", None)

        return category_id in ignored_categories if category_id else False

    async def _deleted_by_ignored_proxy_bot(self, message: discord.Message) -> bool:
        """Best-effort: checks audit log to see if a configured bot deleted this user's message.

        Limitations:
        - Requires View Audit Log permission.
        - Audit log doesn’t include message ID, so this matches by (target user, channel, recency).
        - If many deletions happen at once, it can occasionally mismatch.
        """
        guild = message.guild
        if not guild or not message.author:
            return False

        cfg = self.config.guild(guild)
        if not await cfg.ignore_proxy_deleter_bots():
            return False

        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return False

        ignored_bot_ids = set(await cfg.proxy_deleter_bot_ids())
        if not ignored_bot_ids:
            return False

        # Audit log entries often lag behind the delete event
        await asyncio.sleep(1.2)

        now = discord.utils.utcnow()

        try:
            async for entry in guild.audit_logs(limit=15, action=discord.AuditLogAction.message_delete):
                # executor (deleter) must be one of the proxy bots
                if not entry.user or entry.user.id not in ignored_bot_ids:
                    continue

                # target must be the author whose message got deleted
                if not entry.target or getattr(entry.target, "id", None) != message.author.id:
                    continue

                extra = getattr(entry, "extra", None)
                ch = getattr(extra, "channel", None)
                if not ch or getattr(ch, "id", None) != message.channel.id:
                    continue

                # Timing window (audit log doesn't include message id)
                if entry.created_at:
                    age = abs((now - entry.created_at).total_seconds())
                    if age <= 20:
                        return True

        except (discord.Forbidden, discord.HTTPException):
            return False

        return False

    # ------------------------- Events -------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author is None:
            return

        # Ignore bots unless configured
        if message.author.bot and not await self.config.guild(message.guild).log_bots():
            return

        # Ignore proxy-bot deletions of user messages (Tupperbox/PluralKit style)
        if not message.author.bot:
            if await self._deleted_by_ignored_proxy_bot(message):
                return

        # Ignore locations (channels/categories/log channel)
        try:
            if await self._location_is_ignored(message.channel):
                return
        except Exception:
            pass

        # Ignore roles
        if isinstance(message.author, discord.Member):
            if await self._member_has_ignored_role(message.author):
                return

        # Check enabled + log channel
        if not await self.config.guild(message.guild).enabled():
            return
        log_channel = await self._get_log_channel(message.guild)
        if not log_channel:
            return

        max_content = await self.config.guild(message.guild).max_content()
        content = self._truncate(message.content or "", max_content)

        embed = self._base_embed(
            title="🗑️ Message Deleted",
            color=discord.Color.red(),
            author=message.author,
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        if message.created_at:
            embed.add_field(name="Created", value=discord.utils.format_dt(message.created_at, style="R"), inline=True)

        embed.add_field(
            name="Content",
            value=self._truncate(content.strip() or "*(no text content)*", 1024),
            inline=False,
        )
        embed.add_field(
            name="Attachments",
            value=self._truncate(self._format_attachments(message.attachments), 1024),
            inline=False,
        )

        # Reply context if available
        try:
            if message.reference and isinstance(message.reference.resolved, discord.Message):
                ref = message.reference.resolved
                embed.add_field(
                    name="In reply to",
                    value=f"[Jump to referenced message]({ref.jump_url}) by {ref.author.mention} in {ref.channel.mention}",
                    inline=False,
                )
        except Exception:
            pass

        embed.set_footer(text=f"Message ID: {message.id}")
        await self._safe_send(log_channel, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author is None:
            return

        # Ignore bots unless configured
        if after.author.bot and not await self.config.guild(after.guild).log_bots():
            return

        # Ignore embed-only edits, link preview updates, etc.
        if before.content == after.content and before.attachments == after.attachments:
            return

        # Ignore locations (channels/categories/log channel)
        try:
            if await self._location_is_ignored(after.channel):
                return
        except Exception:
            pass

        # Ignore roles
        if isinstance(after.author, discord.Member):
            if await self._member_has_ignored_role(after.author):
                return

        # Check enabled + log channel
        if not await self.config.guild(after.guild).enabled():
            return
        log_channel = await self._get_log_channel(after.guild)
        if not log_channel:
            return

        max_content = await self.config.guild(after.guild).max_content()
        before_content = self._truncate(before.content or "", max_content)
        after_content = self._truncate(after.content or "", max_content)

        embed = self._base_embed(
            title="✏️ Message Edited",
            color=discord.Color.gold(),
            author=after.author,
        )
        embed.add_field(name="Channel", value=after.channel.mention, inline=True)
        if after.edited_at:
            embed.add_field(name="Edited", value=discord.utils.format_dt(after.edited_at, style="R"), inline=True)
        embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})", inline=True)

        embed.add_field(
            name="Before",
            value=self._truncate(before_content.strip() or "*(no text content)*", 1024),
            inline=False,
        )
        embed.add_field(
            name="After",
            value=self._truncate(after_content.strip() or "*(no text content)*", 1024),
            inline=False,
        )

        if before.attachments != after.attachments:
            embed.add_field(
                name="Attachments (before)",
                value=self._truncate(self._format_attachments(before.attachments), 1024),
                inline=False,
            )
            embed.add_field(
                name="Attachments (after)",
                value=self._truncate(self._format_attachments(after.attachments), 1024),
                inline=False,
            )

        embed.set_footer(text=f"Message ID: {after.id}")
        await self._safe_send(log_channel, embed)

    # ------------------------- Commands -------------------------

    @commands.group(name="msglog")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def msglog(self, ctx: commands.Context):
        """Configure message edit/delete logging."""

    @msglog.command(name="channel")
    async def msglog_channel(self, ctx: commands.Context, channel: discord.TextChannel | None):
        """Set the log channel. Use without a channel to clear."""
        if channel is None:
            await self.config.guild(ctx.guild).log_channel_id.set(None)
            return await ctx.send("✅ Message log channel cleared.")
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(f"✅ Message logs will be sent to {channel.mention}.")

    @msglog.command(name="toggle")
    async def msglog_toggle(self, ctx: commands.Context):
        """Enable/disable logging."""
        cur = await self.config.guild(ctx.guild).enabled()
        new = not cur
        await self.config.guild(ctx.guild).enabled.set(new)
        await ctx.send(f"✅ Message logging is now **{'enabled' if new else 'disabled'}**.")

    @msglog.command(name="bots")
    async def msglog_bots(self, ctx: commands.Context, enabled: bool):
        """Whether to log bot messages (true/false)."""
        await self.config.guild(ctx.guild).log_bots.set(enabled)
        await ctx.send(f"✅ Logging bot messages: **{enabled}**.")

    @msglog.command(name="maxcontent")
    async def msglog_maxcontent(self, ctx: commands.Context, limit: int):
        """Max characters stored in embeds (100-4000)."""
        limit = max(100, min(limit, 4000))
        await self.config.guild(ctx.guild).max_content.set(limit)
        await ctx.send(f"✅ Max content length set to **{humanize_number(limit)}** characters.")

    # ----- Ignore Roles -----

    @msglog.command(name="ignorerole")
    async def msglog_ignorerole(self, ctx: commands.Context, role: discord.Role):
        """Add a role to ignore from logging."""
        roles = await self.config.guild(ctx.guild).ignored_roles()
        if role.id in roles:
            return await ctx.send("That role is already ignored.")
        roles.append(role.id)
        await self.config.guild(ctx.guild).ignored_roles.set(roles)
        await ctx.send(f"✅ Messages from members with {role.mention} will now be ignored.")

    @msglog.command(name="unignorerole")
    async def msglog_unignorerole(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from the ignore list."""
        roles = await self.config.guild(ctx.guild).ignored_roles()
        if role.id not in roles:
            return await ctx.send("That role isn't ignored.")
        roles.remove(role.id)
        await self.config.guild(ctx.guild).ignored_roles.set(roles)
        await ctx.send(f"✅ {role.mention} removed from ignore list.")

    @msglog.command(name="ignoredroles")
    async def msglog_ignoredroles(self, ctx: commands.Context):
        """Show ignored roles."""
        roles = await self.config.guild(ctx.guild).ignored_roles()
        if not roles:
            return await ctx.send("No ignored roles set.")
        mentions = [r.mention for rid in roles if (r := ctx.guild.get_role(rid))]
        await ctx.send("Ignored roles:\n" + (", ".join(mentions) if mentions else "None."))

    # ----- Ignore Channels -----

    @msglog.command(name="ignorechannel")
    async def msglog_ignorechannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Ignore a channel from message logging."""
        chans = await self.config.guild(ctx.guild).ignored_channels()
        if channel.id in chans:
            return await ctx.send("That channel is already ignored.")
        chans.append(channel.id)
        await self.config.guild(ctx.guild).ignored_channels.set(chans)
        await ctx.send(f"✅ Messages in {channel.mention} will now be ignored.")

    @msglog.command(name="unignorechannel")
    async def msglog_unignorechannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Remove a channel from the ignore list."""
        chans = await self.config.guild(ctx.guild).ignored_channels()
        if channel.id not in chans:
            return await ctx.send("That channel isn't ignored.")
        chans.remove(channel.id)
        await self.config.guild(ctx.guild).ignored_channels.set(chans)
        await ctx.send(f"✅ {channel.mention} removed from ignore list.")

    @msglog.command(name="ignoredchannels")
    async def msglog_ignoredchannels(self, ctx: commands.Context):
        """Show ignored channels."""
        chans = await self.config.guild(ctx.guild).ignored_channels()
        if not chans:
            return await ctx.send("No ignored channels set.")
        mentions = []
        for cid in chans:
            ch = ctx.guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                mentions.append(ch.mention)
        await ctx.send("Ignored channels:\n" + (", ".join(mentions) if mentions else "None."))

    # ----- Ignore Categories -----

    @msglog.command(name="ignorecategory")
    async def msglog_ignorecategory(self, ctx: commands.Context, category: discord.CategoryChannel):
        """Ignore a whole category from message logging."""
        cats = await self.config.guild(ctx.guild).ignored_categories()
        if category.id in cats:
            return await ctx.send("That category is already ignored.")
        cats.append(category.id)
        await self.config.guild(ctx.guild).ignored_categories.set(cats)
        await ctx.send(f"✅ Messages in **{category.name}** will now be ignored.")

    @msglog.command(name="unignorecategory")
    async def msglog_unignorecategory(self, ctx: commands.Context, category: discord.CategoryChannel):
        """Remove a category from the ignore list."""
        cats = await self.config.guild(ctx.guild).ignored_categories()
        if category.id not in cats:
            return await ctx.send("That category isn't ignored.")
        cats.remove(category.id)
        await self.config.guild(ctx.guild).ignored_categories.set(cats)
        await ctx.send(f"✅ **{category.name}** removed from ignore list.")

    @msglog.command(name="ignoredcategories")
    async def msglog_ignoredcategories(self, ctx: commands.Context):
        """Show ignored categories."""
        cats = await self.config.guild(ctx.guild).ignored_categories()
        if not cats:
            return await ctx.send("No ignored categories set.")
        names = [cat.name for cid in cats if isinstance((cat := ctx.guild.get_channel(cid)), discord.CategoryChannel)]
        await ctx.send("Ignored categories:\n" + ("\n".join(f"• {n}" for n in names) if names else "None."))

    # ----- Auto-ignore log channel -----

    @msglog.command(name="autologignore")
    async def msglog_autologignore(self, ctx: commands.Context, enabled: bool):
        """Auto-ignore the configured log channel itself (true/false)."""
        await self.config.guild(ctx.guild).auto_ignore_log_channel.set(enabled)
        await ctx.send(f"✅ Auto-ignore log channel: **{enabled}**.")

    # ----- Proxy deleter ignore (Tupperbox/PluralKit behavior) -----

    @msglog.command(name="ignoreproxydeletes")
    async def msglog_ignoreproxydeletes(self, ctx: commands.Context, enabled: bool):
        """Ignore deletions when the audit log shows a configured bot deleted the user's message (true/false)."""
        await self.config.guild(ctx.guild).ignore_proxy_deleter_bots.set(enabled)
        await ctx.send(f"✅ Ignore proxy-bot deletions: **{enabled}**.")

    @msglog.command(name="proxydeleterbots")
    async def msglog_proxydeleterbots(self, ctx: commands.Context):
        """List bot IDs treated as 'proxy deleter bots'."""
        ids = await self.config.guild(ctx.guild).proxy_deleter_bot_ids()
        if not ids:
            return await ctx.send("No proxy deleter bots configured.")
        await ctx.send("Proxy deleter bot IDs:\n" + "\n".join(f"• `{i}`" for i in ids))

    @msglog.command(name="addproxydeleterbot")
    async def msglog_addproxydeleterbot(self, ctx: commands.Context, bot_id: int):
        """Add a bot ID to the proxy deleter list."""
        ids = await self.config.guild(ctx.guild).proxy_deleter_bot_ids()
        if bot_id in ids:
            return await ctx.send("That bot ID is already in the list.")
        ids.append(bot_id)
        await self.config.guild(ctx.guild).proxy_deleter_bot_ids.set(ids)
        await ctx.send(f"✅ Added `{bot_id}` to proxy deleter bots.")

    @msglog.command(name="removeproxydeleterbot")
    async def msglog_removeproxydeleterbot(self, ctx: commands.Context, bot_id: int):
        """Remove a bot ID from the proxy deleter list."""
        ids = await self.config.guild(ctx.guild).proxy_deleter_bot_ids()
        if bot_id not in ids:
            return await ctx.send("That bot ID isn't in the list.")
        ids.remove(bot_id)
        await self.config.guild(ctx.guild).proxy_deleter_bot_ids.set(ids)
        await ctx.send(f"✅ Removed `{bot_id}` from proxy deleter bots.")

    # ----- Settings -----

    @msglog.command(name="settings")
    async def msglog_settings(self, ctx: commands.Context):
        """Show current settings."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data["log_channel_id"]) if data["log_channel_id"] else None

        role_mentions = [r.mention for rid in data.get("ignored_roles", []) if (r := ctx.guild.get_role(rid))]
        roles_value = ", ".join(role_mentions) if role_mentions else "None"

        channel_mentions = []
        for cid in data.get("ignored_channels", []):
            ch = ctx.guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                channel_mentions.append(ch.mention)
        chans_value = ", ".join(channel_mentions) if channel_mentions else "None"

        cat_names = [
            cat.name
            for cat_id in data.get("ignored_categories", [])
            if isinstance((cat := ctx.guild.get_channel(cat_id)), discord.CategoryChannel)
        ]
        cats_value = "\n".join(f"• {n}" for n in cat_names) if cat_names else "None"

        embed = discord.Embed(title="MessageLog Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(data["enabled"]), inline=True)
        embed.add_field(name="Log bots", value=str(data["log_bots"]), inline=True)
        embed.add_field(name="Max content", value=str(data["max_content"]), inline=True)
        embed.add_field(name="Auto-ignore log channel", value=str(data.get("auto_ignore_log_channel", True)), inline=True)
        embed.add_field(name="Log channel", value=channel.mention if channel else "Not set", inline=False)

        embed.add_field(name="Ignored roles", value=self._truncate(roles_value, 1024), inline=False)
        embed.add_field(name="Ignored channels", value=self._truncate(chans_value, 1024), inline=False)
        embed.add_field(name="Ignored categories", value=self._truncate(cats_value, 1024), inline=False)

        embed.add_field(
            name="Ignore proxy-bot deletions",
            value=str(data.get("ignore_proxy_deleter_bots", True)),
            inline=True,
        )
        embed.add_field(
            name="Proxy deleter bot IDs",
            value=self._truncate(", ".join(str(i) for i in data.get("proxy_deleter_bot_ids", [])) or "None", 1024),
            inline=False,
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(MessageLog(bot))
