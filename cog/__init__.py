from .redhawk import RedHawk

async def setup(bot):
    await bot.add_cog(RedHawk(bot))
