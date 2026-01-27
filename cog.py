# westmarch_calendar_weather.py
# Red-DiscordBot cog: Red Hawk Westmarch calendar + deterministic daily weather.
#
# Calendar rules (per your spec):
# - 1 real-world day = 1 in-game day (UTC-based)
# - 10 days in a week
# - 3 weeks in a month => base 30-day months
# - 365 days in a year; every 4th year has 366
# - Extra days are added to every other month
#
# Interpretation (explicit, deterministic):
# - 12 months per year.
# - Base month length = 30.
# - Normal year: 365 = 360 + 5 extras -> months 2,4,6,8,10 are 31 days; month 12 is 30.
# - Leap year: 366 = 360 + 6 extras -> months 2,4,6,8,10,12 are 31 days.
# - Year 4, 8, 12, ... are leap years ("every fourth has 366").
#
# Weather:
# - Deterministic daily weather seeded by (guild_id + in-game date + location).
# - Default location is set to "Coast" (as requested).
# - Seasonal tables are simple and easy to tweak.

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import Config, commands


# -----------------------------
# Calendar math
# -----------------------------

DAYS_PER_WEEK = 10
WEEKS_PER_MONTH = 3
BASE_DAYS_PER_MONTH = DAYS_PER_WEEK * WEEKS_PER_MONTH  # 30
MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class InGameDate:
    year: int   # 1-indexed
    month: int  # 1-12
    day: int    # 1..month_length
    day_of_year: int  # 1..365/366

    @property
    def week(self) -> int:
        # 10-day weeks
        return ((self.day_of_year - 1) // DAYS_PER_WEEK) + 1

    @property
    def weekday(self) -> int:
        # 1..10
        return ((self.day_of_year - 1) % DAYS_PER_WEEK) + 1


def is_leap_year(year: int) -> bool:
    # "every fourth has 366" => year 4,8,12... are leap years
    return year % 4 == 0


def year_length(year: int) -> int:
    return 366 if is_leap_year(year) else 365


def month_lengths_for_year(year: int) -> List[int]:
    """
    Base 30-day months, then distribute extras to every other (even) month.
    Normal year: +5 extras -> months 2,4,6,8,10 get +1 day
    Leap year:   +6 extras -> months 2,4,6,8,10,12 get +1 day
    """
    lengths = [BASE_DAYS_PER_MONTH] * MONTHS_PER_YEAR
    extras = 6 if is_leap_year(year) else 5
    even_months = [2, 4, 6, 8, 10, 12]
    for m in even_months[:extras]:
        lengths[m - 1] += 1
    return lengths


def day_number_to_ingame(day_number: int) -> InGameDate:
    """
    Converts an absolute in-game day number (1-indexed) to InGameDate.
    day_number = 1 => Year 1, Month 1, Day 1
    """
    if day_number < 1:
        raise ValueError("day_number must be >= 1")

    # Find year
    y = 1
    remaining = day_number
    while True:
        yl = year_length(y)
        if remaining > yl:
            remaining -= yl
            y += 1
        else:
            break

    # Find month/day in that year
    lengths = month_lengths_for_year(y)
    m = 1
    while remaining > lengths[m - 1]:
        remaining -= lengths[m - 1]
        m += 1

    d = remaining
    doy = day_number - sum(year_length(yy) for yy in range(1, y))
    return InGameDate(year=y, month=m, day=d, day_of_year=doy)


def ingame_to_day_number(ig: InGameDate) -> int:
    """Converts an InGameDate to absolute in-game day number (1-indexed)."""
    if ig.year < 1 or not (1 <= ig.month <= 12) or ig.day < 1:
        raise ValueError("Invalid in-game date.")

    lengths = month_lengths_for_year(ig.year)
    if ig.day > lengths[ig.month - 1]:
        raise ValueError("Day exceeds month length for that year/month.")

    days_before_year = sum(year_length(yy) for yy in range(1, ig.year))
    days_before_month = sum(lengths[: ig.month - 1])
    return days_before_year + days_before_month + ig.day


# -----------------------------
# Weather generation
# -----------------------------

def season_for_month(month: int) -> str:
    # 1-3 Winter, 4-6 Spring, 7-9 Summer, 10-12 Autumn
    if month in (1, 2, 3):
        return "Winter"
    if month in (4, 5, 6):
        return "Spring"
    if month in (7, 8, 9):
        return "Summer"
    return "Autumn"


# Baseline weather by season
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

# Optional biome modifiers (based on location keyword). Keep simple + readable.
# If location contains the key (case-insensitive), it biases weights a bit.
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
    """
    Adds simple weight biases based on biome keyword matches in `location`.
    This keeps weather deterministic while making "Coast" feel coastal.
    """
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
                        w2 = max(1, w2 + delta)  # keep weights >= 1
                new_opts.append((desc, w2))
            adjusted = new_opts

    return adjusted


def generate_weather(*, guild_id: int, ig: InGameDate, location: str = "") -> str:
    season = season_for_month(ig.month)
    options = WEATHER_TABLE[season]
    options = _apply_biome_biases(options, season, location)

    seed = _stable_seed(
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


# -----------------------------
# Names (configurable)
# -----------------------------

DEFAULT_MONTH_NAMES = [
    "Icehold", "Frostwane", "Rainrich", "Lightwake", "Sunswell", "Greenflux",
    "Sunfade", "Stormreign", "Amberfell", "Auburncrown", "Icefleet", "Frostcrest",
]

DEFAULT_WEEKDAY_NAMES = [
    "Solies", "Halos", "Incedis", "Talis", "Inanos",
    "Penumus", "Oris", "Neptis", "Anaemis", "Anaemis",
]


# -----------------------------
# Cog
# -----------------------------

class WestmarchCalendarWeather(commands.Cog):
    """Red Hawk: in-game calendar + weather for a Westmarch server."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x9A77_4D4E_574D_4341)
        self.config.register_guild(
            epoch_real_date="2026-01-26",   # set via [p]wm setepoch
            epoch_ingame_day_number=1,      # set via [p]wm setepoch
            location="Coast",               # ✅ requested default
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

    async def _get_ingame_today(self, guild: discord.Guild) -> InGameDate:
        day_num = await self._get_ingame_day_number_today(guild)
        return day_number_to_ingame(max(1, day_num))

    async def _name_month(self, guild: discord.Guild, month: int) -> str:
        names = await self.config.guild(guild).month_names()
        if isinstance(names, list) and len(names) == 12:
            return names[month - 1]
        return f"Month {month}"

    async def _name_weekday(self, guild: discord.Guild, weekday: int) -> str:
        names = await self.config.guild(guild).weekday_names()
        if isinstance(names, list) and len(names) == 10:
            return names[weekday - 1]
        return f"Weekday {weekday}"

    async def _format_date_line(self, guild: discord.Guild, ig: InGameDate) -> str:
        show_weekday = await self.config.guild(guild).show_weekday()
        month_name = await self._name_month(guild, ig.month)
        if show_weekday:
            weekday_name = await self._name_weekday(guild, ig.weekday)
            return (
                f"Year **{ig.year}**, **{month_name}** (M{ig.month}), Day **{ig.day}**\n"
                f"Day {ig.day_of_year} • Week {ig.week} • **{weekday_name}** ({ig.weekday}/10)"
            )
        return f"Year **{ig.year}**, **{month_name}** (M{ig.month}), Day **{ig.day}** (Day {ig.day_of_year})"

    # ---- commands ----

    @commands.group(name="wm", invoke_without_command=True)
    async def wm_group(self, ctx: commands.Context):
        """Red Hawk Westmarch tools: calendar + weather."""
        await ctx.send_help()

    @wm_group.command(name="date")
    async def wm_date(self, ctx: commands.Context):
        """Show today's in-game date."""
        ig = await self._get_ingame_today(ctx.guild)
        line = await self._format_date_line(ctx.guild, ig)

        embed = discord.Embed(title="📅 Red Hawk Calendar", description=line)
        embed.add_field(name="Season", value=season_for_month(ig.month), inline=True)
        embed.set_footer(text="Red Hawk Westmarch • 1 real day = 1 in-game day (UTC)")
        await ctx.send(embed=embed)

    @wm_group.command(name="setepoch")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_setepoch(self, ctx: commands.Context, real_date: str, ig_day_number: int = 1):
        """
        Set the epoch mapping.

        real_date: YYYY-MM-DD (UTC date)
        ig_day_number: absolute in-game day number for that real date (default 1)

        Example:
        [p]wm setepoch 2026-01-01 120
        """
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

    @wm_group.command(name="location")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_location(self, ctx: commands.Context, *, location: str = ""):
        """
        Set an optional location/biome seed for weather.

        Example:
        [p]wm location Coast
        """
        await self.config.guild(ctx.guild).location.set(location.strip())
        if location.strip():
            await ctx.send(f"✅ Weather location set to: **{location.strip()}**")
        else:
            await ctx.send("✅ Weather location cleared (no biome seed).")

    @wm_group.command(name="weekday")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_weekday_toggle(self, ctx: commands.Context, enabled: Optional[bool] = None):
        """
        Toggle showing weekday/week info in [p]wm date.
        If enabled is omitted, it flips the setting.
        """
        cur = await self.config.guild(ctx.guild).show_weekday()
        if enabled is None:
            enabled = not cur
        await self.config.guild(ctx.guild).show_weekday.set(bool(enabled))
        await ctx.send(f"✅ show_weekday set to **{bool(enabled)}**.")

    @wm_group.command(name="weather")
    async def wm_weather(self, ctx: commands.Context, offset: int = 0):
        """
        Show in-game weather.
        offset: 0 = today, 1 = tomorrow, -1 = yesterday, etc.
        """
        today_num = await self._get_ingame_day_number_today(ctx.guild)
        target_num = max(1, today_num + offset)
        ig = day_number_to_ingame(target_num)

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
        embed.set_footer(text="Deterministic per day (seeded by guild + date + location).")
        await ctx.send(embed=embed)

    @wm_group.command(name="forecast")
    async def wm_forecast(self, ctx: commands.Context, days: int = 3):
        """Show a short forecast for the next N in-game days (default 3, max 10)."""
        if days < 1:
            return await ctx.send("Days must be >= 1.")
        days = min(days, 10)

        today_num = await self._get_ingame_day_number_today(ctx.guild)
        loc = await self.config.guild(ctx.guild).location()

        lines = []
        for i in range(days):
            ig = day_number_to_ingame(max(1, today_num + i))
            wx = generate_weather(guild_id=ctx.guild.id, ig=ig, location=loc or "")
            month_name = await self._name_month(ctx.guild, ig.month)
            lines.append(f"**Y{ig.year} {month_name} {ig.day}** — {wx}")

        embed = discord.Embed(title=f"🌤️ Red Hawk Forecast — Next {days} day(s)", description="\n".join(lines))
        if loc:
            embed.add_field(name="Location", value=loc, inline=True)
        embed.set_footer(text="Weather is deterministic; tweak tables/biomes in the cog to fit your world.")
        await ctx.send(embed=embed)

    # ---- naming commands ----

    @wm_group.command(name="setmonthnames")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_setmonthnames(self, ctx: commands.Context, *, names: str):
        """
        Set all 12 month names at once.
        Provide 12 names separated by commas.

        Example:
        [p]wm setmonthnames Frostwane, Thawrise, Dawnspring, ...
        """
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 12:
            return await ctx.send("Please provide exactly **12** comma-separated month names.")
        await self.config.guild(ctx.guild).month_names.set(parts)
        await ctx.send("✅ Month names updated.")

    @wm_group.command(name="setweekdaynames")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_setweekdaynames(self, ctx: commands.Context, *, names: str):
        """
        Set all 10 weekday names at once.
        Provide 10 names separated by commas.

        Example:
        [p]wm setweekdaynames Oneday, Twoday, Threeday, ...
        """
        parts = [p.strip() for p in names.split(",") if p.strip()]
        if len(parts) != 10:
            return await ctx.send("Please provide exactly **10** comma-separated weekday names.")
        await self.config.guild(ctx.guild).weekday_names.set(parts)
        await ctx.send("✅ Weekday names updated.")

    @wm_group.command(name="namesreset")
    @commands.admin_or_permissions(manage_guild=True)
    async def wm_namesreset(self, ctx: commands.Context):
        """Reset month + weekday names to defaults."""
        await self.config.guild(ctx.guild).month_names.set(DEFAULT_MONTH_NAMES)
        await self.config.guild(ctx.guild).weekday_names.set(DEFAULT_WEEKDAY_NAMES)
        await ctx.send("✅ Month and weekday names reset to defaults.")


def setup(bot):
    bot.add_cog(WestmarchCalendarWeather(bot))
