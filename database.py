import aiosqlite
import logging
from pathlib import Path

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
            proof_url TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS Settings (
            guild_id INTEGER PRIMARY KEY,
            forum_channel_id INTEGER,
            verified_role_id INTEGER,
            audit_role_id INTEGER
        )''')

        # Identity Tracking
        await db.execute('''CREATE TABLE IF NOT EXISTS NameHistory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            old_name TEXT,
            new_name TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        # Watchlist
        await db.execute('''CREATE TABLE IF NOT EXISTS Watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            keyword TEXT
        )''')
        
        await db.commit()
    logging.info(f"Database initialized at {DB_PATH}")

def get_db():
    return aiosqlite.connect(DB_PATH)
