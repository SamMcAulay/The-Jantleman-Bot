import discord
from discord.ext import commands
import os
import logging
import asyncio
from dotenv import load_dotenv
import database
import aiosqlite
from datetime import datetime, timedelta

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
load_dotenv()

class JantlemanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # Required for Identity Tracking
        intents.message_content = True  # Required for Watchlist scanning
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await database.init_db()
        await self.load_extension('cogs.reputation')
        await self.load_extension('cogs.watchlist') # Load new cog
        await self.tree.sync()
        logging.info("System Online. Extensions Loaded.")

bot = JantlemanBot()

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')

# --- IDENTITY TRACKING EVENT ---
@bot.event
async def on_member_update(before, after):
    """Logs when a user changes their display name (Nickname or Global Name)"""
    if before.display_name != after.display_name:
        async with database.get_db() as db:
            await db.execute(
                "INSERT INTO NameHistory (user_id, old_name, new_name) VALUES (?, ?, ?)",
                (after.id, before.display_name, after.display_name)
            )
            await db.commit()

# --- TRADING FORUM EVENT ---
@bot.event
async def on_thread_create(thread):
    # 1. Validation: Must be a Forum Channel
    if not isinstance(thread.parent, discord.ForumChannel): return

    # 2. Validation: Must be in our Monitored List
    async with database.get_db() as db:
        async with db.execute("SELECT 1 FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?", (thread.guild.id, thread.parent_id)) as cursor:
            is_monitored = await cursor.fetchone()
    
    if not is_monitored: return

    owner = thread.owner
    if not owner: return

    # 3. Gather Data (Reputation + Name History)
    async with database.get_db() as db:
        db.row_factory = aiosqlite.Row
        
        # Fetch Rep
        async with db.execute("SELECT total_stars, total_reviews FROM Users WHERE user_id = ?", (owner.id,)) as cursor:
            rep_data = await cursor.fetchone()
        
        # Fetch Identity Changes (Last 7 Days)
        seven_days_ago = datetime.now() - timedelta(days=7)
        async with db.execute("SELECT COUNT(*) FROM NameHistory WHERE user_id = ? AND timestamp > ?", (owner.id, seven_days_ago)) as cursor:
            name_changes = await cursor.fetchone()
            change_count = name_changes[0] if name_changes else 0

    # 4. Construct Background Check Embed
    embed = discord.Embed(timestamp=datetime.now())
    embed.set_footer(text="The Jantleman • Automated Check", icon_url=bot.user.display_avatar.url)
    embed.set_thumbnail(url=owner.display_avatar.url)

    # Risk Analysis Logic
    if not rep_data or rep_data['total_reviews'] == 0:
        embed.title = "⚠️ First-Time Trader"
        embed.color = 0xffa500 # Orange
        desc = f"**User:** {owner.mention}\nHas **0** recorded trades."
    else:
        avg = round(rep_data['total_stars'] / rep_data['total_reviews'], 1)
        stars = ("⭐" * int(avg)) + ("☆" * (5 - int(avg)))
        embed.title = "✅ Verified Trader"
        embed.color = discord.Color.gold()
        desc = f"**User:** {owner.mention}\n**Rating:** {stars} ({avg}/5)\n**Deals:** {rep_data['total_reviews']}"

    # Add Identity Warning if applicable
    if change_count > 0:
        embed.add_field(
            name="⚠️ Identity Alert", 
            value=f"Changed name **{change_count} times** in the last 7 days.", 
            inline=False
        )
        if change_count >= 3:
            embed.color = discord.Color.red()
            embed.title = "🛑 High Risk Alert"

    embed.description = desc
    await thread.send(embed=embed)

    # WATCHLIST NOTIFICATIONS
    # Scan the title + valid starter message
    content_to_scan = thread.name.lower()
    
    # Wait briefly for the starter message to be available
    await asyncio.sleep(1)
    try:
        starter_msg = await thread.fetch_message(thread.id) # Starter message often has same ID as thread
        if starter_msg:
            content_to_scan += " " + starter_msg.content.lower()
    except:
        pass # Sometimes fetch fails if msg isn't ready, just scan title

    async with database.get_db() as db:
        # Get all unique keywords to avoid spamming database calls
        async with db.execute("SELECT DISTINCT keyword FROM Watchlist") as cursor:
            all_keywords = await cursor.fetchall()
        
        matched_keywords = []
        for (kw,) in all_keywords:
            if kw in content_to_scan:
                matched_keywords.append(kw)
        
        # If we found matches, find WHO to alert
        if matched_keywords:
            placeholders = ','.join('?' for _ in matched_keywords)
            async with db.execute(f"SELECT DISTINCT user_id FROM Watchlist WHERE keyword IN ({placeholders})", tuple(matched_keywords)) as cursor:
                users_to_alert = await cursor.fetchall()
            
            # Send DMs
            for (uid,) in users_to_alert:
                if uid == owner.id: continue # Don't alert the author
                
                user_obj = thread.guild.get_member(uid)
                if user_obj:
                    try:
                        dm_embed = discord.Embed(
                            title="🔔 Market Alert!",
                            description=f"A new thread matched your watchlist.\n\n**Thread:** {thread.mention}\n**Matched:** {', '.join(matched_keywords)}",
                            color=discord.Color.blue()
                        )
                        await user_obj.send(embed=dm_embed)
                    except discord.Forbidden:
                        pass # User has DMs off

async def main():
    async with bot:
        await bot.start(os.getenv('DISCORD_TOKEN'))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
