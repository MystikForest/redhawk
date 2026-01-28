# redhawk.py
# Red-DiscordBot cog: Red Hawk Westmarch calendar + deterministic daily weather
# + "usually right" forecast + alternating-year holidays + autopost (7 AM Pacific) + post-now command.
#
# Player commands:
#   [p]date
#   [p]weather [offset]
#   [p]forecast [days]   (default 10, max 10)
#
# Admin commands:
#   [p]wmautopost #channel
#   [p]wmautopost off
#   [p]wmpostnow
#   [p]wmpostnow here
#   [p]wmsetepoch YYYY-MM-DD [ig_day_number]
#   [p]wmlocation <text>
#   [p]wmweekday [true/false]
#   [p]wmsetmonthnames name1, ... (12)
#   [p]wmsetweekdaynames name1, ... (10)
#   [p]wmnamesreset
#
# Time:
# - In-game day rolls over at midnight Pacific (America/Los_Angeles).
# - Autopost runs daily at 7:00 AM Pacific.

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from redbot.core import Config, commands

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


# =============================
# Canonical calendar names
# =============================

DEFAULT_MONTH_NAMES = [
    "Icehold", "Frostwane", "Rainrich", "Lightwake", "Sunswell", "Greenflux",
    "Sunfade", "Stormreign", "Amberfell", "Auburncrown", "Icefleet", "Frostcrest",
]

DEFAULT_WEEKDAY_NAMES = [
    "Solies", "Halos", "Incedis", "Talis", "Inanos",
    "Penumus", "Oris", "Neptis", "Anaemis", "Extos",
]


# =============================
# Holidays (alternating years)
# odd years -> middle of odd months
# even years -> middle of even months
# =============================

HOLIDAY_DAY = 15

ODD_YEAR_HOLIDAYS: Dict[int, str] = {
    1: "Hearthmend Eve",
    3: "Mossbirth Day",
    5: "Embershare Day",
    7: "Deep Toll",
    9: "Honeyguard Day",
    11: "Quietbound Night",
}

EVEN_YEAR_HOLIDAYS: Dict[int, str] = {
    2: "River’s Wake",
    4: "Lanternveil Night",
    6: "Milkmoon Festival",
    8: "Bloomstride Parade",
    10: "Rainsong Carnival",
    12: "Wishfrost Morning",
}


# =============================
# Calendar math
# =============================

DAYS_PER_WEEK = 10
WEEKS_PER_MONTH = 3
BASE_DAYS_PER_MONTH = DAYS_PER_WEEK * WEEKS_PER_MONTH  # 30
MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class InGameDate:
    year: int
    month: int  # 1-12
    day: int
    day_of_year: int  # 1..365/366

    @property
    def week(self) -> int:
        return ((self.day_of_year - 1) // DAYS_PER_WEEK) + 1

    @property
    def weekday(self) -> int:
        return ((self.day_of_year - 1) % DAYS_PER_WEEK) + 1  # 1..10


def is_leap_year(year: int) -> bool:
    return year % 4 == 0


def year_length(year: int) -> int:
    return 366 if is_leap_year(year) else 365


def month_lengths_for_year(year: int) -> List[int]:
    lengths = [BASE_DAYS_PER_MONTH] * MONTHS_PER_YEAR
    extras = 6 if is_leap_year(year) else 5
    for m in [2, 4, 6, 8, 10, 12][:extras]:
        lengths[m - 1] += 1
    return lengths


def day_number_to_ingame(day_number: int) -> InGameDate:
    if day_number < 1:
        raise ValueError("day_number must be >= 1")

    y = 1
    remaining = day_number
    while remaining > year_length(y):
        remaining -= year_length(y)
        y += 1

    lengths = month_lengths_for_year(y)
    m = 1
    while remaining > lengths[m - 1]:
        remaining -= lengths[m - 1]
        m += 1

    d = remaining
    doy = day_number - sum(year_length(yy) for yy in range(1, y))
    return InGameDate(year=y, month=m, day=d, day_of_year=doy)


# =============================
# Weather generation (truth)
# =============================

def season_for_month(month: int) -> str:
    if month in (1, 2, 3):
        return "Winter"
    if month in (4, 5, 6):
        return "Spring"
    if month in (7, 8, 9):
        return "Summer"
    return "Autumn"


WEATHER_TABLE: Dict[str, List[Tuple[str, int]]] = {
    "Winter": [
        ("Clear, bitter cold", 20),
        ("Overcast and freezing", 25),
        ("Snow flurries", 25),
        ("Heavy snowfall", 15),
        ("Sleeting rain", 10),
        ("Howling windstorm", 5),
    ],
    "Spring": [
        ("Crisp and clear", 20),
        ("Mild, scattered clouds", 25),
        ("Light rain", 25),
        ("Steady rain", 15),
        ("Thunderstorm", 10),
        ("Foggy morning", 5),
    ],
    "Summer": [
        ("Bright and hot", 30),
        ("Warm with scattered clouds", 25),
        ("Humid haze", 15),
        ("Brief afternoon rain", 15),
        ("Thunderstorm", 10),
        ("Oppressive heatwave", 5),
    ],
    "Autumn": [
        ("Cool and clear", 25),
        ("Breezy, drifting clouds", 25),
        ("Light rain", 20),
        ("Chill drizzle", 15),
        ("Foggy", 10),
        ("Gusty windstorm", 5),
    ],
}

BIOME_BIASES: Dict[str, Dict[str, Dict[str, int]]] = {
    "coast": {
        "Winter": {
            "Sleeting rain": +8,
            "Snow flurries": -6,
            "Heavy snowfall": -6,
            "Howling windstorm": +4,
            "Overcast and freezing": +2,
        },
        "Spring": {
            "Light rain": +6,
            "Steady rain": +3,
            "Foggy morning": +3,
            "Crisp and clear": -2,
        },
        "Summer": {
            "Humid haze": +6,
            "Brief afternoon rain": +3,
            "Thunderstorm": +2,
            "Bright and hot": -4,
        },
        "Autumn": {
            "Foggy": +6,
            "Light rain": +3,
            "Chill drizzle": +3,
            "Cool and clear": -3,
            "Gusty windstorm": +2,
        },
    }
}


def _stable_seed(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _apply_biome_biases(options: List[Tuple[str, int]], season: str, location: str) -> List[Tuple[str, int]]:
    loc = (location or "").strip().lower()
    adjusted = list(options)

    for biome_key, seasons in BIOME_BIASES.items():
        if biome_key in loc:
            bias_map = seasons.get(season, {})
            out: List[Tuple[str, int]] = []
            for desc, w in adjusted:
                w2 = w
                for needle, delta in bias_map.items():
                    if needle.lower() in desc.lower():
                        w2 = max(1, w2 + delta)
                out.append((desc, w2))
            adjusted = out

    return adjusted


def generate_weather(*, guild_id: int, ig: InGameDate, location: str = "") -> str:
    season = season_for_month(ig.month)
    options = _apply_biome_biases(WEATHER_TABLE[season], season, location)

    seed = _stable_seed(
        "TRUTH",
        str(guild_id),
        f"{ig.year:04d}-{ig.month:02d}-{ig.day:02d}",
        (location or "").strip().lower(),
    )
    rng = random.Random(seed)

    total = sum(w for _, w in options)
    roll = rng.randint(1, total)
    acc = 0
    for desc, w in options:
        acc += w
        if roll <= acc:
            flavor = {
                1: " The air feels oddly still.",
                2: " Distant gulls cry over the water.",
                3: " The horizon looks sharp and clean.",
                4: " Salt hangs faintly in the air.",
                5: " Wind tugs at cloaks and canvas.",
            }[rng.randint(1, 5)]
            return f"{desc}.{flavor}"

    return options[-1][0]


# =============================
# Forecast (prediction)
# =============================

def _forecast_accuracy(lead_days: int) -> float:
    acc = 0.92 - (0.06 * lead_days)
    return max(0.55, min(0.92, acc))


def forecast_confidence_label(lead_days: int) -> str:
    acc = _forecast_accuracy(lead_days)
    if acc >= 0.80:
        return "Likely"
    if acc >= 0.65:
        return "Possible"
    return "Uncertain"


def _pick_alternate_weather(
    *,
    guild_id: int,
    ig: InGameDate,
    location: str,
    actual_desc: str,
    today_key: str,
) -> str:
    season = season_for_month(ig.month)
    options = _apply_biome_biases(WEATHER_TABLE[season], season, location)

    actual_base = actual_desc.split(".", 1)[0].strip().lower()
    filtered = [(desc, w) for desc, w in options if desc.strip().lower() != actual_base]
    if not filtered:
        return actual_desc

    seed = _stable_seed(
        "FORECAST_WRONG",
        str(guild_id),
        today_key,
        f"{ig.year:04d}-{ig.month:02d}-{ig.day:02d}",
        (location or "").strip().lower(),
    )
    rng = random.Random(seed)

    total = sum(w for _, w in filtered)
    roll = rng.randint(1, total)
    acc = 0
    for desc, w in filtered:
        acc += w
        if roll <= acc:
            flavor = {
                1: " If the wind holds, expect it.",
                2: " Signs point that way—for now.",
                3: " The sky suggests it, but not decisively.",
                4: " Conditions could shift overnight.",
                5: " Local sailors swear by this read.",
                6: " Watch the horizon; it may change.",
            }[rng.randint(1, 6)]
            return f"{desc}.{flavor}"

    return filtered[-1][0]


def forecast_weather(
    *,
    guild_id: int,
    ig_today: InGameDate,
    ig_target: InGameDate,
    lead_days: int,
    location: str,
) -> str:
    actual = generate_weather(guild_id=guild_id, ig=ig_target, location=location)
    today_key = f"{ig_today.year:04d}-{ig_today.month:02d}-{ig_today.day:02d}"

    seed = _stable_seed(
        "FORECAST_HITCHECK",
        str(guild_id),
        today_key,
        f"{ig_target.year:04d}-{ig_target.month:02d}-{ig_target.day:02d}",
        (location or "").strip().lower(),
    )
    rng = random.Random(seed)

    if rng.random() <= _forecast_accuracy(lead_days):
        return actual

    return _pick_alternate_weather(
        guild_id=guild_id,
        ig=ig_target,
        location=location,
        actual_desc=actual,
        today_key=today_key,
    )


# =============================
# Cog
# =============================

class RedHawk(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5245445F4841574B)
        self.config.register_guild(
            epoch_real_date="2026-01-26",
            epoch_ingame_day_number=1,
            location="Coast",
            show_weekday=True,
            month_names=DEFAULT_MONTH_NAMES,
            weekday_names=DEFAULT_WEEKDAY_NAMES,
            autopost_channel_id=None,  # int | None
            autopost_last_pt=None,     # "YYYY-MM-DD" | None (Pacific date key)
        )
        self._autopost_daily.start()

    def cog_unload(self):
        self._autopost_daily.cancel()

    # ---- time helpers ----

    @staticmethod
    def _today_pt_date() -> date:
        return datetime.now(PACIFIC_TZ).date()

    # ---- calendar helpers ----

    async def _get_ingame_day_number_today(self, guild: discord.Guild) -> int:
        data = await self.config.guild(guild).all()
        epoch_real = date.fromisoformat(data["epoch_real_date"])
        epoch_ig = int(data["epoch_ingame_day_number"])
        delta_days = (self._today_pt_date() - epoch_real).days
        return epoch_ig + delta_days

    async def _get_ingame_for_offset(self, guild: discord.Guild, offset: int) -> InGameDate:
        today_num = await self._get_ingame_day_number_today(guild)
        target_num = max(1, today_num + offset)
        return day_number_to_ingame(target_num)

    async def _month_name(self, guild: discord.Guild, month: int) -> str:
        names = await self.config.guild(guild).month_names()
        return names[month - 1] if isinstance(names, list) and len(names) == 12 else f"Month {month}"

    async def _weekday_name(self, guild: discord.Guild, weekday: int) -> str:
        names = await self.config.guild(guild).weekday_names()
        return names[weekday - 1] if isinstance(names, list) and len(names) == 10 else f"Day {weekday}"

    async def _holiday_for(self, ig: InGameDate) -> Optional[str]:
        if ig.day != HOLIDAY_DAY:
            return None
        if ig.year % 2 == 1:
            return ODD_YEAR_HOLIDAYS.get(ig.month)
        return EVEN_YEAR_HOLIDAYS.get(ig.month)

    async def _format_date_line(self, guild: discord.Guild, ig: InGameDate) -> str:
        show_weekday = await self.config.guild(guild).show_weekday()
        month_name = await self._month_name(guild, ig.month)
        if show_weekday:
            weekday_name = await self._weekday_name(guild, ig.weekday)
            return (
                f"**{weekday_name}**, **{month_name}** {ig.day}\n"
                f"Year **{ig.year}** • Day {ig.day_of_year} • Week {ig.week}"
            )
        return f"**{month_name}** {ig.day}\nYear **{ig.year}** • Day {ig.day_of_year}"

    async def _send_daily_report(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        *,
        mark_posted: bool,
    ):
        cfg = await self.config.guild(guild).all()
        loc = cfg.get("location") or ""

        ig_today = await self._get_ingame_for_offset(guild, 0)
        holiday_today = await self._holiday_for(ig_today)
        wx_today = generate_weather(guild_id=guild.id, ig=ig_today, location=loc)

        forecast_lines: List[str] = []
        for lead in range(10):
            ig_target = await self._get_ingame_for_offset(guild, lead)
            predicted = forecast_weather(
                guild_id=guild.id,
                ig_today=ig_today,
                ig_target=ig_target,
                lead_days=lead,
                location=loc,
            )
            conf = forecast_confidence_label(lead)
            month_name = await self._month_name(guild, ig_target.month)
            weekday_name = await self._weekday_name(guild, ig_target.weekday)
            hol = await self._holiday_for(ig_target)

            base_pred = predicted.split(".", 1)[0]
            line = f"{weekday_name} {month_name} {ig_target.day}: {conf} — {base_pred}"
            if hol:
                line += f" (🎉 {hol})"
            forecast_lines.append(line)

        embed = discord.Embed(title="📣 Red Hawk Daily Report")
        embed.add_field(name="Date", value=await self._format_date_line(guild, ig_today), inline=False)
        embed.add_field(name="Weather", value=wx_today, inline=False)
        if holiday_today:
            embed.add_field(name="Holiday", value=holiday_today, inline=False)
        embed.add_field(name="10-Day Forecast", value="\n".join(forecast_lines), inline=False)

        await channel.send(embed=embed)

        if mark_posted:
            today_key = datetime.now(PACIFIC_TZ).date().isoformat()
            await self.config.guild(guild).autopost_last_pt.set(today_key)

    # =============================
    # PLAYER COMMANDS
    # =============================

    @commands.command(name="date")
    async def date_cmd(self, ctx: commands.Context):
        ig = await self._get_ingame_for_offset(ctx.guild, 0)
        holiday = await self._holiday_for(ig)

        embed = discord.Embed(title="📅 Red Hawk Date", description=await self._format_date_line(ctx.guild, ig))
        embed.add_field(name="Season", value=season_for_month(ig.month), inline=True)
        if holiday:
            embed.add_field(name="Holiday", value=holiday, inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="weather")
    async def weather_cmd(self, ctx: commands.Context, offset: int = 0):
        ig = await self._get_ingame_for_offset(ctx.guild, offset)
        cfg = await self.config.guild(ctx.guild).all()
        loc = cfg.get("location") or ""
        holiday = await self._holiday_for(ig)

        wx = generate_weather(guild_id=ctx.guild.id, ig=ig, location=loc)

        when = (
            "Today" if offset == 0 else
            "Tomorrow" if offset == 1 else
            "Yesterday" if offset == -1 else
            f"Day {offset:+d}"
        )

        embed = discord.Embed(title=f"⛅ Red Hawk Weather — {when}", description=wx)
        embed.add_field(name="Date", value=await self._format_date_line(ctx.guild, ig), inline=False)
        if loc:
            embed.add_field(name="Location", value=loc, inline=True)
        embed.add_field(name="Season", value=season_for_month(ig.month), inline=True)
        if holiday:
            embed.add_field(name="Holiday", value=holiday, inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="forecast")
    async def forecast_cmd(self, ctx: commands.Context, days: int = 10):
        if days < 1:
            return await ctx.send("Days must be >= 1.")
        days = min(days, 10)

        cfg = await self.config.guild(ctx.guild).all()
        loc = cfg.get("location") or ""

        ig_today = await self._get_ingame_for_offset(ctx.guild, 0)
        lines: List[str] = []

        for lead in range(days):
            ig_target = await self._get_ingame_for_offset(ctx.guild, lead)
            predicted = forecast_weather(
                guild_id=ctx.guild.id,
                ig_today=ig_today,
                ig_target=ig_target,
                lead_days=lead,
                location=loc,
            )
            conf = forecast_confidence_label(lead)
            month_name = await self._month_name(ctx.guild, ig_target.month)
            weekday_name = await self._weekday_name(ctx.guild, ig_target.weekday)
            holiday = await self._holiday_for(ig_target)

            line = f"**{weekday_name}, {month_name} {ig_target.day}** — *{conf}* — {predicted}"
            if holiday:
                line += f"  🎉 **{holiday}**"
            lines.append(line)

        embed = discord.Embed(title=f"🌤️ Red Hawk Forecast — Next {days} day(s)", description="\n".join(lines))
        await ctx.send(embed=embed)

    # =============================
    # ADMIN: AUTOPOST + POSTNOW
    # =============================

    @commands.command(name="wmautopost", aliases=["rhautopost"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmautopost(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        content = (ctx.message.content or "").strip().lower()
        if channel is None and content.endswith(" off"):
            await self.config.guild(ctx.guild).autopost_channel_id.set(None)
            await self.config.guild(ctx.guild).autopost_last_pt.set(None)
            return await ctx.send("✅ Autopost disabled.")

        if channel is None:
            return await ctx.send("Usage: `wmautopost #channel` or `wmautopost off`")

        await self.config.guild(ctx.guild).autopost_channel_id.set(channel.id)
        await ctx.send(f"✅ Autopost enabled in {channel.mention} (posts at 7 AM Pacific)")

    @commands.command(name="wmpostnow", aliases=["rhpostnow"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmpostnow(self, ctx: commands.Context, where: str = ""):
        """
        Post the daily report immediately.

        Usage:
          [p]wmpostnow         -> posts to configured autopost channel
          [p]wmpostnow here    -> posts to current channel
        """
        where = (where or "").strip().lower()

        if where == "here":
            await self._send_daily_report(ctx.guild, ctx.channel, mark_posted=False)
            return await ctx.send("✅ Posted the daily report here.")

        cfg = await self.config.guild(ctx.guild).all()
        ch_id = cfg.get("autopost_channel_id")
        if not ch_id:
            return await ctx.send("❌ Autopost channel isn’t set. Use `wmautopost #channel` first.")

        channel = ctx.guild.get_channel(ch_id)
        if channel is None:
            return await ctx.send("❌ I can’t find the configured autopost channel anymore.")

        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await ctx.send("❌ I need Send Messages + Embed Links in the autopost channel.")

        await self._send_daily_report(ctx.guild, channel, mark_posted=False)
        await ctx.send(f"✅ Posted the daily report in {channel.mention}.")

    # =============================
    # ADMIN / SETTINGS
    # =============================

    @commands.command(name="wmsetepoch", aliases=["rhsetepoch"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetepoch(self, ctx: commands.Context, real_date: str, ig_day_number: int = 1):
        try:
            parsed = date.fromisoformat(real_date)
        except Exception:
            return await ctx.send("Please use YYYY-MM-DD, e.g. `2026-01-26`.")
        if ig_day_number < 1:
            return await ctx.send("`ig_day_number` must be >= 1.")

        await self.config.guild(ctx.guild).epoch_real_date.set(parsed.isoformat())
        await self.config.guild(ctx.guild).epoch_ingame_day_number.set(int(ig_day_number))

        ig = day_number_to_ingame(int(ig_day_number))
        await ctx.send(
            f"✅ Epoch set: real **{parsed.isoformat()}** ↔ in-game day **{ig_day_number}** "
            f"(Year {ig.year}, Month {ig.month}, Day {ig.day})."
        )

    @commands.command(name="wmlocation", aliases=["rhlocation"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmlocation(self, ctx: commands.Context, *, location: str = ""):
        await self.config.guild(ctx.guild).location.set(location.strip())
        await ctx.send("✅ Location updated." if location.strip() else "✅ Location cleared.")

    @commands.command(name="wmweekday", aliases=["rhweekday"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmweekday(self, ctx: commands.Context, enabled: Optional[bool] = None):
        cur = await self.config.guild(ctx.guild).show_weekday()
        if enabled is None:
            enabled = not cur
        await self.config.guild(ctx.guild).show_weekday.set(bool(enabled))
        await ctx.send(f"✅ show_weekday set to **{bool(enabled)}**.")

    @commands.command(name="wmsetmonthnames", aliases=["rhsetmonthnames"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetmonthnames(self, ctx: commands.Context, *, names: str):
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 12:
            return await ctx.send("Please provide exactly **12** comma-separated month names.")
        await self.config.guild(ctx.guild).month_names.set(parts)
        await ctx.send("✅ Month names updated.")

    @commands.command(name="wmsetweekdaynames", aliases=["rhsetweekdaynames"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetweekdaynames(self, ctx: commands.Context, *, names: str):
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 10:
            return await ctx.send("Please provide exactly **10** comma-separated weekday names.")
        await self.config.guild(ctx.guild).weekday_names.set(parts)
        await ctx.send("✅ Weekday names updated.")

    @commands.command(name="wmnamesreset", aliases=["rhnamesreset"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmnamesreset(self, ctx: commands.Context):
        await self.config.guild(ctx.guild).month_names.set(DEFAULT_MONTH_NAMES)
        await self.config.guild(ctx.guild).weekday_names.set(DEFAULT_WEEKDAY_NAMES)
        await ctx.send("✅ Month and weekday names reset to defaults.")

    # =============================
    # AUTOPOST (daily at 7:00 AM Pacific)
    # =============================

    @tasks.loop(time=dtime(hour=7, minute=0, tzinfo=PACIFIC_TZ))
    async def _autopost_daily(self):
        today_key = datetime.now(PACIFIC_TZ).date().isoformat()

        for guild in list(self.bot.guilds):
            cfg = await self.config.guild(guild).all()
            ch_id = cfg.get("autopost_channel_id")
            if not ch_id:
                continue

            if cfg.get("autopost_last_pt") == today_key:
                continue

            channel = guild.get_channel(ch_id)
            if channel is None:
                continue

            perms = channel.permissions_for(guild.me)
            if not (perms.send_messages and perms.embed_links):
                continue

            await self._send_daily_report(guild, channel, mark_posted=True)

    @_autopost_daily.before_loop
    async def _before_autopost(self):
        await self.bot.wait_until_red_ready()


async def setup(bot):
    await bot.add_cog(RedHawk(bot))
