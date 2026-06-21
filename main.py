import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("DISCORD_TOKEN")
if not token:
    print("ERROR: DISCORD_TOKEN is not set. Add it to .env before starting the bot.")
    sys.exit(1)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
_commands_synced = False


async def setup_hook():
    await bot.load_extension("queue_cog")
    await bot.load_extension("backup_cog")


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    global _commands_synced

    print(f"Logged in as {bot.user.name}")

    if _commands_synced:
        return

    _commands_synced = True
    guild_id = os.getenv("GUILD_ID")

    if guild_id:
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} guild slash command(s) to guild {guild_id}")

        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared global slash commands")
    else:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global slash command(s)")


bot.run(token, log_handler=handler, log_level=logging.DEBUG)
