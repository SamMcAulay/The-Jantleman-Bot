import os
import time
import jwt
from aiohttp import web, ClientSession
from discord.ext import commands

DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8


def _cors(request) -> dict:
    origin = os.getenv("DASHBOARD_ORIGIN", "*")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def _issue_token(user_id: int, guilds: list[int]) -> str:
    secret = os.getenv("DASHBOARD_SECRET_KEY", "changeme")
    payload = {
        "user_id": user_id,
        "guilds": guilds,
        "exp": int(time.time()) + 3600 * 8,  # 8-hour session
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _decode_token(token: str) -> dict:
    secret = os.getenv("DASHBOARD_SECRET_KEY", "changeme")
    return jwt.decode(token, secret, algorithms=["HS256"])


def _require_auth(request: web.Request, guild_id: int) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(reason="Missing token")
    try:
        payload = _decode_token(auth[7:])
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(reason="Token expired")
    except jwt.InvalidTokenError:
        raise web.HTTPUnauthorized(reason="Invalid token")
    if guild_id not in payload.get("guilds", []):
        raise web.HTTPForbidden(reason="No access to this guild")
    return payload


def _get_guilds_from_token(request: web.Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(reason="Missing token")
    try:
        return _decode_token(auth[7:])
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(reason="Token expired")
    except jwt.InvalidTokenError:
        raise web.HTTPUnauthorized(reason="Invalid token")


class Api(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.runner = None

    async def cog_load(self):
        app = web.Application()

        # Public
        app.router.add_get("/health", self.handle_health)

        # Auth
        app.router.add_get("/auth/login", self.handle_login)
        app.router.add_get("/auth/callback", self.handle_callback)

        # Preflight (CORS)
        for path in [
            "/api/guilds",
            "/api/settings/{guild_id}",
            "/api/channels/{guild_id}",
            "/api/channels/{guild_id}/{channel_id}",
            "/api/blacklist/{guild_id}",
            "/api/blacklist/{guild_id}/{user_id}",
            "/api/limits/{guild_id}",
            "/api/limits/{guild_id}/{user_id}",
        ]:
            app.router.add_route("OPTIONS", path, self.handle_preflight)

        # Dashboard API
        app.router.add_get("/api/guilds", self.handle_get_guilds)
        app.router.add_get("/api/settings/{guild_id}", self.handle_get_settings)
        app.router.add_post("/api/settings/{guild_id}", self.handle_post_settings)
        app.router.add_get("/api/channels/{guild_id}", self.handle_get_channels)
        app.router.add_post("/api/channels/{guild_id}", self.handle_add_channel)
        app.router.add_delete("/api/channels/{guild_id}/{channel_id}", self.handle_remove_channel)
        app.router.add_get("/api/blacklist/{guild_id}", self.handle_get_blacklist)
        app.router.add_post("/api/blacklist/{guild_id}", self.handle_add_blacklist)
        app.router.add_delete("/api/blacklist/{guild_id}/{user_id}", self.handle_remove_blacklist)
        app.router.add_get("/api/limits/{guild_id}", self.handle_get_limits)
        app.router.add_post("/api/limits/{guild_id}", self.handle_set_limit)
        app.router.add_delete("/api/limits/{guild_id}/{user_id}", self.handle_remove_limit)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = int(os.getenv("PORT", 8080))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        print(f"[API] Dashboard API running on port {port}")

    async def cog_unload(self):
        if self.runner:
            await self.runner.cleanup()

    # ── Public ─────────────────────────────────────────────────────────────────

    async def handle_health(self, request: web.Request):
        return web.Response(text="OK", headers=_cors(request))

    async def handle_preflight(self, request: web.Request):
        return web.Response(status=204, headers=_cors(request))

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    async def handle_login(self, request: web.Request):
        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        redirect_uri = os.getenv("DASHBOARD_REDIRECT_URI", "")
        discord_url = (
            f"https://discord.com/api/oauth2/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope=identify%20guilds"
        )
        raise web.HTTPFound(location=discord_url)

    async def handle_callback(self, request: web.Request):
        code = request.rel_url.query.get("code")
        if not code:
            raise web.HTTPBadRequest(reason="Missing code parameter")

        client_id = os.getenv("DISCORD_CLIENT_ID", "")
        client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
        redirect_uri = os.getenv("DASHBOARD_REDIRECT_URI", "")
        dashboard_url = os.getenv("DASHBOARD_ORIGIN", "")

        async with ClientSession() as session:
            token_res = await session.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_res.status != 200:
                raise web.HTTPBadGateway(reason="Discord token exchange failed")
            token_data = await token_res.json()
            access_token = token_data["access_token"]

            auth_headers = {"Authorization": f"Bearer {access_token}"}

            user_res = await session.get(f"{DISCORD_API}/users/@me", headers=auth_headers)
            user_data = await user_res.json()
            user_id = int(user_data["id"])

            guilds_res = await session.get(f"{DISCORD_API}/users/@me/guilds", headers=auth_headers)
            user_guilds = await guilds_res.json()

        bot_guild_ids = {g.id for g in self.bot.guilds}
        allowed_guilds = []
        for g in user_guilds:
            perms = int(g.get("permissions", 0))
            has_perm = bool(perms & ADMINISTRATOR) or bool(perms & MANAGE_GUILD)
            if has_perm and int(g["id"]) in bot_guild_ids:
                allowed_guilds.append(int(g["id"]))

        token = _issue_token(user_id, allowed_guilds)
        raise web.HTTPFound(location=f"{dashboard_url}#token={token}")

    # ── Guilds ────────────────────────────────────────────────────────────────

    async def handle_get_guilds(self, request: web.Request):
        payload = _get_guilds_from_token(request)
        guild_ids = payload.get("guilds", [])
        guilds = []
        for gid in guild_ids:
            g = self.bot.get_guild(gid)
            if g:
                guilds.append({
                    "id": str(g.id),
                    "name": g.name,
                    "icon": str(g.icon) if g.icon else None,
                })
        return web.json_response(guilds, headers=_cors(request))

    # ── Settings ──────────────────────────────────────────────────────────────

    async def handle_get_settings(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT track_identity, proof_req FROM Settings WHERE guild_id = ?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()

        data = {
            "track_identity": bool(row["track_identity"]) if row else True,
            "proof_req": row["proof_req"] if row else "required",
        }
        return web.json_response(data, headers=_cors(request))

    async def handle_post_settings(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")

        track_identity = bool(body.get("track_identity", True))
        proof_req = str(body.get("proof_req", "required"))
        if proof_req not in ("required", "optional", "off"):
            raise web.HTTPBadRequest(reason="Invalid proof_req value")

        import database
        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO Settings (guild_id, track_identity, proof_req)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       track_identity = excluded.track_identity,
                       proof_req = excluded.proof_req""",
                (guild_id, track_identity, proof_req),
            )
            await db.commit()

        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Monitored Channels ─────────────────────────────────────────────────────

    async def handle_get_channels(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT channel_id, channel_name FROM MonitoredChannels WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                rows = await cursor.fetchall()

        channels = [
            {"channel_id": str(r["channel_id"]), "channel_name": r["channel_name"] or str(r["channel_id"])}
            for r in rows
        ]
        return web.json_response(channels, headers=_cors(request))

    async def handle_add_channel(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")

        channel_id = body.get("channel_id")
        channel_name = str(body.get("channel_name", "")).strip() or str(channel_id)
        if not channel_id:
            raise web.HTTPBadRequest(reason="channel_id is required")
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="channel_id must be a valid integer")

        import database
        async with database.get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id, channel_name) VALUES (?, ?, ?)",
                (guild_id, channel_id, channel_name),
            )
            await db.commit()

        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_channel(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        channel_id = int(request.match_info["channel_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "DELETE FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            await db.commit()

        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Blacklist ──────────────────────────────────────────────────────────────

    async def handle_get_blacklist(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id FROM Users WHERE is_blacklisted = 1"
            ) as cursor:
                rows = await cursor.fetchall()

        result = []
        for r in rows:
            uid = r["user_id"]
            member = None
            guild = self.bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(uid)
            result.append({
                "user_id": str(uid),
                "username": member.display_name if member else str(uid),
            })

        return web.json_response(result, headers=_cors(request))

    async def handle_add_blacklist(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")

        user_id = body.get("user_id")
        if not user_id:
            raise web.HTTPBadRequest(reason="user_id is required")
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="user_id must be a valid integer")

        import database
        async with database.get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user_id,)
            )
            await db.execute(
                "UPDATE Users SET is_blacklisted = 1 WHERE user_id = ?", (user_id,)
            )
            await db.commit()

        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_blacklist(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "UPDATE Users SET is_blacklisted = 0 WHERE user_id = ?", (user_id,)
            )
            await db.commit()

        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Posting Limits ─────────────────────────────────────────────────────────

    async def handle_get_limits(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, post_limit_hours FROM Users WHERE post_limit_hours IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()

        result = []
        guild = self.bot.get_guild(guild_id)
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid) if guild else None
            result.append({
                "user_id": str(uid),
                "username": member.display_name if member else str(uid),
                "post_limit_hours": r["post_limit_hours"],
            })

        return web.json_response(result, headers=_cors(request))

    async def handle_set_limit(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")

        user_id = body.get("user_id")
        hours = body.get("hours")
        if not user_id or hours is None:
            raise web.HTTPBadRequest(reason="user_id and hours are required")
        try:
            user_id = int(user_id)
            hours = int(hours)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="Invalid user_id or hours")
        if hours < 1:
            raise web.HTTPBadRequest(reason="hours must be at least 1")

        import database
        async with database.get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user_id,)
            )
            await db.execute(
                "UPDATE Users SET post_limit_hours = ? WHERE user_id = ?", (hours, user_id)
            )
            await db.commit()

        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_limit(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "UPDATE Users SET post_limit_hours = NULL WHERE user_id = ?", (user_id,)
            )
            await db.commit()

        return web.json_response({"ok": True}, headers=_cors(request))


async def setup(bot):
    await bot.add_cog(Api(bot))
