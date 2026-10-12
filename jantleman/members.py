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
_MAX_CACHE = 5000
_fetch_sem = asyncio.Semaphore(5)


async def resolve_member(guild: discord.Guild, user_id: int, fresh: bool = False):
    """Return the Member for user_id in guild, or None if they aren't in it.

    Pass fresh=True for authorization checks so revoked roles aren't served
    from a stale cache entry.
    """
    if guild is None:
        return None
    key = (guild.id, user_id)
    if not fresh:
        member = guild.get_member(user_id)
        if member:
            return member
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
    if len(_cache) >= _MAX_CACHE:
        now = time.monotonic()
        for k in [k for k, v in _cache.items() if v[0] <= now]:
            del _cache[k]
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
    _cache[key] = (time.monotonic() + _TTL, member)
    return member


def _is_cached(guild: discord.Guild, user_id: int) -> bool:
    if guild.get_member(user_id):
        return True
    hit = _cache.get((guild.id, user_id))
    return bool(hit and hit[0] > time.monotonic())


async def resolve_members(guild: discord.Guild, user_ids, max_fetch: int = 25) -> dict:
    """Resolve many members concurrently. Returns {user_id: Member | None}.

    At most max_fetch uncached members are fetched over REST per call so a
    single large request can't burn the bot's shared rate limit; the rest
    come back as None (callers fall back to showing the ID).
    """
    if guild is None:
        return {}
    ids = list(dict.fromkeys(user_ids))
    to_resolve, fetches = [], 0
    for uid in ids:
        if _is_cached(guild, uid):
            to_resolve.append(uid)
        elif fetches < max_fetch:
            to_resolve.append(uid)
            fetches += 1
    results = await asyncio.gather(*(resolve_member(guild, uid) for uid in to_resolve))
    return dict(zip(to_resolve, results))
