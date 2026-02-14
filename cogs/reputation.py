import discord
from discord import app_commands
from discord.ext import commands
import database
import logging
import aiosqlite
from datetime import datetime, timedelta

class Reputation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- HELPER: Unified Design System ---
    def create_embed(self, title, description, color=0x2b2d31, user=None):
        embed = discord.Embed(title=title, description=description, color=color)
        if user:
            embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="The Jantleman • Trusted Trading", icon_url=self.bot.user.display_avatar.url)
        embed.timestamp = datetime.now()
        return embed

    def get_stars_display(self, rating: float) -> str:
        full = int(rating)
        return ("⭐" * full) + ("☆" * (5 - full))

    async def get_user_stats(self, user_id: int):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT total_reviews FROM Users WHERE user_id = ?", (user_id,)) as cursor:
                return await cursor.fetchone()

    # ... (Imports and helper functions remain the same) ...

    # --- 1. SETUP (ROLES ONLY) ---
    @app_commands.command(name="setup", description="Configure bot roles (Admins Only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction, verified_role: discord.Role, audit_role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        try:
            async with database.get_db() as db:
                await db.execute('''
                    INSERT INTO Settings (guild_id, verified_role_id, audit_role_id) 
                    VALUES (?, ?, ?) 
                    ON CONFLICT(guild_id) DO UPDATE SET 
                        verified_role_id = excluded.verified_role_id,
                        audit_role_id = excluded.audit_role_id
                ''', (interaction.guild_id, verified_role.id, audit_role.id))
                await db.commit()
            
            embed = self.create_embed("⚙️ Roles Configured", f"**Verified:** {verified_role.mention}\n**Audit:** {audit_role.mention}", discord.Color.green())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # --- 2. TRACK COMMAND (ADD CHANNEL) ---
    @app_commands.command(name="track", description="Start monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def track(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        async with database.get_db() as db:
            # Insert, ignoring if it already exists
            await db.execute("INSERT OR IGNORE INTO MonitoredChannels (guild_id, channel_id) VALUES (?, ?)", (interaction.guild_id, forum.id))
            await db.commit()
        
        await interaction.response.send_message(f"✅ Now monitoring: {forum.mention}", ephemeral=True)

    # --- 3. UNTRACK COMMAND (REMOVE CHANNEL) ---
    @app_commands.command(name="untrack", description="Stop monitoring a forum channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def untrack(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        async with database.get_db() as db:
            await db.execute("DELETE FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?", (interaction.guild_id, forum.id))
            await db.commit()
        
        await interaction.response.send_message(f"sz Stopped monitoring: {forum.mention}", ephemeral=True)

    # ... (Keep vouch, rep, audit, and leaderboard commands exactly the same) ...

    # --- VOUCH COMMAND ---
    @app_commands.command(name="vouch", description="Rate a trade (Requires Proof + Verified Role)")
    @app_commands.describe(proof="Upload a screenshot of the trade/chat")
    async def vouch(self, interaction: discord.Interaction, user: discord.Member, stars: int, comment: str, proof: discord.Attachment):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot vouch for yourself.", ephemeral=True)
        if not (1 <= stars <= 5):
            return await interaction.response.send_message("❌ Stars must be between 1 and 5.", ephemeral=True)
        if not proof.content_type or not proof.content_type.startswith("image/"):
             return await interaction.response.send_message("❌ Proof must be an image.", ephemeral=True)

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            # Role Check
            async with db.execute("SELECT verified_role_id FROM Settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                setting = await cursor.fetchone()
                if setting and setting['verified_role_id']:
                    role = interaction.guild.get_role(setting['verified_role_id'])
                    if role not in interaction.user.roles:
                        return await interaction.response.send_message(f"⛔ You need the {role.mention} role.", ephemeral=True)

            # Anti-Spam
            async with db.execute("SELECT timestamp FROM Reviews WHERE author_id = ? AND target_id = ? ORDER BY timestamp DESC LIMIT 1", (interaction.user.id, user.id)) as cursor:
                last = await cursor.fetchone()
                if last and datetime.now() - datetime.strptime(last['timestamp'], "%Y-%m-%d %H:%M:%S") < timedelta(hours=24):
                    return await interaction.response.send_message("⏳ Only one vouch per user per 24 hours.", ephemeral=True)

            # Weight Logic
            stats = await self.get_user_stats(interaction.user.id)
            weight = 1
            if stats:
                if stats['total_reviews'] >= 50: weight = 2.0
                elif stats['total_reviews'] >= 20: weight = 1.5
            
            # Save
            weighted_stars = int(stars * weight)
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
            await db.execute("INSERT INTO Reviews (target_id, author_id, stars, comment, proof_url) VALUES (?, ?, ?, ?, ?)", 
                             (user.id, interaction.user.id, stars, comment, proof.url))
            await db.execute("UPDATE Users SET total_stars = total_stars + ?, total_reviews = total_reviews + 1 WHERE user_id = ?", (weighted_stars, user.id))
            await db.commit()

        # Fancy Success Embed
        embed = self.create_embed(
            title="✅ Vouch Recorded",
            description=f"**Target:** {user.mention}\n**Rating:** {stars}/5 ⭐\n**Comment:** *\"{comment}\"*\n**Weight:** {weight}x",
            color=discord.Color.gold(),
            user=user
        )
        embed.set_image(url=proof.url)
        await interaction.response.send_message(f"{user.mention} received a new review!", embed=embed)

    # --- REP COMMAND ---
    @app_commands.command(name="rep", description="View a trader's reputation.")
    async def rep(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row 
            async with db.execute("SELECT total_stars, total_reviews FROM Users WHERE user_id = ?", (user.id,)) as cursor:
                data = await cursor.fetchone()
            
            if not data or data['total_reviews'] == 0:
                return await interaction.response.send_message(f"🤷 {user.display_name} has no reputation yet.", ephemeral=True)

            avg = round(data['total_stars'] / data['total_reviews'], 1)
            async with db.execute("SELECT stars, comment, author_id FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 3", (user.id,)) as cursor:
                recent = await cursor.fetchall()

        # Fancy Rep Profile
        embed = self.create_embed(title=f"🛡️ Trader Profile: {user.display_name}", description="", color=discord.Color.gold(), user=user)
        embed.add_field(name="🌟 Reputation", value=f"**{avg}/5.0**\n{self.get_stars_display(avg)}", inline=True)
        embed.add_field(name="🤝 Trades", value=f"**{data['total_reviews']}**\nCompleted", inline=True)
        
        feed = ""
        for r in recent:
            auth = interaction.guild.get_member(r['author_id'])
            name = auth.display_name if auth else "Unknown"
            feed += f"**{r['stars']}⭐** *\"{r['comment']}\"* — {name}\n"
        
        embed.add_field(name="💬 Recent Feedback", value=feed or "No comments available.", inline=False)
        await interaction.response.send_message(embed=embed)

    # --- AUDIT COMMAND ---
    @app_commands.command(name="audit", description="🛡️ View proof logs (Knights Only)")
    async def audit(self, interaction: discord.Interaction, user: discord.Member):
        is_admin = interaction.user.guild_permissions.administrator
        has_role = False
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT audit_role_id FROM Settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                setting = await cursor.fetchone()
                if setting and setting['audit_role_id'] and interaction.guild.get_role(setting['audit_role_id']) in interaction.user.roles:
                    has_role = True
        
        if not is_admin and not has_role:
            return await interaction.response.send_message("⛔ Access Denied.", ephemeral=True)

        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT stars, comment, proof_url, author_id, timestamp FROM Reviews WHERE target_id = ? ORDER BY timestamp DESC LIMIT 10", (user.id,)) as cursor:
                reviews = await cursor.fetchall()

        if not reviews:
            return await interaction.response.send_message(f"No records for {user.display_name}.", ephemeral=True)

        embed = self.create_embed(title=f"📜 Audit Log: {user.display_name}", description="Recent 10 Transactions", color=discord.Color.red(), user=user)
        for r in reviews:
            auth = interaction.guild.get_member(r['author_id'])
            name = auth.mention if auth else f"ID: {r['author_id']}"
            embed.add_field(
                name=f"{r['timestamp']} ({r['stars']}⭐)",
                value=f"**From:** {name}\n**Note:** {r['comment']}\n[🔗 **Inspect Proof**]({r['proof_url']})",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- LEADERBOARD COMMAND ---
    @app_commands.command(name="leaderboard", description="🏆 View the top merchants of the Arc")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row
            # Get top 10 traders by volume
            async with db.execute("""
                SELECT user_id, total_reviews, total_stars 
                FROM Users 
                WHERE total_reviews > 0 
                ORDER BY total_reviews DESC 
                LIMIT 10
            """) as cursor:
                top_traders = await cursor.fetchall()

        if not top_traders:
            return await interaction.followup.send("The ledger is empty. No trades recorded yet.")

        embed = self.create_embed(
            title="🏆 The Merchant's Ledger",
            description="The most trusted traders in the sector.",
            color=discord.Color.gold()
        )

        for i, row in enumerate(top_traders, 1):
            user = interaction.guild.get_member(row['user_id'])
            name = user.display_name if user else "Unknown Trader"
            avg = round(row['total_stars'] / row['total_reviews'], 1)
            
            # Add a medal emoji for top 3
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
            
            embed.add_field(
                name=f"{medal} {name}",
                value=f"**{row['total_reviews']}** Trades • **{avg}** ⭐",
                inline=False
            )
            
        await interaction.followup.send(embed=embed)

    @setup.error
    async def setup_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("⛔ Administrators only.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Reputation(bot))
