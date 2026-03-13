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


def _issue_token(user_id: int, guilds: list[int], is_admin: bool = False) -> str:
    secret = os.getenv("DASHBOARD_SECRET_KEY", "changeme")
    payload = {
        "user_id": user_id,
        "guilds": guilds,
        "is_admin": is_admin,
        "exp": int(time.time()) + 3600 * 8,
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
    # Admins bypass guild membership check
    if payload.get("is_admin"):
        return payload
    if guild_id not in payload.get("guilds", []):
        raise web.HTTPForbidden(reason="No access to this guild")
    return payload


def _require_admin(request: web.Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(reason="Missing token")
    try:
        payload = _decode_token(auth[7:])
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(reason="Token expired")
    except jwt.InvalidTokenError:
        raise web.HTTPUnauthorized(reason="Invalid token")
    if not payload.get("is_admin"):
        raise web.HTTPForbidden(reason="Admin access required")
    return payload


def _get_token_payload(request: web.Request) -> dict:
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
        self.start_time = None

    async def cog_load(self):
        app = web.Application()

        # Public
        app.router.add_get("/health", self.handle_health)

        # Auth
        app.router.add_get("/auth/login", self.handle_login)
        app.router.add_get("/auth/callback", self.handle_callback)

        # CORS preflight
        for path in [
            "/api/guilds",
            "/api/settings/{guild_id}",
            "/api/channels/{guild_id}",
            "/api/channels/{guild_id}/{channel_id}",
            "/api/blacklist/{guild_id}",
            "/api/blacklist/{guild_id}/{user_id}",
            "/api/limits/{guild_id}",
            "/api/limits/{guild_id}/{user_id}",
            "/api/members/{guild_id}",
            "/api/reviews/{guild_id}/{user_id}",
            "/api/reviewbans/{guild_id}",
            "/api/reviewbans/{guild_id}/{user_id}",
            "/admin/guilds",
            "/admin/stats",
            "/admin/guild/{guild_id}/users",
            "/admin/guild/{guild_id}/leave",
            "/admin/user/{user_id}",
            "/admin/audit-log",
        ]:
            app.router.add_route("OPTIONS", path, self.handle_preflight)

        # Dashboard API (requires guild membership or admin)
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
        app.router.add_get("/api/members/{guild_id}", self.handle_get_members)
        app.router.add_get("/api/reviews/{guild_id}/{user_id}", self.handle_get_user_reviews)
        app.router.add_get("/api/reviewbans/{guild_id}", self.handle_get_reviewbans)
        app.router.add_post("/api/reviewbans/{guild_id}", self.handle_add_reviewban)
        app.router.add_delete("/api/reviewbans/{guild_id}/{user_id}", self.handle_remove_reviewban)

        # Admin API (requires is_admin claim)
        app.router.add_get("/admin/guilds", self.handle_admin_guilds)
        app.router.add_get("/admin/stats", self.handle_admin_stats)
        app.router.add_get("/admin/guild/{guild_id}/users", self.handle_admin_guild_users)
        app.router.add_post("/admin/guild/{guild_id}/leave", self.handle_admin_leave_guild)
        app.router.add_get("/admin/user/{user_id}", self.handle_admin_user_lookup)
        app.router.add_get("/admin/audit-log", self.handle_admin_audit_log)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = int(os.getenv("PORT", 8080))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        print(f"[API] Dashboard API running on port {port}")

    async def cog_unload(self):
        if self.runner:
            await self.runner.cleanup()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.start_time is None:
            self.start_time = time.time()

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

        admin_ids = {
            int(x.strip())
            for x in os.getenv("ADMIN_USER_IDS", "").split(",")
            if x.strip().isdigit()
        }
        is_admin = user_id in admin_ids

        token = _issue_token(user_id, allowed_guilds, is_admin=is_admin)
        raise web.HTTPFound(location=f"{dashboard_url}#token={token}")

    # ── Guilds ────────────────────────────────────────────────────────────────

    async def handle_get_guilds(self, request: web.Request):
        payload = _get_token_payload(request)
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
                "SELECT * FROM Settings WHERE guild_id = ?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()

        data = {
            "track_identity":           bool(row["track_identity"])        if row else True,
            "proof_req":                row["proof_req"]                   if row else "required",
            "verified_role_id":         str(row["verified_role_id"])       if row and row["verified_role_id"] else "",
            "audit_role_id":            str(row["audit_role_id"])          if row and row["audit_role_id"] else "",
            "min_reviews":              row["min_reviews"]                 if row and row["min_reviews"] is not None else 1,
            "global_post_limit_hours":  row["global_post_limit_hours"]     if row else None,
            "auto_delete_new":          bool(row["auto_delete_new"])       if row else False,
            "alert_channel_id":         str(row["alert_channel_id"])       if row and row["alert_channel_id"] else "",
        }
        return web.json_response(data, headers=_cors(request))

    async def handle_post_settings(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")

        track_identity          = bool(body.get("track_identity", True))
        proof_req               = str(body.get("proof_req", "required"))
        auto_delete_new         = bool(body.get("auto_delete_new", False))
        min_reviews             = int(body.get("min_reviews", 1))
        global_post_limit_hours = body.get("global_post_limit_hours")
        verified_role_id        = body.get("verified_role_id") or None
        audit_role_id           = body.get("audit_role_id") or None
        alert_channel_id        = body.get("alert_channel_id") or None

        if proof_req not in ("required", "optional", "off"):
            raise web.HTTPBadRequest(reason="Invalid proof_req value")
        if min_reviews < 0:
            raise web.HTTPBadRequest(reason="min_reviews must be >= 0")

        def to_int_or_none(v):
            try:
                return int(v) if v else None
            except (TypeError, ValueError):
                return None

        import database
        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO Settings (guild_id, track_identity, proof_req, verified_role_id,
                       audit_role_id, min_reviews, global_post_limit_hours, auto_delete_new, alert_channel_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       track_identity          = excluded.track_identity,
                       proof_req               = excluded.proof_req,
                       verified_role_id        = excluded.verified_role_id,
                       audit_role_id           = excluded.audit_role_id,
                       min_reviews             = excluded.min_reviews,
                       global_post_limit_hours = excluded.global_post_limit_hours,
                       auto_delete_new         = excluded.auto_delete_new,
                       alert_channel_id        = excluded.alert_channel_id""",
                (guild_id, track_identity, proof_req,
                 to_int_or_none(verified_role_id), to_int_or_none(audit_role_id),
                 min_reviews, to_int_or_none(global_post_limit_hours),
                 auto_delete_new, to_int_or_none(alert_channel_id)),
            )
            await db.commit()

        await database.log_admin_action(payload["user_id"], "update_settings", guild_id=guild_id)
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
        payload = _require_auth(request, guild_id)

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

        await database.log_admin_action(payload["user_id"], "add_channel", guild_id=guild_id, target_id=channel_id)
        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_channel(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)
        channel_id = int(request.match_info["channel_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "DELETE FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            await db.commit()

        await database.log_admin_action(payload["user_id"], "remove_channel", guild_id=guild_id, target_id=channel_id)
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
        guild = self.bot.get_guild(guild_id)
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid) if guild else None
            result.append({
                "user_id": str(uid),
                "username": member.display_name if member else str(uid),
            })

        return web.json_response(result, headers=_cors(request))

    async def handle_add_blacklist(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)

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

        await database.log_admin_action(payload["user_id"], "blacklist_add", guild_id=guild_id, target_id=user_id)
        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_blacklist(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "UPDATE Users SET is_blacklisted = 0 WHERE user_id = ?", (user_id,)
            )
            await db.commit()

        await database.log_admin_action(payload["user_id"], "blacklist_remove", guild_id=guild_id, target_id=user_id)
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
        payload = _require_auth(request, guild_id)

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

        await database.log_admin_action(payload["user_id"], "limit_set", guild_id=guild_id, target_id=user_id, details=f"{hours}h")
        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_limit(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])

        import database
        async with database.get_db() as db:
            await db.execute(
                "UPDATE Users SET post_limit_hours = NULL WHERE user_id = ?", (user_id,)
            )
            await db.commit()

        await database.log_admin_action(payload["user_id"], "limit_remove", guild_id=guild_id, target_id=user_id)
        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _refresh_discord_urls(self, urls: list) -> dict:
        """Refresh expiring Discord CDN attachment URLs via the Discord API."""
        cdn_urls = [u for u in urls if u and u.startswith("https://cdn.discordapp.com/attachments/")]
        if not cdn_urls:
            return {}
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        try:
            async with ClientSession() as session:
                res = await session.post(
                    "https://discord.com/api/v10/attachments/refresh-urls",
                    json={"attachment_urls": cdn_urls},
                    headers={"Authorization": f"Bot {token}"},
                )
                if res.status != 200:
                    return {}
                data = await res.json()
                return {item["original"]: item["refreshed"] for item in data.get("refreshed_urls", [])}
        except Exception:
            return {}

    # ── Members & Reviews ──────────────────────────────────────────────────────

    async def handle_get_members(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        import database
        import aiosqlite

        guild = self.bot.get_guild(guild_id)
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT u.user_id, u.is_blacklisted, u.post_limit_hours,
                          COUNT(r.review_id) AS guild_reviews,
                          ROUND(AVG(r.stars), 1) AS guild_avg
                   FROM Users u
                   INNER JOIN Reviews r ON r.target_id = u.user_id AND r.guild_id = ?
                   GROUP BY u.user_id
                   ORDER BY guild_reviews DESC""",
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()

        members = []
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid) if guild else None
            members.append({
                "user_id": str(uid),
                "username": member.display_name if member else None,
                "avatar": str(member.display_avatar.url) if member else None,
                "total_reviews": r["guild_reviews"],
                "avg_rating": r["guild_avg"] or 0,
                "is_blacklisted": bool(r["is_blacklisted"]),
                "post_limit_hours": r["post_limit_hours"],
            })

        return web.json_response(members, headers=_cors(request))

    async def handle_get_user_reviews(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])
        import database
        import aiosqlite

        guild = self.bot.get_guild(guild_id)
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT stars, comment, proof_url, author_id, timestamp
                   FROM Reviews WHERE target_id = ? AND guild_id = ?
                   ORDER BY timestamp DESC""",
                (user_id, guild_id)
            ) as cursor:
                rows = await cursor.fetchall()

        raw_proof_urls = [r["proof_url"] for r in rows]
        url_map = await self._refresh_discord_urls(raw_proof_urls)

        reviews = []
        for r in rows:
            aid = r["author_id"]
            author = guild.get_member(aid) if guild else None
            raw_url = r["proof_url"]
            proof_url = url_map.get(raw_url, raw_url) if raw_url and raw_url.startswith("https://cdn.discordapp.com/") else raw_url
            # Normalise old "No Proof Provided" sentinel to null
            if proof_url and not proof_url.startswith("http"):
                proof_url = None
            reviews.append({
                "stars": r["stars"],
                "comment": r["comment"],
                "proof_url": proof_url,
                "author_id": str(aid),
                "author_name": author.display_name if author else str(aid),
                "timestamp": r["timestamp"],
            })

        return web.json_response(reviews, headers=_cors(request))

    # ── Review Bans ────────────────────────────────────────────────────────────

    async def handle_get_reviewbans(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        _require_auth(request, guild_id)
        import database, aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id FROM Users WHERE review_banned = 1"
            ) as cursor:
                rows = await cursor.fetchall()

        guild = self.bot.get_guild(guild_id)
        result = []
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid) if guild else None
            result.append({
                "user_id": str(uid),
                "username": member.display_name if member else str(uid),
            })
        return web.json_response(result, headers=_cors(request))

    async def handle_add_reviewban(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Invalid JSON")
        user_id = body.get("user_id")
        if not user_id:
            raise web.HTTPBadRequest(reason="user_id required")
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="Invalid user_id")

        import database
        async with database.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user_id,))
            await db.execute("UPDATE Users SET review_banned = 1 WHERE user_id = ?", (user_id,))
            await db.commit()

        await database.log_admin_action(payload["user_id"], "reviewban_add", guild_id=guild_id, target_id=user_id)
        return web.json_response({"ok": True}, status=201, headers=_cors(request))

    async def handle_remove_reviewban(self, request: web.Request):
        guild_id = int(request.match_info["guild_id"])
        payload = _require_auth(request, guild_id)
        user_id = int(request.match_info["user_id"])

        import database
        async with database.get_db() as db:
            await db.execute("UPDATE Users SET review_banned = 0 WHERE user_id = ?", (user_id,))
            await db.commit()

        await database.log_admin_action(payload["user_id"], "reviewban_remove", guild_id=guild_id, target_id=user_id)
        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Admin: All Guilds ──────────────────────────────────────────────────────

    async def handle_admin_guilds(self, request: web.Request):
        _require_admin(request)
        import database
        import aiosqlite

        guilds = []
        for g in self.bot.guilds:
            async with database.get_db() as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM MonitoredChannels WHERE guild_id = ?", (g.id,)
                ) as cursor:
                    ch_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM Users WHERE total_reviews > 0"
                ) as cursor:
                    users_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT track_identity, proof_req FROM Settings WHERE guild_id = ?", (g.id,)
                ) as cursor:
                    settings_row = await cursor.fetchone()

            guilds.append({
                "id": str(g.id),
                "name": g.name,
                "icon": str(g.icon) if g.icon else None,
                "member_count": g.member_count,
                "monitored_channels": ch_row["cnt"] if ch_row else 0,
                "tracked_users": users_row["cnt"] if users_row else 0,
                "track_identity": bool(settings_row["track_identity"]) if settings_row else True,
                "proof_req": settings_row["proof_req"] if settings_row else "required",
            })

        guilds.sort(key=lambda x: x["name"].lower())
        return web.json_response({"guilds": guilds}, headers=_cors(request))

    # ── Admin: Stats ───────────────────────────────────────────────────────────

    async def handle_admin_stats(self, request: web.Request):
        _require_admin(request)
        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT COUNT(*) as cnt FROM Users WHERE total_reviews > 0") as cursor:
                users_row = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) as cnt FROM Reviews") as cursor:
                reviews_row = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) as cnt FROM Users WHERE is_blacklisted = 1") as cursor:
                bl_row = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) as cnt FROM Watchlist") as cursor:
                wl_row = await cursor.fetchone()

        uptime = int(time.time() - self.start_time) if self.start_time else 0
        return web.json_response({
            "uptime_seconds": uptime,
            "guild_count": len(self.bot.guilds),
            "latency_ms": round(self.bot.latency * 1000, 1),
            "tracked_users": users_row["cnt"] if users_row else 0,
            "total_reviews": reviews_row["cnt"] if reviews_row else 0,
            "blacklisted_users": bl_row["cnt"] if bl_row else 0,
            "watchlist_entries": wl_row["cnt"] if wl_row else 0,
        }, headers=_cors(request))

    # ── Admin: Guild Users ─────────────────────────────────────────────────────

    async def handle_admin_guild_users(self, request: web.Request):
        _require_admin(request)
        guild_id = int(request.match_info["guild_id"])
        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT u.user_id, u.total_stars, u.total_reviews, u.is_blacklisted, u.post_limit_hours
                   FROM Users u
                   WHERE u.total_reviews > 0
                   ORDER BY u.total_reviews DESC
                   LIMIT 50"""
            ) as cursor:
                rows = await cursor.fetchall()

        guild = self.bot.get_guild(guild_id)
        users = []
        for r in rows:
            uid = r["user_id"]
            member = guild.get_member(uid) if guild else None
            avg = round(r["total_stars"] / r["total_reviews"], 1) if r["total_reviews"] > 0 else 0
            users.append({
                "user_id": str(uid),
                "username": member.display_name if member else str(uid),
                "avatar": str(member.display_avatar.url) if member else None,
                "total_reviews": r["total_reviews"],
                "avg_rating": avg,
                "is_blacklisted": bool(r["is_blacklisted"]),
                "post_limit_hours": r["post_limit_hours"],
            })

        return web.json_response({"users": users}, headers=_cors(request))

    # ── Admin: User Lookup ─────────────────────────────────────────────────────

    async def handle_admin_user_lookup(self, request: web.Request):
        _require_admin(request)
        user_id = int(request.match_info["user_id"])
        import database
        import aiosqlite

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM Users WHERE user_id = ?", (user_id,)
            ) as cursor:
                user_row = await cursor.fetchone()

            async with db.execute(
                """SELECT r.stars, r.comment, r.proof_url, r.author_id, r.timestamp
                   FROM Reviews r WHERE r.target_id = ?
                   ORDER BY r.timestamp DESC LIMIT 20""",
                (user_id,)
            ) as cursor:
                reviews = await cursor.fetchall()

            async with db.execute(
                """SELECT old_name, new_name, timestamp FROM NameHistory
                   WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20""",
                (user_id,)
            ) as cursor:
                name_history = await cursor.fetchall()

        # Try to get Discord user info
        user_info = {"id": str(user_id), "name": str(user_id), "avatar": None}
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if user:
                user_info = {
                    "id": str(user.id),
                    "name": user.name,
                    "display_name": user.display_name,
                    "avatar": str(user.display_avatar.url) if user.display_avatar else None,
                }
        except Exception:
            pass

        avg = 0
        if user_row and user_row["total_reviews"] > 0:
            avg = round(user_row["total_stars"] / user_row["total_reviews"], 1)

        raw_proof_urls = [r["proof_url"] for r in reviews]
        url_map = await self._refresh_discord_urls(raw_proof_urls)

        def _clean_proof(raw_url):
            if not raw_url:
                return None
            refreshed = url_map.get(raw_url, raw_url) if raw_url.startswith("https://cdn.discordapp.com/") else raw_url
            return refreshed if refreshed.startswith("http") else None

        return web.json_response({
            "user": user_info,
            "stats": {
                "total_reviews": user_row["total_reviews"] if user_row else 0,
                "avg_rating": avg,
                "is_blacklisted": bool(user_row["is_blacklisted"]) if user_row else False,
                "post_limit_hours": user_row["post_limit_hours"] if user_row else None,
            },
            "reviews": [
                {
                    "stars": r["stars"],
                    "comment": r["comment"],
                    "proof_url": _clean_proof(r["proof_url"]),
                    "author_id": str(r["author_id"]),
                    "timestamp": r["timestamp"],
                }
                for r in reviews
            ],
            "name_history": [
                {
                    "old_name": r["old_name"],
                    "new_name": r["new_name"],
                    "timestamp": r["timestamp"],
                }
                for r in name_history
            ],
        }, headers=_cors(request))

    # ── Admin: Leave Guild ─────────────────────────────────────────────────────

    async def handle_admin_leave_guild(self, request: web.Request):
        payload = _require_admin(request)
        guild_id = int(request.match_info["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if not guild:
            raise web.HTTPNotFound(reason="Guild not found")
        import database
        await database.log_admin_action(payload["user_id"], "leave_guild", guild_id=guild_id, details=guild.name)
        await guild.leave()
        return web.json_response({"ok": True}, headers=_cors(request))

    # ── Admin: Audit Log ───────────────────────────────────────────────────────

    async def handle_admin_audit_log(self, request: web.Request):
        _require_admin(request)
        import database
        import aiosqlite

        limit = min(int(request.rel_url.query.get("limit", 100)), 500)
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT admin_id, action, guild_id, target_id, details, timestamp FROM AuditLog ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()

        entries = []
        for r in rows:
            admin = self.bot.get_user(r["admin_id"])
            entries.append({
                "admin_id": str(r["admin_id"]),
                "admin_name": admin.name if admin else str(r["admin_id"]),
                "action": r["action"],
                "guild_id": str(r["guild_id"]) if r["guild_id"] else None,
                "target_id": str(r["target_id"]) if r["target_id"] else None,
                "details": r["details"],
                "timestamp": r["timestamp"],
            })

        return web.json_response({"entries": entries}, headers=_cors(request))


async def setup(bot):
    await bot.add_cog(Api(bot))
