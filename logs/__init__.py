from .messagelog import MessageLog


async def setup(bot):
    await bot.add_cog(MessageLog(bot))