import datetime
import io
import logging
import os

import discord
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from queue_manager import export_queues_backup_json

logger = logging.getLogger(__name__)


def get_backup_timezone() -> datetime.tzinfo:
    tz_name = os.getenv("BACKUP_TIMEZONE", "UTC")
    if tz_name.upper() == "UTC":
        return datetime.timezone.utc

    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ZoneInfoNotFoundError(
            f"Unknown timezone '{tz_name}'. Install tzdata (`pip install tzdata`) "
            f"or set BACKUP_TIMEZONE to UTC."
        ) from exc


def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel_id = os.getenv("LOG_CHANNEL_ID")
    if channel_id:
        try:
            channel = guild.get_channel(int(channel_id))
        except ValueError:
            channel = None

        if isinstance(channel, discord.TextChannel):
            return channel

    return discord.utils.get(guild.text_channels, name="aaron_bot_log")


async def send_queue_backup(
    guild: discord.Guild,
) -> discord.Message | None:
    channel = get_log_channel(guild)
    if not channel:
        return None

    timezone = get_backup_timezone()
    date_str = datetime.datetime.now(timezone).strftime("%Y-%m-%d")
    payload = export_queues_backup_json(guild)
    filename = f"queue_backup_{date_str}.json"
    file = discord.File(io.BytesIO(payload.encode("utf-8")), filename=filename)

    return await channel.send(file=file)


class BackupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        timezone = get_backup_timezone()

        @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=timezone))
        async def daily_backup():
            await self._send_daily_backups()

        @daily_backup.before_loop
        async def before_daily_backup():
            await self.bot.wait_until_ready()

        self.daily_backup = daily_backup
        self.daily_backup.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self.bot, "_startup_backup_sent", False):
            return

        self.bot._startup_backup_sent = True
        sent = False
        for guild in self.bot.guilds:
            message = await send_queue_backup(guild)
            if message:
                sent = True

        if not sent:
            logger.warning("Startup queue backup skipped: no log channel found")

    def cog_unload(self):
        self.daily_backup.cancel()

    async def _send_daily_backups(self) -> None:
        sent = False
        for guild in self.bot.guilds:
            message = await send_queue_backup(guild)
            if message:
                sent = True
            else:
                logger.warning("Log channel not found in guild %s", guild.id)

        if not sent:
            logger.warning("Daily queue backup skipped: no log channel found")


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))
