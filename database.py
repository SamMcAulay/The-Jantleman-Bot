import aiosqlite
import logging
import os
from pathlib import Path

# Check if we are running on Railway (Volume mounted at /app/data)
# If the folder exists, use it. Otherwise, use local directory.
VOLUME_PATH = Path("/app/data")
if VOLUME_PATH.exists():
    DB_PATH = VOLUME_PATH / "reputation.db"
    logging.info("Using PRODUCTION database at /app/data/reputation.db")
else:
    DB_PATH = Path(__file__).parent / "reputation.db"
    logging.info("Using LOCAL database")

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
