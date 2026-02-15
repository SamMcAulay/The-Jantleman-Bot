import discord
from discord import app_commands
from discord.ext import commands
import database
import aiosqlite


class Watchlist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Helper for consistency
    def create_embed(self, title, description, color=discord.Color.blue()):
        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_footer(
            text="The Jantleman", icon_url=self.bot.user.display_avatar.url
        )
        return embed

    @app_commands.command(name="watch", description="Manage your market alerts")
    @app_commands.describe(
        action="Add, Remove, or List alerts",
        keyword="The item to watch for (e.g., 'Gold Bar')",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add Alert", value="add"),
            app_commands.Choice(name="Remove Alert", value="remove"),
            app_commands.Choice(name="List My Alerts", value="list"),
        ]
    )
    async def watch(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        keyword: str = None,
    ):
        user_id = interaction.user.id

        async with database.get_db() as db:
            # --- LIST ---
            if action.value == "list":
                async with db.execute(
                    "SELECT keyword FROM Watchlist WHERE user_id = ?", (user_id,)
                ) as cursor:
                    rows = await cursor.fetchall()

                if not rows:
                    return await interaction.response.send_message(
                        "🔕 You have no active alerts.", ephemeral=True
                    )

                keywords = [f"• **{row[0]}**" for row in rows]
                embed = self.create_embed("Your Watchlist", "\n".join(keywords))
                return await interaction.response.send_message(
                    embed=embed, ephemeral=True
                )

            # --- ADD ---
            if action.value == "add":
                if not keyword:
                    return await interaction.response.send_message(
                        "❌ You must provide a keyword.", ephemeral=True
                    )

                keyword = keyword.lower().strip()
                # Check limit (Max 10 per user to prevent spam)
                async with db.execute(
                    "SELECT COUNT(*) FROM Watchlist WHERE user_id = ?", (user_id,)
                ) as cursor:
                    count = await cursor.fetchone()
                    if count[0] >= 10:
                        return await interaction.response.send_message(
                            "❌ Limit reached (Max 10 alerts).", ephemeral=True
                        )

                await db.execute(
                    "INSERT INTO Watchlist (user_id, keyword) VALUES (?, ?)",
                    (user_id, keyword),
                )
                await db.commit()
                return await interaction.response.send_message(
                    f"✅ Alert added for **'{keyword}'**.", ephemeral=True
                )

            # --- REMOVE ---
            if action.value == "remove":
                if not keyword:
                    return await interaction.response.send_message(
                        "❌ You must provide a keyword to remove.", ephemeral=True
                    )

                keyword = keyword.lower().strip()
                await db.execute(
                    "DELETE FROM Watchlist WHERE user_id = ? AND keyword = ?",
                    (user_id, keyword),
                )
                await db.commit()
                return await interaction.response.send_message(
                    f"🗑️ Removed alert for **'{keyword}'**.", ephemeral=True
                )


async def setup(bot):
    await bot.add_cog(Watchlist(bot))
