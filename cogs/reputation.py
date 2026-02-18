import discord
from discord import app_commands
from discord.ext import commands
import database
import aiosqlite
from datetime import datetime, timedelta

class Reputation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- HELPER FUNCTIONS ---
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

    async def check_staff_perms(self, interaction: discord.Interaction) -> bool:
        """Checks if user is Admin OR has a configured Audit role."""
        if interaction.user.guild_permissions.administrator:
            return True
            
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT role_id FROM GuildRoles WHERE guild_id = ? AND role_type = 'audit'", (interaction.guild_id,)) as cursor:
                valid_roles = [row['role_id'] for row in await cursor.fetchall()]
        
        if valid_roles:
            user_roles = [r.id for r in interaction.user.roles]
            return any(rid in user_roles for rid in valid_roles)
        return False

    # --- CONFIGURATION ---
    @app_commands.command(name="setup", description="Configure server rules and roles")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        name_tracking="Enable/Disable identity tracking alerts",
        proof="Choose if proof is Required, Optional, or Off.",
        verified_role_1="Main role for trusted members",
        verified_role_2="Additional trusted role",
        verified_role_3="Additional trusted role",
        audit_role_1="Main role for staff/audit access",
        audit_role_2="Additional staff role",
        monitor_channel="Add a channel to track immediately",
    )
    @app_commands.choices(
        proof=[
            app_commands.Choice(name="Required (Must attach image)", value="required"),
            app_commands.Choice(name="Optional (Can attach image)", value="optional"),
            app_commands.Choice(name="Off (No images allowed)", value="off"),
        ]
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        name_tracking: bool = True,
        proof: app_commands.Choice[str] = None,
        verified_role_1: discord.Role = None,
        verified_role_2: discord.Role = None,
        verified_role_3: discord.Role = None,
        audit_role_1: discord.Role = None,
        audit_role_2: discord.Role = None,
        monitor_channel: discord.ForumChannel = None,
    ):
        proof_setting = proof.value if proof else "required"

        v_roles = [r for r in [verified_role_1, verified_role_2, verified_role_3] if r is not None]
        a_roles = [r for r in [audit_role_1, audit_role_2] if r is not None]

        await interaction.response.defer(ephemeral=True)
        try:
            async with database.get_db() as db:
                await db.execute(
                    """
                    INSERT INTO Settings (guild_id, track_identity, proof_req) 
                    VALUES (?, ?, ?) 
                    ON CONFLICT(guild_id) DO UPDATE SET 
                        track_identity = excluded.track_identity,
                        proof_req = excluded.proof_req
                """,
                    (interaction.guild_id, name_tracking, proof_setting),
                )

                await db.execute("DELETE FROM GuildRoles WHERE guild_id = ?", (interaction.guild_id,))

                for role in v_roles:
                    await db.execute(
                        "INSERT INTO GuildRoles (guild_id, role_id, role_type) VALUES (?, ?, ?)",
                        (interaction.guild_id, role.id, "verified"),
                    )

                for role in a_roles:
                    await db.execute(
                        "INSERT INTO GuildRoles (guild_id, role_id, role_type) VALUES (?, ?, ?)",
                        (interaction.guild_id, role.id, "audit"),
                    )

                if monitor_channel:
                    await db.execute(
                        "INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id) VALUES (?, ?)",
                        (interaction.guild_id, monitor_channel.id),
                    )

                await db.commit()

            desc = f"**Name Tracking:** {'✅ On' if name_tracking else '❌ Off'}\n**Proof Requirement:** {proof_setting.capitalize()}\n\n"
            desc += "**✅ Verified Roles:**\n" + (", ".join([r.mention for r in v_roles]) if v_roles else "*None set*")
            desc += "\n\n**🛡️ Audit Roles:**\n" + (", ".join([r.mention for r in a_roles]) if a_roles else "*None set*")

            if monitor_channel:
                desc += f"\n\n**Added Channel:** {monitor_channel.mention}"

            embed = self.create_embed("⚙️ Configuration Saved", desc, discord.Color.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # --- TRACKING COMMANDS ---
    @app_commands.command(name="track", description="Start monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def track(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        async with database.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id) VALUES (?, ?)", (interaction.guild_id, forum.id))
            await db.commit()
        await interaction.response.send_message(f"✅ Now monitoring: {forum.mention}", ephemeral=True)

    @app_commands.command(name="untrack", description="Stop monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def untrack(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        async with database.get_db() as db:
            await db.execute("DELETE FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?", (interaction.guild_id, forum.id))
            await db.commit()
        await interaction.response.send_message(f"🗑️ Stopped monitoring: {forum.mention}", ephemeral=True)

    # --- REVIEW MODERATION ---
    review_group = app_commands.Group(name="review", description="Manage reviews and review permissions")

    @review_group.command(name="remove", description="Delete a specific review by ID (Staff Only)")
    async def review_remove(self, interaction: discord.Interaction, review_id: int):
        if not await self.check_staff_perms(interaction):
            return await interaction.response.send_message("⛔ Access Denied: Staff only.", ephemeral=True)

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT target_id, stars FROM Reviews WHERE review_id = ?", (review_id,)) as cursor:
                review = await cursor.fetchone()
            
            if not review:
                return await interaction.response.send_message(f"❌ Review ID `#{review_id}` not found.", ephemeral=True)

            target_id = review['target_id']
            stars_to_remove = review['stars']

            await db.execute("DELETE FROM Reviews WHERE review_id = ?", (review_id,))
            
            await db.execute("""
                UPDATE Users 
                SET total_stars = MAX(0, total_stars - ?), 
                    total_reviews = MAX(0, total_reviews - 1) 
                WHERE user_id = ?
            """, (stars_to_remove, target_id))
            
            await db.commit()

        await interaction.response.send_message(f"🗑️ **Deleted Review #{review_id}** and updated reputation stats.", ephemeral=True)

    @review_group.command(name="block", description="Block a user from leaving reviews (Staff Only)")
    async def review_block(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.check_staff_perms(interaction):
            return await interaction.response.send_message("⛔ Access Denied: Staff only.", ephemeral=True)

        async with database.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
            await db.execute("UPDATE Users SET review_banned = 1 WHERE user_id = ?", (user.id,))
            await db.commit()
        
        await interaction.response.send_message(f"🚫 **Blocked:** {user.mention} can no longer use `/vouch`.", ephemeral=True)

    @review_group.command(name="unblock", description="Allow a user to leave reviews again (Staff Only)")
    async def review_unblock(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.check_staff_perms(interaction):
            return await interaction.response.send_message("⛔ Access Denied: Staff only.", ephemeral=True)

        async with database.get_db() as db:
            await db.execute("UPDATE Users SET review_banned = 0 WHERE user_id = ?", (user.id,))
            await db.commit()

        await interaction.response.send_message(f"✅ **Unblocked:** {user.mention} can now use `/vouch`.", ephemeral=True)

    # --- VOUCH COMMAND ---
    @app_commands.command(name="vouch", description="Review a community member")
    @app_commands.describe(proof="Screenshot proof (Requirement based on /setup)")
    async def vouch(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stars: int,
        comment: str,
        proof: discord.Attachment = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if user.id == interaction.user.id:
            return await interaction.followup.send("❌ You cannot review yourself.", ephemeral=True)
        if not (1 <= stars <= 5):
            return await interaction.followup.send("❌ Stars must be between 1 and 5.", ephemeral=True)

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row

            async with db.execute("SELECT review_banned FROM Users WHERE user_id = ?", (interaction.user.id,)) as cursor:
                author_data = await cursor.fetchone()
                if author_data and author_data['review_banned']:
                    return await interaction.followup.send("⛔ **You are blocked from leaving reviews.**", ephemeral=True)

            async with db.execute("SELECT proof_req FROM Settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                setting = await cursor.fetchone()
                proof_req = setting["proof_req"] if setting else "required"

            async with db.execute("SELECT role_id FROM GuildRoles WHERE guild_id = ? AND role_type = 'verified'", (interaction.guild_id,)) as cursor:
                valid_roles = [row["role_id"] for row in await cursor.fetchall()]

            if valid_roles:
                user_role_ids = [r.id for r in interaction.user.roles]
                if not any(rid in user_role_ids for rid in valid_roles):
                    display_roles = valid_roles[:3]
                    allowed_mentions = " ".join([f"<@&{rid}>" for rid in display_roles])
                    return await interaction.followup.send(
                        f"⛔ You need one of these roles to review: {allowed_mentions}",
                        ephemeral=True,
                    )

            if proof_req == "required" and not proof:
                return await interaction.followup.send(
                    "📸 **Proof Required:** This server requires a screenshot attachment for all reviews.\n\nPlease run the command again and attach an image in the `proof` field.",
                    ephemeral=True,
                )
            if proof_req == "off" and proof:
                return await interaction.followup.send(
                    "❌ **Proof Disabled:** This server has disabled screenshot attachments.",
                    ephemeral=True
                )
            if proof and not proof.content_type.startswith("image/"):
                return await interaction.followup.send("❌ Proof must be an image file.", ephemeral=True)

            async with db.execute(
                "SELECT timestamp FROM Reviews WHERE author_id = ? AND target_id = ? ORDER BY timestamp DESC LIMIT 1",
                (interaction.user.id, user.id),
            ) as cursor:
                last = await cursor.fetchone()
                if last and datetime.now() - datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S") < timedelta(hours=24):
                    return await interaction.followup.send(
                        "⏳ You can only review the same person once every 24 hours.",
                        ephemeral=True,
                    )

            stats = await self.get_user_stats(interaction.user.id)
            weight = 1
            if stats:
                if stats["total_reviews"] >= 50: weight = 2.0
                elif stats["total_reviews"] >= 20: weight = 1.5

            proof_link = proof.url if proof else "No Proof Provided"
            weighted_stars = int(stars * weight)

            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
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
        
        await interaction.followup.send(f"{user.mention} received a new review!", embed=embed)

    # --- REPUTATION CARD ---
    @app_commands.command(name="rep", description="View a member's reputation card.")
    async def rep(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT total_stars, total_reviews FROM Users WHERE user_id = ?", (user.id,)) as cursor:
                data = await cursor.fetchone()

            if not data or data["total_reviews"] == 0:
                return await interaction.response.send_message(f"🤷 {user.display_name} has no reputation history yet.", ephemeral=True)

            avg = round(data["total_stars"] / data["total_reviews"], 1)
            async with db.execute("SELECT stars, comment, author_id FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 3", (user.id,)) as cursor:
                recent = await cursor.fetchall()

        embed = self.create_embed(
            title=f"🛡️ Member Profile: {user.display_name}",
            description="",
            color=discord.Color.gold(),
            user=user,
        )
        embed.add_field(name="🌟 Reputation", value=f"**{avg}/5.0**\n{self.get_stars_display(avg)}", inline=True)
        embed.add_field(name="🤝 Interactions", value=f"**{data['total_reviews']}**\nRecorded", inline=True)

        feed = ""
        for r in recent:
            auth = interaction.guild.get_member(r["author_id"])
            name = auth.display_name if auth else "Unknown"
            feed += f'**{r["stars"]}⭐** *"{r["comment"]}"* — {name}\n'

        embed.add_field(name="💬 Recent Feedback", value=feed or "No comments available.", inline=False)
        await interaction.response.send_message(embed=embed)

    # --- AUDIT LOG ---
    @app_commands.command(name="audit", description="🛡️ View proof logs (Staff Only)")
    async def audit(self, interaction: discord.Interaction, user: discord.Member):
        if not await self.check_staff_perms(interaction):
            return await interaction.response.send_message("⛔ Access Denied.", ephemeral=True)

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT review_id, stars, comment, proof_url, author_id, timestamp FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 10",
                (user.id,),
            ) as cursor:
                reviews = await cursor.fetchall()

        if not reviews:
            return await interaction.response.send_message(f"No records for {user.display_name}.", ephemeral=True)

        embed = self.create_embed(title=f"📜 Audit Log: {user.display_name}", description="Recent 10 Reviews", color=discord.Color.red(), user=user)
        for r in reviews:
            auth = interaction.guild.get_member(r["author_id"])
            name = auth.mention if auth else f"ID: {r['author_id']}"
            proof_display = f"[🔗 **Inspect Proof**]({r['proof_url']})" if r["proof_url"].startswith("http") else r["proof_url"]

            embed.add_field(
                name=f"ID: #{r['review_id']} • {r['timestamp']} ({r['stars']}⭐)",
                value=f"**From:** {name}\n**Note:** {r['comment']}\n{proof_display}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- LEADERBOARD ---
    @app_commands.command(name="leaderboard", description="🏆 View the most reputable members")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, total_reviews, total_stars FROM Users WHERE total_reviews > 0 ORDER BY total_reviews DESC LIMIT 10") as cursor:
                top_traders = await cursor.fetchall()

        if not top_traders:
            return await interaction.followup.send("The ledger is empty.")

        embed = self.create_embed(title="🏆 Hall of Fame", description="Top 10 Most Reviewed Members", color=discord.Color.gold())
        for i, row in enumerate(top_traders, 1):
            user = interaction.guild.get_member(row["user_id"])
            name = user.display_name if user else "Unknown User"
            avg = round(row["total_stars"] / row["total_reviews"], 1)
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
            embed.add_field(name=f"{medal} {name}", value=f"**{row['total_reviews']}** Reviews • **{avg}** ⭐", inline=False)
        await interaction.followup.send(embed=embed)

    # --- BLACKLIST COMMANDS ---
    blacklist_group = app_commands.Group(name="blacklist", description="Manage banned users")

    @blacklist_group.command(name="add", description="Ban a user from posting in tracked channels")
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        async with database.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
            await db.execute("UPDATE Users SET is_blacklisted = 1 WHERE user_id = ?", (user.id,))
            await db.commit()
        await interaction.response.send_message(f"⛔ **Blacklisted** {user.mention}.\nReason: {reason}", ephemeral=True)

    @blacklist_group.command(name="remove", description="Unban a user")
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            await db.execute("UPDATE Users SET is_blacklisted = 0 WHERE user_id = ?", (user.id,))
            await db.commit()
        await interaction.response.send_message(f"✅ Removed {user.mention} from the blacklist.", ephemeral=True)

    # --- RATE LIMIT COMMANDS ---
    limit_group = app_commands.Group(name="limit", description="Manage posting cooldowns")

    @limit_group.command(name="set", description="Limit a user to 1 post every X hours")
    @app_commands.checks.has_permissions(administrator=True)
    async def limit_set(self, interaction: discord.Interaction, user: discord.Member, hours: int):
        if hours < 1:
            return await interaction.response.send_message("❌ Hours must be at least 1.", ephemeral=True)

        async with database.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
            await db.execute("UPDATE Users SET post_limit_hours = ? WHERE user_id = ?", (hours, user.id))
            await db.commit()
        await interaction.response.send_message(f"⏱️ **Limit Set:** {user.mention} can now only post once every **{hours} hours**.", ephemeral=True)

    @limit_group.command(name="remove", description="Remove posting limit for a user")
    @app_commands.checks.has_permissions(administrator=True)
    async def limit_remove(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            await db.execute("UPDATE Users SET post_limit_hours = NULL WHERE user_id = ?", (user.id,))
            await db.commit()
        await interaction.response.send_message(f"✅ **Limit Removed:** {user.mention} can post freely.", ephemeral=True)

    @setup.error
    async def setup_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("⛔ Administrators only.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Reputation(bot))
