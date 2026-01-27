from .redhawk import WestmarchCalendarWeather

async def setup(bot):
    await bot.add_cog(WestmarchCalendarWeather(bot))