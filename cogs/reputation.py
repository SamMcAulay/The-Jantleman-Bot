import discord
from discord import app_commands
from discord.ext import commands
import database
import logging
import aiosqlite  

class Reputation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_stars_display(self, rating: float) -> str:
        full = int(rating)
        return ("⭐" * full) + ("☆" * (5 - full))

    @app_commands.command(name="setup", description="Link the trading forum to the bot.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        async with database.get_db() as db:
            await db.execute('''
                INSERT INTO Settings (guild_id, forum_channel_id) 
                VALUES (?, ?) 
                ON CONFLICT(guild_id) DO UPDATE SET forum_channel_id = ?
            ''', (interaction.guild_id, forum.id, forum.id))
            await db.commit()
        
        await interaction.response.send_message(f"✅ Configuration saved! Monitoring: {forum.mention}", ephemeral=True)

    @app_commands.command(name="vouch", description="Rate a successful trade.")
    async def vouch(self, interaction: discord.Interaction, user: discord.Member, stars: int, comment: str):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ Nice try! You cannot vouch for yourself.", ephemeral=True)
        if not (1 <= stars <= 5):
            return await interaction.response.send_message("❌ Stars must be between 1 and 5.", ephemeral=True)

        async with database.get_db() as db:
            
            await db.execute("INSERT OR IGNORE INTO Users (user_id) VALUES (?)", (user.id,))
            await db.execute("""
                INSERT INTO Reviews (target_id, author_id, stars, comment) 
                VALUES (?, ?, ?, ?)
            """, (user.id, interaction.user.id, stars, comment))
            
            await db.execute("""
                UPDATE Users 
                SET total_stars = total_stars + ?, total_reviews = total_reviews + 1 
                WHERE user_id = ?
            """, (stars, user.id))
            await db.commit()

        await interaction.response.send_message(f"✅ Vouch recorded for {user.mention}!", ephemeral=False)

    @app_commands.command(name="rep", description="View a trader's reputation.")
    async def rep(self, interaction: discord.Interaction, user: discord.Member):
        async with database.get_db() as db:
            db.row_factory = aiosqlite.Row  # This caused the error before!
            async with db.execute("SELECT total_stars, total_reviews FROM Users WHERE user_id = ?", (user.id,)) as cursor:
                data = await cursor.fetchone()
            
            if not data or data['total_reviews'] == 0:
                return await interaction.response.send_message(f"🤷 {user.display_name} has no reputation history.", ephemeral=True)

            avg = round(data['total_stars'] / data['total_reviews'], 1)

            async with db.execute("""
                SELECT stars, comment, author_id 
                FROM Reviews 
                WHERE target_id = ? 
                ORDER BY timestamp DESC LIMIT 3
            """, (user.id,)) as cursor:
                recent_reviews = await cursor.fetchall()

        # Build Embed
        embed = discord.Embed(title=f"🛡️ Trader Profile: {user.display_name}", color=0x5865F2)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Trust Rating", value=f"{self.get_stars_display(avg)} **({avg}/5.0)**", inline=True)
        embed.add_field(name="Total Trades", value=f"**{data['total_reviews']}** completed", inline=True)
        
        # Format reviews
        review_text = ""
        for row in recent_reviews:
            reviewer = interaction.guild.get_member(row['author_id'])
            reviewer_name = reviewer.display_name if reviewer else "Unknown User"
            review_text += f"**{row['stars']}⭐** \"{row['comment']}\" — *{reviewer_name}*\n"
        
        embed.add_field(name="Latest Feedback", value=review_text or "No comments provided.", inline=False)
        await interaction.response.send_message(embed=embed)

    # Global Error Handler for this Cog
    @setup.error
    async def setup_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("⛔ You need 'Administrator' permissions to do this.", ephemeral=True)
        else:
            logging.error(f"Setup Command Error: {error}")

async def setup(bot):
    await bot.add_cog(Reputation(bot))
