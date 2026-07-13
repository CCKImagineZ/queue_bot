import logging
import os
import sys

import discord
from discord import app_commands
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

log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)

# discord.py DEBUG logs every HTTP/WebSocket event and grows quickly on small hosts.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.members = True
# Slash-only bot — no need to receive every message body (saves RAM on free tier).
intents.message_content = False


async def _disabled_prefix(_bot: commands.Bot, _message: discord.Message):
    """This bot uses slash commands only; ignore text like /gcreate or /gstart."""
    return []


bot = commands.Bot(
    command_prefix=_disabled_prefix,
    intents=intents,
    # Free-tier hosts (100 MB): avoid caching every guild member in RAM.
    member_cache_flags=discord.MemberCacheFlags.none(),
    chunk_guilds_at_startup=False,
)
_commands_synced = False
logger = logging.getLogger(__name__)


async def delete_all_global_commands(bot: commands.Bot) -> None:
    """Remove stale global slash commands so clients don't hit dead endpoints."""
    app_id = bot.application_id
    if not app_id:
        return

    global_commands = await bot.http.get_global_commands(app_id)
    for command in global_commands:
        await bot.http.delete_global_command(app_id, command["id"])
        logger.info("Deleted stale global command: /%s", command["name"])


async def sync_guild_commands(bot: commands.Bot, guild_id: int) -> None:
    guild = discord.Object(id=guild_id)

    # Required: guild sync only reads the guild command bucket, not globals.
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    command_names = [command.name for command in synced]
    logger.info(
        "Synced %s guild slash command(s) to guild %s: %s",
        len(synced),
        guild_id,
        ", ".join(command_names) or "(none)",
    )

    if not synced:
        logger.error(
            "No commands synced — slash commands will not appear. "
            "Check that queue_cog loaded correctly."
        )
        return

    await delete_all_global_commands(bot)


async def setup_hook():
    await bot.load_extension("queue_cog")
    await bot.load_extension("backup_cog")


bot.setup_hook = setup_hook


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if interaction.response.is_done():
        send = interaction.followup.send
    else:
        send = interaction.response.send_message

    if isinstance(error, app_commands.MissingPermissions):
        missing = ", ".join(f"`{perm.replace('_', ' ').title()}`" for perm in error.missing_permissions)
        await send(
            f"You need **Administrator** permission to use queue commands.",
            ephemeral=True,
        )
        return

    if isinstance(error, app_commands.CheckFailure):
        await send(
            "You don't have permission to use this command.",
            ephemeral=True,
        )
        return

    logging.getLogger(__name__).exception("Slash command failed")
    await send(
        "Something went wrong running that command. Try again or contact an admin.",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    global _commands_synced

    logger.info("Logged in as %s (guilds: %s)", bot.user, len(bot.guilds))

    if _commands_synced:
        return

    _commands_synced = True
    guild_id = os.getenv("GUILD_ID")

    if guild_id:
        await sync_guild_commands(bot, int(guild_id))
    else:
        synced = await bot.tree.sync()
        logger.info("Synced %s global slash command(s)", len(synced))


@bot.event
async def on_disconnect():
    logger.warning("Disconnected from Discord gateway")


@bot.event
async def on_resumed():
    logger.info("Resumed Discord gateway session")


@bot.event
async def on_error(event: str, *args, **kwargs):
    logger.exception("Unhandled error in event %s", event)


bot.run(token, log_handler=handler, log_level=log_level)
