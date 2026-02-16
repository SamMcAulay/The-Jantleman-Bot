import discord
from discord.ext import commands
import os
import logging
import asyncio
from dotenv import load_dotenv
import database
import aiosqlite
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
load_dotenv()

class JantlemanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await database.init_db()
        await self.load_extension("cogs.reputation")
        await self.load_extension("cogs.watchlist")
        await self.tree.sync()
        logging.info("System Online. Extensions Loaded.")

bot = JantlemanBot()

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")

@bot.event
async def on_member_update(before, after):
    if before.display_name != after.display_name:
        async with database.get_db() as db:
            await db.execute(
                "INSERT INTO NameHistory (user_id, old_name, new_name) VALUES (?, ?, ?)",
                (after.id, before.display_name, after.display_name),
            )
            await db.commit()

@bot.event
async def on_thread_create(thread):
    if not isinstance(thread.parent, discord.ForumChannel):
        return

    async with database.get_db() as db:
        async with db.execute(
            "SELECT 1 FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?",
            (thread.guild.id, thread.parent_id),
        ) as cursor:
            is_monitored = await cursor.fetchone()

    if not is_monitored:
        return

    owner = thread.owner

    if not owner and thread.owner_id:
        try:
            owner = await thread.guild.fetch_member(thread.owner_id)
        except Exception:
            pass

    if not owner:
        try:
            await asyncio.sleep(1)
            starter_message = await thread.fetch_message(thread.id)
            owner = starter_message.author
        except Exception:
            pass

    if not owner:
        logging.error(f"❌ Could not determine owner for thread {thread.id}. Skipping.")
        return

    async with database.get_db() as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT * FROM Users WHERE user_id = ?", (owner.id,)
        ) as cursor:
            user_data = await cursor.fetchone()

        if user_data and user_data["is_blacklisted"]:
            try:
                await thread.delete()
                await owner.send(
                    f"⛔ **Access Denied.**\nYou are blacklisted from posting in {thread.parent.mention}."
                )
            except:
                pass
            return

        if (
            user_data
            and user_data["post_limit_hours"]
            and user_data["last_post_timestamp"]
        ):
            last_post = datetime.strptime(
                user_data["last_post_timestamp"], "%Y-%m-%d %H:%M:%S"
            )
            diff = datetime.now() - last_post
            limit_hours = user_data["post_limit_hours"]

            if diff < timedelta(hours=limit_hours):
                remaining = int(
                    (timedelta(hours=limit_hours) - diff).total_seconds() / 60
                )
                try:
                    await thread.delete()
                    await owner.send(
                        f"⏱️ **Cooldown Active**\nYou are limited to one post every {limit_hours} hours.\nPlease wait **{remaining} minutes** before posting again."
                    )
                except:
                    pass
                return

        await db.execute(
            "INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (owner.id,)
        )
        await db.execute(
            "UPDATE Users SET last_post_timestamp = ? WHERE user_id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), owner.id),
        )
        await db.commit()

        async with db.execute(
            "SELECT track_identity FROM Settings WHERE guild_id = ?", (thread.guild.id,)
        ) as cursor:
            settings = await cursor.fetchone()
            tracking_enabled = settings["track_identity"] if settings else True

        change_count = 0
        if tracking_enabled:
            seven_days_ago = datetime.now() - timedelta(days=7)
            async with db.execute(
                "SELECT COUNT(*) FROM NameHistory WHERE user_id = ? AND timestamp > ?",
                (owner.id, seven_days_ago),
            ) as cursor:
                name_changes = await cursor
