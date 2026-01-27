from .redhawk import redhawk

async def setup(bot):
    await bot.add_cog(redhawk(bot))
