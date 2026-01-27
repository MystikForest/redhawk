# westmarch_calendar_weather.py
# Red-DiscordBot cog: Red Hawk Westmarch calendar + deterministic daily weather + "usually right" forecast.
#
# Player commands (as requested):
#   [p]date
#   [p]weather [offset]
#   [p]forecast [days]
#
# Admin/settings (kept prefixed):
#   [p]wmsetepoch YYYY-MM-DD [ig_day_number]
#   [p]wmlocation <text>
#   [p]wmweekday [true/false]
#   [p]wmsetmonthnames name1, ... (12)
#   [p]wmsetweekdaynames name1, ... (10)
#   [p]wmnamesreset
#
# Calendar rules (per your spec):
# - 1 real-world day = 1 in-game day (UTC-based)
# - 10 days in a week
# - 3 weeks in a month => base 30-day months
# - 365 days in a year; every 4th year has 366
# - Extra days are added to every other month
#
# Interpretation:
# - 12 months per year.
# - Base month length = 30.
# - Normal year: +5 extras -> months 2,4,6,8,10 are 31 days; month 12 is 30.
# - Leap year:   +6 extras -> months 2,4,6,8,10,12 are 31 days.
# - Year 4,8,12,... are leap years.

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import Config, commands


# =============================
# Names (your provided canon)
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
    even_months = [2, 4, 6, 8, 10, 12]
    for m in even_months[:extras]:
        lengths[m - 1] += 1
    return lengths


def day_number_to_ingame(day_number: int) -> InGameDate:
    if day_number < 1:
        raise ValueError("day_number must be >= 1")

    y = 1
    remaining = day_number
    while True:
        yl = year_length(y)
        if remaining > yl:
            remaining -= yl
            y += 1
        else:
            break

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

# Optional biome bias: makes "Coast" feel coastal without hard-coding outcomes.
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
            new_opts: List[Tuple[str, int]] = []
            for desc, w in adjusted:
                w2 = w
                for needle, delta in bias_map.items():
                    if needle.lower() in desc.lower():
                        w2 = max(1, w2 + delta)
                new_opts.append((desc, w2))
            adjusted = new_opts

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
                6: " A pressure change hints at tomorrow.",
            }[rng.randint(1, 6)]
            return f"{desc}.{flavor}"

    return options[-1][0]


# =============================
# Forecast (prediction)
# =============================

def _forecast_accuracy(lead_days: int) -> float:
    # 0: 92%, 1: 86%, 2: 80%, 3: 74%, then down to 55% floor
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

    filtered: List[Tuple[str, int]] = []
    for desc, w in options:
        if desc.strip().lower() != actual_base:
            filtered.append((desc, w))

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

    acc = _forecast_accuracy(lead_days)
    seed = _stable_seed(
        "FORECAST_HITCHECK",
        str(guild_id),
        today_key,
        f"{ig_target.year:04d}-{ig_target.month:02d}-{ig_target.day:02d}",
        (location or "").strip().lower(),
    )
    rng = random.Random(seed)

    if rng.random() <= acc:
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

class WestmarchCalendarWeather(commands.Cog):
    """Red Hawk: in-game calendar + weather for a Westmarch server."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5245445F4841574B)  # "RED_HAWK" hex-ish
        self.config.register_guild(
            epoch_real_date="2026-01-26",
            epoch_ingame_day_number=1,
            location="Coast",
            show_weekday=True,
            month_names=DEFAULT_MONTH_NAMES,
            weekday_names=DEFAULT_WEEKDAY_NAMES,
        )

    # ---- helpers ----

    @staticmethod
    def _today_utc_date() -> date:
        return datetime.now(timezone.utc).date()

    async def _get_ingame_day_number_today(self, guild: discord.Guild) -> int:
        data = await self.config.guild(guild).all()
        epoch_real = date.fromisoformat(data["epoch_real_date"])
        epoch_ig = int(data["epoch_ingame_day_number"])
        delta_days = (self._today_utc_date() - epoch_real).days
        return epoch_ig + delta_days

    async def _get_ingame_for_offset(self, guild: discord.Guild, offset: int) -> InGameDate:
        today_num = await self._get_ingame_day_number_today(guild)
        target_num = max(1, today_num + offset)
        return day_number_to_ingame(target_num)

    async def _month_name(self, guild: discord.Guild, month: int) -> str:
        names = await self.config.guild(guild).month_names()
        if isinstance(names, list) and len(names) == 12:
            return names[month - 1]
        return f"Month {month}"

    async def _weekday_name(self, guild: discord.Guild, weekday: int) -> str:
        names = await self.config.guild(guild).weekday_names()
        if isinstance(names, list) and len(names) == 10:
            return names[weekday - 1]
        return f"Day {weekday}"

    async def _format_date_line(self, guild: discord.Guild, ig: InGameDate) -> str:
        show_weekday = await self.config.guild(guild).show_weekday()
        month_name = await self._month_name(guild, ig.month)
        if show_weekday:
            weekday_name = await self._weekday_name(guild, ig.weekday)
            return (
                f"**{weekday_name}**, **{month_name}** {ig.day}\n"
                f"Year **{ig.year}** • Day {ig.day_of_year} • Week {ig.week}"
            )
        return f"**{month_name}** {ig.day}, Year **{ig.year}** (Day {ig.day_of_year})"

    # =============================
    # PLAYER COMMANDS
    # =============================

    @commands.command(name="date")
    async def date_cmd(self, ctx: commands.Context):
        """Show today's in-game date."""
        ig = await self._get_ingame_for_offset(ctx.guild, 0)
        line = await self._format_date_line(ctx.guild, ig)

        embed = discord.Embed(title="📅 Red Hawk Date", description=line)
        embed.add_field(name="Season", value=season_for_month(ig.month), inline=True)
        embed.set_footer(text="Red Hawk Westmarch • 1 real day = 1 in-game day (UTC)")
        await ctx.send(embed=embed)

    @commands.command(name="weather")
    async def weather_cmd(self, ctx: commands.Context, offset: int = 0):
        """
        Show in-game weather (truth).
        offset: 0=today, 1=tomorrow, -1=yesterday, etc.
        """
        ig = await self._get_ingame_for_offset(ctx.guild, offset)
        loc = await self.config.guild(ctx.guild).location()
        wx = generate_weather(guild_id=ctx.guild.id, ig=ig, location=loc or "")

        when = (
            "Today" if offset == 0 else
            "Tomorrow" if offset == 1 else
            "Yesterday" if offset == -1 else
            f"Day {offset:+d}"
        )

        date_line = await self._format_date_line(ctx.guild, ig)

        embed = discord.Embed(title=f"⛅ Red Hawk Weather — {when}", description=wx)
        embed.add_field(name="Date", value=date_line, inline=False)
        if loc:
            embed.add_field(name="Location", value=loc, inline=True)
        embed.add_field(name="Season", value=season_for_month(ig.month), inline=True)
        embed.set_footer(text="Truth: deterministic per day (seeded by guild + date + location).")
        await ctx.send(embed=embed)

    @commands.command(name="forecast")
    async def forecast_cmd(self, ctx: commands.Context, days: int = 3):
        """
        Show a forecast for the next N in-game days (default 3, max 10).
        Forecast is usually right, but can be wrong (more often further out).
        """
        if days < 1:
            return await ctx.send("Days must be >= 1.")
        days = min(days, 10)

        loc = await self.config.guild(ctx.guild).location()
        ig_today = await self._get_ingame_for_offset(ctx.guild, 0)

        lines = []
        for lead in range(days):
            ig_target = await self._get_ingame_for_offset(ctx.guild, lead)
            predicted = forecast_weather(
                guild_id=ctx.guild.id,
                ig_today=ig_today,
                ig_target=ig_target,
                lead_days=lead,
                location=loc or "",
            )
            conf = forecast_confidence_label(lead)

            month_name = await self._month_name(ctx.guild, ig_target.month)
            weekday_name = await self._weekday_name(ctx.guild, ig_target.weekday)
            lines.append(
                f"**{weekday_name}, {month_name} {ig_target.day}** — *{conf}* — {predicted}"
            )

        embed = discord.Embed(title=f"🌤️ Red Hawk Forecast — Next {days} day(s)", description="\n".join(lines))
        if loc:
            embed.add_field(name="Location", value=loc, inline=True)
        embed.set_footer(text="Confidence is based on lead time (tomorrow > later).")
        await ctx.send(embed=embed)

    # =============================
    # ADMIN / SETTINGS
    # =============================

    @commands.command(name="wmsetepoch", aliases=["rhsetepoch"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetepoch(self, ctx: commands.Context, real_date: str, ig_day_number: int = 1):
        """Set the epoch mapping: YYYY-MM-DD (UTC) ↔ absolute in-game day number."""
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
        """Set weather location/biome seed (e.g. Coast)."""
        await self.config.guild(ctx.guild).location.set(location.strip())
        if location.strip():
            await ctx.send(f"✅ Weather location set to: **{location.strip()}**")
        else:
            await ctx.send("✅ Weather location cleared (no biome seed).")

    @commands.command(name="wmweekday", aliases=["rhweekday"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmweekday(self, ctx: commands.Context, enabled: Optional[bool] = None):
        """Toggle showing weekday/week info in date."""
        cur = await self.config.guild(ctx.guild).show_weekday()
        if enabled is None:
            enabled = not cur
        await self.config.guild(ctx.guild).show_weekday.set(bool(enabled))
        await ctx.send(f"✅ show_weekday set to **{bool(enabled)}**.")

    @commands.command(name="wmsetmonthnames", aliases=["rhsetmonthnames"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetmonthnames(self, ctx: commands.Context, *, names: str):
        """Set all 12 month names at once (comma-separated)."""
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 12:
            return await ctx.send("Please provide exactly **12** comma-separated month names.")
        await self.config.guild(ctx.guild).month_names.set(parts)
        await ctx.send("✅ Month names updated.")

    @commands.command(name="wmsetweekdaynames", aliases=["rhsetweekdaynames"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmsetweekdaynames(self, ctx: commands.Context, *, names: str):
        """Set all 10 weekday names at once (comma-separated)."""
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 10:
            return await ctx.send("Please provide exactly **10** comma-separated weekday names.")
        await self.config.guild(ctx.guild).weekday_names.set(parts)
        await ctx.send("✅ Weekday names updated.")

    @commands.command(name="wmnamesreset", aliases=["rhnamesreset"])
    @commands.admin_or_permissions(manage_guild=True)
    async def wmnamesreset(self, ctx: commands.Context):
        """Reset month + weekday names to defaults."""
        await self.config.guild(ctx.guild).month_names.set(DEFAULT_MONTH_NAMES)
        await self.config.guild(ctx.guild).weekday_names.set(DEFAULT_WEEKDAY_NAMES)
        await ctx.send("✅ Month and weekday names reset to defaults.")


async def setup(bot):
    await bot.add_cog(redhawk(bot))
