import aiosqlite
import logging
from pathlib import Path

VOLUME_PATH = Path("/app/data")
if VOLUME_PATH.exists():
    DB_PATH = VOLUME_PATH / "reputation.db"
else:
    DB_PATH = Path(__file__).parent / "reputation.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS Users (user_id INTEGER PRIMARY KEY, total_stars INTEGER DEFAULT 0, total_reviews INTEGER DEFAULT 0, is_blacklisted BOOLEAN DEFAULT 0,post_limit_hours INTEGER DEFAULT NULL,last_post_timestamp DATETIME DEFAULT NULL, review_banned BOOLEAN DEFAULT 0)"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS Reviews (review_id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER, author_id INTEGER, stars INTEGER, comment TEXT, proof_url TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS NameHistory (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, old_name TEXT, new_name TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS Watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, keyword TEXT)"
        )
        await db.execute("""CREATE TABLE IF NOT EXISTS Settings (guild_id INTEGER PRIMARY KEY,verified_role_id INTEGER,audit_role_id INTEGER,
            track_identity BOOLEAN DEFAULT 1,
            proof_req TEXT DEFAULT 'required'
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS MonitoredChannels (
            guild_id INTEGER,
            channel_id INTEGER,
            channel_name TEXT DEFAULT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )""")
        # Safe migrations
        for migration in [
            "ALTER TABLE MonitoredChannels ADD COLUMN channel_name TEXT DEFAULT NULL",
            "ALTER TABLE Settings ADD COLUMN min_reviews INTEGER DEFAULT 1",
            "ALTER TABLE Settings ADD COLUMN global_post_limit_hours INTEGER DEFAULT NULL",
            "ALTER TABLE Settings ADD COLUMN auto_delete_new BOOLEAN DEFAULT 0",
            "ALTER TABLE Settings ADD COLUMN alert_channel_id INTEGER DEFAULT NULL",
            "ALTER TABLE Reviews ADD COLUMN guild_id INTEGER DEFAULT NULL",
        ]:
            try:
                await db.execute(migration)
            except Exception:
                pass
        await db.execute("""CREATE TABLE IF NOT EXISTS GuildRoles (
            guild_id INTEGER,
            role_id INTEGER,
            role_type TEXT,
            PRIMARY KEY (guild_id, role_id, role_type)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS AuditLog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            guild_id INTEGER DEFAULT NULL,
            target_id INTEGER DEFAULT NULL,
            details TEXT DEFAULT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        await db.commit()
    logging.info(f"Database initialized at {DB_PATH}")


def get_db():
    return aiosqlite.connect(DB_PATH)


async def backfill_review_guild_ids(bot):
    """One-time migration: assign guild_id to reviews created before guild scoping."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT review_id, target_id, author_id FROM Reviews WHERE guild_id IS NULL"
        ) as cursor:
            reviews = await cursor.fetchall()

        if not reviews:
            return

        updated = 0
        for review in reviews:
            rid = review["review_id"]
            target_id = review["target_id"]
            author_id = review["author_id"]

            # Prefer guilds where the target (reviewed person) is a current member
            target_guilds = [g.id for g in bot.guilds if g.get_member(target_id)]
            if len(target_guilds) == 1:
                guild_id = target_guilds[0]
            else:
                # Fall back to guilds where both reviewer and target are members
                both_guilds = [
                    g.id for g in bot.guilds
                    if g.get_member(target_id) and g.get_member(author_id)
                ]
                if len(both_guilds) == 1:
                    guild_id = both_guilds[0]
                elif len(bot.guilds) == 1:
                    guild_id = bot.guilds[0].id
                else:
                    continue  # Ambiguous — skip

            await db.execute(
                "UPDATE Reviews SET guild_id = ? WHERE review_id = ?", (guild_id, rid)
            )
            updated += 1

        if updated:
            await db.commit()
            logging.info(f"[Migration] Backfilled guild_id for {updated}/{len(reviews)} reviews.")


async def log_admin_action(admin_id: int, action: str, guild_id: int = None, target_id: int = None, details: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO AuditLog (admin_id, action, guild_id, target_id, details) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, guild_id, target_id, details),
        )
        await db.commit()
