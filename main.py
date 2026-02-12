import discord
from discord.ext import commands
import os
import logging
import asyncio
from dotenv import load_dotenv
import database
import aiosqlite

# 1. Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

load_dotenv()

class JantlemanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Initialize DB tables
        await database.init_db()
        # Load Cogs
        await self.load_extension('cogs.reputation')
        # Sync Commands
        await self.tree.sync()
        logging.info("Commands synced and extensions loaded.")

bot = JantlemanBot()

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user} (ID: {bot.user.id})')

@bot.event
async def on_thread_create(thread):
    # Ignore private threads or non-forum threads to prevent errors
    if not isinstance(thread.parent, discord.ForumChannel):
        return

    async with database.get_db() as db:
        async with db.execute("SELECT forum_channel_id FROM Settings WHERE guild_id = ?", (thread.guild.id,)) as cursor:
            setting = await cursor.fetchone()

    # If this guild hasn't set up a channel, or it's the wrong channel
    if not setting or thread.parent_id != setting[0]:
        return

    owner = thread.owner
    # Edge Case: Owner might have left the server
    if not owner:
        return

    async with database.get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT total_stars, total_reviews FROM Users WHERE user_id = ?", (owner.id,)) as cursor:
            data = await cursor.fetchone()

    embed = discord.Embed(color=0xf1c40f)
    if not data or data['total_reviews'] == 0:
        embed.title = "⚠️ New Trader Alert"
        embed.description = f"{owner.mention} has no recorded trading history. Proceed with caution."
    else:
        avg = round(data['total_stars'] / data['total_reviews'], 1)
        stars = ("⭐" * int(avg)) + ("☆" * (5 - int(avg)))
        embed.title = "✅ Background Check"
        embed.description = f"{owner.mention}\n**Rating:** {stars} ({avg}/5)\n**Trades:** {data['total_reviews']}"
    
    await thread.send(embed=embed)

async def main():
    async with bot:
        await bot.start(os.getenv('DISCORD_TOKEN'))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        pass
