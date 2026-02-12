import aiosqlite
import logging
from pathlib import Path

# Use pathlib for robust cross-platform paths
DB_PATH = Path(__file__).parent / "reputation.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS Users (
            user_id INTEGER PRIMARY KEY,
            total_stars INTEGER DEFAULT 0,
            total_reviews INTEGER DEFAULT 0,
            is_blacklisted BOOLEAN DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS Reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER,
            author_id INTEGER,
            stars INTEGER,
            comment TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS Settings (
            guild_id INTEGER PRIMARY KEY,
            forum_channel_id INTEGER
        )''')
        await db.commit()
    logging.info(f"Database initialized at {DB_PATH}")

def get_db():
    """Returns an async context manager for the database."""
    return aiosqlite.connect(DB_PATH)
