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
        await self.load_extension("cogs.api")
        await self.load_extension("cogs.reputation")
        await self.load_extension("cogs.watchlist")
        await self.tree.sync()
        logging.info("System Online. Extensions Loaded.")

bot = JantlemanBot()

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user}")
    await database.backfill_review_guild_ids(bot)

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

        # Load guild settings first so all checks can use them
        async with db.execute(
            "SELECT * FROM Settings WHERE guild_id = ?", (thread.guild.id,)
        ) as cursor:
            settings = await cursor.fetchone()

        s_track_identity       = settings["track_identity"]        if settings else True
        s_min_reviews          = settings["min_reviews"]           if settings and settings["min_reviews"] is not None else 1
        s_global_limit         = settings["global_post_limit_hours"] if settings else None
        s_auto_delete_new      = bool(settings["auto_delete_new"]) if settings else False
        s_alert_channel_id     = settings["alert_channel_id"]      if settings else None

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

        # Per-user limit, falling back to server-wide global limit
        effective_limit = user_data["post_limit_hours"] if user_data else None
        if effective_limit is None:
            effective_limit = s_global_limit

        if effective_limit and user_data and user_data["last_post_timestamp"]:
            last_post = datetime.strptime(
                user_data["last_post_timestamp"], "%Y-%m-%d %H:%M:%S"
            )
            diff = datetime.now() - last_post
            if diff < timedelta(hours=effective_limit):
                remaining = int(
                    (timedelta(hours=effective_limit) - diff).total_seconds() / 60
                )
                try:
                    await thread.delete()
                    await owner.send(
                        f"⏱️ **Cooldown Active**\nYou are limited to one post every {effective_limit} hours.\nPlease wait **{remaining} minutes** before posting again."
                    )
                except:
                    pass
                return

        # Auto-delete threads from users below the minimum review threshold
        review_count = user_data["total_reviews"] if user_data else 0
        if s_auto_delete_new and review_count < s_min_reviews:
            try:
                await thread.delete()
                await owner.send(
                    f"⛔ **Post Removed**\n"
                    f"This channel requires at least **{s_min_reviews}** verified review(s) to post.\n"
                    f"Build your reputation elsewhere first!"
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

        change_count = 0
        if s_track_identity:
            seven_days_ago = datetime.now() - timedelta(days=7)
            async with db.execute(
                "SELECT COUNT(*) FROM NameHistory WHERE user_id = ? AND timestamp > ?",
                (owner.id, seven_days_ago),
            ) as cursor:
                name_changes = await cursor.fetchone()
                change_count = name_changes[0] if name_changes else 0

    embed = discord.Embed(timestamp=datetime.now())
    embed.set_footer(
        text="The Jantleman • Automated Check", icon_url=bot.user.display_avatar.url
    )
    embed.set_thumbnail(url=owner.display_avatar.url)

    if not user_data or review_count < s_min_reviews:
        embed.title = "⚠️ New Member Alert"
        embed.color = 0xFFA500
        desc = f"**User:** {owner.mention}\nHas **{review_count}** recorded review(s)."
    else:
        avg = round(user_data["total_stars"] / review_count, 1)
        stars = ("⭐" * int(avg)) + ("☆" * (5 - int(avg)))
        embed.title = "✅ Established Member"
        embed.color = discord.Color.gold()
        desc = f"**User:** {owner.mention}\n**Rating:** {stars} ({avg}/5)\n**Reviews:** {review_count}"

    if s_track_identity and change_count > 0:
        embed.add_field(
            name="⚠️ Identity Alert",
            value=f"Changed name **{change_count} times** in the last 7 days.",
            inline=False,
        )
        if change_count >= 3:
            embed.color = discord.Color.red()
            embed.title = "🛑 High Risk Alert"

    embed.description = desc
    
    max_retries = 50
    retry_delay = 2

    for attempt in range(max_retries):
        try:
            await thread.send(embed=embed)
            if attempt > 0:
                logging.info(f"✅ Success: Posted to thread {thread.id} after {attempt * retry_delay}s delay.")
            break
        
        except discord.Forbidden as e:
            if e.code == 40058:
                if attempt % 2 == 0:
                    logging.warning(f"⏳ Upload in progress for thread {thread.id}... Waiting {retry_delay}s (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(retry_delay)
            else:
                logging.error(f"❌ Failed to post in thread {thread.id}: {e}")
                break
        
        except Exception as e:
            logging.error(f"❌ Unexpected error in thread {thread.id}: {e}")
            break
    else:
        logging.error(f"❌ TIMEOUT: Gave up on thread {thread.id} after 3 minutes. Upload stuck or abandoned.")
        return

    # Post to alert channel if configured and the embed is a warning/risk
    if s_alert_channel_id and embed.title and any(w in embed.title for w in ("⚠️", "🛑")):
        alert_ch = thread.guild.get_channel(s_alert_channel_id)
        if alert_ch:
            try:
                await alert_ch.send(
                    f"🚨 **Flag in {thread.parent.mention}:** {thread.mention}\n"
                    f"**User:** {owner.mention} — {embed.title}",
                )
            except Exception:
                pass

    content_to_scan = thread.name.lower()

    try:
        starter_msg = await thread.fetch_message(thread.id)
        if starter_msg:
            content_to_scan += " " + starter_msg.content.lower()
    except:
        pass

    async with database.get_db() as db:
        async with db.execute("SELECT DISTINCT keyword FROM Watchlist") as cursor:
            all_keywords = await cursor.fetchall()

        matched_keywords = []
        for (kw,) in all_keywords:
            if kw in content_to_scan:
                matched_keywords.append(kw)

        if matched_keywords:
            placeholders = ",".join("?" for _ in matched_keywords)
            async with db.execute(
                f"SELECT DISTINCT user_id FROM Watchlist WHERE keyword IN ({placeholders})",
                tuple(matched_keywords),
            ) as cursor:
                users_to_alert = await cursor.fetchall()

            for (uid,) in users_to_alert:
                if uid == owner.id:
                    continue
                user_obj = thread.guild.get_member(uid)
                if user_obj:
                    try:
                        dm_embed = discord.Embed(
                            title="🔔 Market Alert!",
                            description=f"A new thread matched your watchlist.\n\n**Thread:** {thread.mention}\n**Matched:** {', '.join(matched_keywords)}",
                            color=discord.Color.blue(),
                        )
                        await user_obj.send(embed=dm_embed)
                    except discord.Forbidden:
                        pass

async def main():
    async with bot:
        await bot.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
