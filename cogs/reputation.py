import discord
from discord import app_commands
from discord.ext import commands
import database
import aiosqlite
from datetime import datetime, timedelta


class Reputation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def create_embed(self, title, description, color=0x2B2D31, user=None):
        embed = discord.Embed(title=title, description=description, color=color)
        if user:
            embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(
            text="The Jantleman • Community Reputation",
            icon_url=self.bot.user.display_avatar.url,
        )
        embed.timestamp = datetime.now()
        return embed

    def get_stars_display(self, rating: float) -> str:
        full = int(rating)
        return ("⭐" * full) + ("☆" * (5 - full))

    async def get_user_stats(self, user_id: int):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT total_reviews FROM Users WHERE user_id = ?", (user_id,)
            ) as cursor:
                return await cursor.fetchone()

    @app_commands.command(name="setup", description="Configure bot settings")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        verified_role="Role required to give reviews",
        audit_role="Role allowing access to logs",
        name_tracking="Enable/Disable identity tracking alerts",
        proof="Requirement for screenshots on reviews",
        monitor_channel="(Optional) Add a channel to track immediately",
    )
    @app_commands.choices(
        proof=[
            app_commands.Choice(name="Required", value="required"),
            app_commands.Choice(name="Optional", value="optional"),
            app_commands.Choice(name="Off", value="off"),
        ]
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        verified_role: discord.Role,
        audit_role: discord.Role,
        name_tracking: bool = True,
        proof: app_commands.Choice[str] = None,
        monitor_channel: discord.ForumChannel = None,
    ):
        proof_setting = proof.value if proof else "required"

        await interaction.response.defer(ephemeral=True)
        try:
            async with database.get_db() as db:
                await db.execute(
                    """
                    INSERT INTO Settings (guild_id, verified_role_id, audit_role_id, track_identity, proof_req) 
                    VALUES (?, ?, ?, ?, ?) 
                    ON CONFLICT(guild_id) DO UPDATE SET 
                        verified_role_id = excluded.verified_role_id,
                        audit_role_id = excluded.audit_role_id,
                        track_identity = excluded.track_identity,
                        proof_req = excluded.proof_req
                """,
                    (
                        interaction.guild_id,
                        verified_role.id,
                        audit_role.id,
                        name_tracking,
                        proof_setting,
                    ),
                )

                if monitor_channel:
                    await db.execute(
                        "INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id) VALUES (?, ?)",
                        (interaction.guild_id, monitor_channel.id),
                    )

                await db.commit()

            desc = (
                f"**Verified Role:** {verified_role.mention}\n"
                f"**Audit Role:** {audit_role.mention}\n"
                f"**Name Tracking:** {'✅ On' if name_tracking else '❌ Off'}\n"
                f"**Proof Logic:** {proof_setting.capitalize()}"
            )
            if monitor_channel:
                desc += f"\n**Added Channel:** {monitor_channel.mention}"

            embed = self.create_embed(
                "⚙️ Configuration Saved", desc, discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="track", description="Start monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def track(
        self, interaction: discord.Interaction, forum: discord.ForumChannel
    ):
        async with database.get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id) VALUES (?, ?)",
                (interaction.guild_id, forum.id),
            )
            await db.commit()
        await interaction.response.send_message(
            f"✅ Now monitoring: {forum.mention}", ephemeral=True
        )

    @app_commands.command(name="untrack", description="Stop monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def untrack(
        self, interaction: discord.Interaction, forum: discord.ForumChannel
    ):
        async with database.get_db() as db:
            await db.execute(
                "DELETE FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild_id, forum.id),
            )
            await db.commit()
        await interaction.response.send_message(
            f"🗑️ Stopped monitoring: {forum.mention}", ephemeral=True
        )

    @app_commands.command(name="vouch", description="Review a community member")
    @app_commands.describe(
        proof="(Optional) Screenshot proof. Requirement depends on server settings."
    )
    async def vouch(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stars: int,
        comment: str,
        proof: discord.Attachment = None,
    ):
        if user.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You cannot review yourself.", ephemeral=True
            )
        if not (1 <= stars <= 5):
            return await interaction.response.send_message(
                "❌ Stars must be between 1 and 5.", ephemeral=True
            )

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT verified_role_id, proof_req FROM Settings WHERE guild_id = ?",
                (interaction.guild_id,),
            ) as cursor:
                setting = await cursor.fetchone()

            if not setting:
                return await interaction.response.send_message(
                    "❌ Bot not set up. Ask an admin to run /setup.", ephemeral=True
                )

            if setting["verified_role_id"]:
                role = interaction.guild.get_role(setting["verified_role_id"])
                if role not in interaction.user.roles:
                    return await interaction.response.send_message(
                        f"⛔ You need the {role.mention} role to leave reviews.",
                        ephemeral=True,
                    )

            req = setting["proof_req"]
            if req == "required" and not proof:
                return await interaction.response.send_message(
                    "📸 **Proof Required:** This server requires a screenshot attachment for all reviews.",
                    ephemeral=True,
                )
            if (
                req == "required"
                and proof
                and not proof.content_type.startswith("image/")
            ):
                return await interaction.response.send_message(
                    "❌ Proof must be an image file.", ephemeral=True
                )

            async with db.execute(
                "SELECT timestamp FROM Reviews WHERE author_id = ? AND target_id = ? ORDER BY timestamp DESC LIMIT 1",
                (interaction.user.id, user.id),
            ) as cursor:
                last = await cursor.fetchone()
                if last and datetime.now() - datetime.strptime(
                    last["timestamp"], "%Y-%m-%d %H:%M:%S"
                ) < timedelta(hours=24):
                    return await interaction.response.send_message(
                        "⏳ You can only review the same person once every 24 hours.",
                        ephemeral=True,
                    )

            stats = await self.get_user_stats(interaction.user.id)
            weight = 1
            if stats:
                if stats["total_reviews"] >= 50:
                    weight = 2.0
                elif stats["total_reviews"] >= 20:
                    weight = 1.5

            proof_link = proof.url if proof else "No Proof Provided"
            weighted_stars = int(stars * weight)

            await db.execute(
                "INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,)
            )
            await db.execute(
                "INSERT INTO Reviews (target_id, author_id, stars, comment, proof_url) VALUES (?, ?, ?, ?, ?)",
                (user.id, interaction.user.id, stars, comment, proof_link),
            )
            await db.execute(
                "UPDATE Users SET total_stars = total_stars + ?, total_reviews = total_reviews + 1 WHERE user_id = ?",
                (weighted_stars, user.id),
            )
            await db.commit()

        embed = self.create_embed(
            title="✅ Review Recorded",
            description=f'**Target:** {user.mention}\n**Rating:** {stars}/5 ⭐\n**Comment:** *"{comment}"*\n**Weight:** {weight}x',
            color=discord.Color.gold(),
            user=user,
        )
        if proof:
            embed.set_image(url=proof.url)
        await interaction.response.send_message(
            f"{user.mention} received a new review!", embed=embed
        )

    @app_commands.command(name="rep", description="View a member's reputation card.")
    async def rep(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT total_stars, total_reviews FROM Users WHERE user_id = ?",
                (user.id,),
            ) as cursor:
                data = await cursor.fetchone()

            if not data or data["total_reviews"] == 0:
                return await interaction.response.send_message(
                    f"🤷 {user.display_name} has no reputation history yet.",
                    ephemeral=True,
                )

            avg = round(data["total_stars"] / data["total_reviews"], 1)
            async with db.execute(
                "SELECT stars, comment, author_id FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 3",
                (user.id,),
            ) as cursor:
                recent = await cursor.fetchall()

        embed = self.create_embed(
            title=f"🛡️ Member Profile: {user.display_name}",
            description="",
            color=discord.Color.gold(),
            user=user,
        )
        embed.add_field(
            name="🌟 Reputation",
            value=f"**{avg}/5.0**\n{self.get_stars_display(avg)}",
            inline=True,
        )
        embed.add_field(
            name="🤝 Interactions",
            value=f"**{data['total_reviews']}**\nRecorded",
            inline=True,
        )

        feed = ""
        for r in recent:
            auth = interaction.guild.get_member(r["author_id"])
            name = auth.display_name if auth else "Unknown"
            feed += f'**{r["stars"]}⭐** *"{r["comment"]}"* — {name}\n'

        embed.add_field(
            name="💬 Recent Feedback",
            value=feed or "No comments available.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="audit", description="🛡️ View proof logs (Staff Only)")
    async def audit(self, interaction: discord.Interaction, user: discord.Member):
        is_admin = interaction.user.guild_permissions.administrator
        has_role = False
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT audit_role_id FROM Settings WHERE guild_id = ?",
                (interaction.guild_id,),
            ) as cursor:
                setting = await cursor.fetchone()
                if (
                    setting
                    and setting["audit_role_id"]
                    and interaction.guild.get_role(setting["audit_role_id"])
                    in interaction.user.roles
                ):
                    has_role = True

        if not is_admin and not has_role:
            return await interaction.response.send_message(
                "⛔ Access Denied.", ephemeral=True
            )

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT stars, comment, proof_url, author_id, timestamp FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 10",
                (user.id,),
            ) as cursor:
                reviews = await cursor.fetchall()

        if not reviews:
            return await interaction.response.send_message(
                f"No records for {user.display_name}.", ephemeral=True
            )

        embed = self.create_embed(
            title=f"📜 Audit Log: {user.display_name}",
            description="Recent 10 Reviews",
            color=discord.Color.red(),
            user=user,
        )
        for r in reviews:
            auth = interaction.guild.get_member(r["author_id"])
            name = auth.mention if auth else f"ID: {r['author_id']}"

            proof_display = (
                f"[🔗 **Inspect Proof**]({r['proof_url']})"
                if r["proof_url"].startswith("http")
                else r["proof_url"]
            )

            embed.add_field(
                name=f"{r['timestamp']} ({r['stars']}⭐)",
                value=f"**From:** {name}\n**Note:** {r['comment']}\n{proof_display}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="leaderboard", description="🏆 View the most reputable members"
    )
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, total_reviews, total_stars FROM Users WHERE total_reviews > 0 ORDER BY total_reviews DESC LIMIT 10"
            ) as cursor:
                top_traders = await cursor.fetchall()

        if not top_traders:
            return await interaction.followup.send("The ledger is empty.")

        embed = self.create_embed(
            title="🏆 Hall of Fame",
            description="Top 10 Most Reviewed Members",
            color=discord.Color.gold(),
        )
        for i, row in enumerate(top_traders, 1):
            user = interaction.guild.get_member(row["user_id"])
            name = user.display_name if user else "Unknown User"
            avg = round(row["total_stars"] / row["total_reviews"], 1)
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
            embed.add_field(
                name=f"{medal} {name}",
                value=f"**{row['total_reviews']}** Reviews • **{avg}** ⭐",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @setup.error
    async def setup_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "⛔ Administrators only.", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(Reputation(bot))
