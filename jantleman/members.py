"""Member lookups that work without the privileged GUILD_MEMBERS intent.

Without that intent the member cache is mostly empty, so guild.get_member()
returns None for almost everyone. These helpers fall back to the REST API
(which needs no intent) and cache results briefly to avoid rate limits.
"""
import asyncio
import time

import discord

_TTL = 600  # seconds
_cache: dict = {}  # (guild_id, user_id) -> (expires_at, Member | None)
_fetch_sem = asyncio.Semaphore(5)


async def resolve_member(guild: discord.Guild, user_id: int):
    """Return the Member for user_id in guild, or None if they aren't in it."""
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member:
        return member

    key = (guild.id, user_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    async with _fetch_sem:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            return None  # transient — don't cache
    _cache[key] = (time.monotonic() + _TTL, member)
    return member


async def resolve_members(guild: discord.Guild, user_ids) -> dict:
    """Resolve many members concurrently. Returns {user_id: Member | None}."""
    ids = list(dict.fromkeys(user_ids))
    results = await asyncio.gather(*(resolve_member(guild, uid) for uid in ids))
    return dict(zip(ids, results))
