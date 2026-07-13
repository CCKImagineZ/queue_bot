import io
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from backup_cog import send_queue_backup
from queue_manager import (
    build_queue_embed,
    cache_member_name,
    create_custom_section,
    delete_custom_section,
    delete_section_autocomplete_choices,
    entry_matches_member,
    find_entry_index,
    find_queue_users_by_name,
    get_all_categories,
    import_queues_backup,
    import_queue_from_board_message,
    is_persisted_queue_empty,
    is_queue_board_message,
    load_data,
    member_to_entry,
    move_section,
    parse_queue_embed,
    parse_queue_message,
    queue_embeds_match,
    remove_member_from_all,
    remove_name_match_from_all,
    save_data,
    section_display,
    entry_user_id,
    _build_queue_description,
)

logger = logging.getLogger(__name__)


def _format_userremoval_preview(matches: list[dict], data: dict, query: str) -> str:
    lines = [
        f"Found **{len(matches)}** queue match(es) for `{query}`:",
        "",
    ]
    for match in matches:
        sections = ", ".join(
            f"**{section_display(key, data)}**" for key in match["sections"]
        )
        user_id = match.get("user_id")
        id_note = f" (ID `{user_id}`)" if user_id is not None else ""
        lines.append(f"• **{match['display_name']}**{id_note} — {sections}")

    lines.extend(
        [
            "",
            "Press **Continue** to remove them from the queue, or **Cancel**.",
        ]
    )
    return "\n".join(lines)


class UserRemovalConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "QueueCog",
        channel: discord.TextChannel,
        author_id: int,
        matches: list[dict],
        query: str,
        timeout: float = 120,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.channel = channel
        self.author_id = author_id
        self.matches = matches
        self.query = query
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/queue userremoval` can confirm this.",
                ephemeral=True,
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("userremoval button failed (%s)", getattr(item, "label", item))
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong while handling that button. Try `/queue userremoval` again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong while handling that button. Try `/queue userremoval` again.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    async def _delete_prompt(self, interaction: discord.Interaction) -> None:
        target = interaction.message or self.message
        if target is None:
            return
        try:
            await target.delete()
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # Acknowledge immediately — defer(ephemeral=True) is invalid for buttons
        # and causes Discord's "This interaction failed".
        await interaction.response.edit_message(
            content="Removing matched users…",
            view=None,
        )
        self.message = interaction.message

        data = load_data()
        removed_lines: list[str] = []
        for match in self.matches:
            removed_from = remove_name_match_from_all(data, match)
            if not removed_from:
                continue
            sections = ", ".join(
                f"**{section_display(key, data)}**" for key in removed_from
            )
            removed_lines.append(f"• **{match['display_name']}** from {sections}")

        if not removed_lines:
            await self._delete_prompt(interaction)
            await interaction.followup.send(
                "No matching users were still on the queue.",
                ephemeral=True,
            )
            self.stop()
            return

        save_data(data)
        try:
            await self.cog._refresh_queue_message(self.channel)
        except Exception:
            logger.exception("Failed to refresh queue after userremoval")
            await self._delete_prompt(interaction)
            await interaction.followup.send(
                "Users were removed from saved data, but the board refresh failed. "
                "Try `/queue post`.\n" + "\n".join(removed_lines),
                ephemeral=True,
            )
            self.stop()
            return

        await self._delete_prompt(interaction)
        await interaction.followup.send(
            "Removed from queue:\n" + "\n".join(removed_lines),
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Cancelling…",
            view=None,
        )
        self.message = interaction.message
        await self._delete_prompt(interaction)
        await interaction.followup.send("Removal cancelled.", ephemeral=True)
        self.stop()


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.application_command:
            return

        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        logger.info(
            "Command /%s from %s (%s) in guild %s",
            command_name,
            interaction.user,
            interaction.user.id,
            interaction.guild_id,
        )

    async def section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        data = load_data()
        current_lower = current.casefold()
        choices = []

        for key, header in get_all_categories(data):
            if (
                not current
                or current_lower in header.casefold()
                or current_lower in key.casefold()
            ):
                choices.append(
                    app_commands.Choice(name=header[:100], value=key)
                )

        return choices[:25]

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self.bot, "_queue_startup_done", False):
            return

        self.bot._queue_startup_done = True

        for guild in self.bot.guilds:
            channel = self._get_queue_channel(guild)
            if not channel:
                continue

            data = load_data()
            if not data.get("seeded_from_backup"):
                self._seed_from_backup()

            await self._restore_queue_data_if_needed(channel)
            self.bot.loop.create_task(self._startup_refresh(channel))

    async def _restore_queue_data_if_needed(
        self,
        channel: discord.TextChannel,
    ) -> None:
        data = load_data()
        if not is_persisted_queue_empty(data):
            return

        message = None
        if data.get("message_id"):
            try:
                message_channel = channel
                if data.get("channel_id") and data["channel_id"] != channel.id:
                    maybe_channel = channel.guild.get_channel(data["channel_id"])
                    if isinstance(maybe_channel, discord.TextChannel):
                        message_channel = maybe_channel

                message = await message_channel.fetch_message(data["message_id"])
            except discord.NotFound:
                message = None

        if not message:
            message = await self._find_existing_queue_message(channel)

        if not message:
            logger.warning(
                "Queue data file is empty and no board message was found to restore from."
            )
            return

        if import_queue_from_board_message(data, message):
            logger.info(
                "Restored queue data from board message %s in #%s",
                message.id,
                channel.name,
            )

    async def _startup_refresh(self, channel: discord.TextChannel) -> None:
        try:
            await self._refresh_queue_message(channel)
        except Exception:
            logger.exception("Startup queue refresh failed for #%s", channel.name)

    def _seed_from_backup(self) -> None:
        if not import_queues_backup():
            return

        data = load_data()
        data["seeded_from_backup"] = True
        save_data(data)
        logger.info("Imported queue data from queues_backup.json")

    async def _find_existing_queue_message(
        self,
        channel: discord.TextChannel,
    ) -> discord.Message | None:
        async for message in channel.history(limit=50):
            if message.author.id != self.bot.user.id:
                continue

            if is_queue_board_message(message):
                return message
        return None

    async def _refresh_queue_message(
        self,
        channel: discord.TextChannel,
    ) -> discord.Message:
        data = load_data()
        name_cache = data.setdefault("name_cache", {})
        guild = channel.guild

        for key in data["categories"]:
            for entry in data["categories"][key]:
                user_id = entry_user_id(entry)
                if user_id is None or str(user_id) in name_cache:
                    continue

                member = guild.get_member(user_id)
                if member:
                    name_cache[str(user_id)] = member.display_name
                    continue

                try:
                    user = await self.bot.fetch_user(user_id)
                    name_cache[str(user_id)] = user.display_name or user.name
                except discord.NotFound:
                    pass

        save_data(data)
        embed = build_queue_embed(
            data["categories"],
            guild,
            name_cache,
            data,
            self.bot,
        )

        message = None
        if data.get("message_id"):
            try:
                message_channel = channel
                if data.get("channel_id") and data["channel_id"] != channel.id:
                    maybe_channel = channel.guild.get_channel(data["channel_id"])
                    if isinstance(maybe_channel, discord.TextChannel):
                        message_channel = maybe_channel

                message = await message_channel.fetch_message(data["message_id"])
            except discord.NotFound:
                message = None
                data["message_id"] = None
                save_data(data)

        if not message:
            message = await self._find_existing_queue_message(channel)

        if message:
            if message.channel.id != channel.id:
                message = await channel.send(embed=embed)
            else:
                needs_update = not message.embeds or not queue_embeds_match(
                    message.embeds[0],
                    embed,
                )
                if needs_update:
                    try:
                        await message.edit(content=None, embed=embed)
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to edit queue board message %s in #%s",
                            message.id,
                            channel.name,
                        )
                        raise

            data["message_id"] = message.id
            data["channel_id"] = channel.id
            save_data(data)
        else:
            message = await channel.send(embed=embed)
            data["message_id"] = message.id
            data["channel_id"] = channel.id
            save_data(data)

        return message

    def _get_queue_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = os.getenv("QUEUE_CHANNEL_ID")
        if channel_id:
            try:
                channel = guild.get_channel(int(channel_id))
            except ValueError:
                channel = None

            if isinstance(channel, discord.TextChannel):
                return channel

        return discord.utils.get(guild.text_channels, name="queue")

    async def _get_queue_channel_or_reply(
        self,
        interaction: discord.Interaction,
    ) -> discord.TextChannel | None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return None

        channel = self._get_queue_channel(interaction.guild)
        if not channel:
            await interaction.response.send_message(
                "Queue channel not found. Set `QUEUE_CHANNEL_ID` in `.env` or create a `#queue` channel.",
                ephemeral=True,
            )
            return None

        return channel

    async def _save_and_refresh(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        confirmation: str,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        try:
            await self._refresh_queue_message(channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't update the queue board. Check that I have "
                "**View Channel**, **Send Messages**, **Embed Links**, and "
                "**Manage Messages** in the queue channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Failed to refresh queue board")
            await interaction.followup.send(
                "Failed to update the queue board. Try `/queue post` again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(confirmation, ephemeral=True)

    async def _defer_and_get_channel(
        self,
        interaction: discord.Interaction,
    ) -> discord.TextChannel | None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return None

        channel = self._get_queue_channel(interaction.guild)
        if not channel:
            await interaction.response.send_message(
                "Queue channel not found. Set `QUEUE_CHANNEL_ID` in `.env` or create a `#queue` channel.",
                ephemeral=True,
            )
            return None

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        return channel

    queue_group = app_commands.Group(
        name="queue",
        description="Manage the server queue board",
        default_permissions=discord.Permissions(administrator=True),
    )

    creator_group = app_commands.Group(
        name="creator",
        description="Create, reorder, or delete queue sections",
        parent=queue_group,
    )

    @creator_group.command(name="new", description="Create a new queue section")
    @app_commands.describe(text="Section header text (shown bold on the board)")
    @app_commands.checks.has_permissions(administrator=True)
    async def creator_new(
        self,
        interaction: discord.Interaction,
        text: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()

        try:
            _, header = create_custom_section(data, text)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Created section **{header}**.",
        )

    @creator_group.command(name="delete", description="Delete a queue section")
    @app_commands.describe(
        section="Section to delete (pick from list or type part of the name)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def creator_delete(
        self,
        interaction: discord.Interaction,
        section: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()

        try:
            header = delete_custom_section(data, section)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Deleted section **{header}** and removed all members in it.",
        )

    @creator_group.command(name="move", description="Move a section up or down")
    @app_commands.describe(
        section="Section to move",
        direction="Move up or down on the board",
    )
    @app_commands.choices(
        direction=[
            app_commands.Choice(name="Up", value="up"),
            app_commands.Choice(name="Down", value="down"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def creator_move(
        self,
        interaction: discord.Interaction,
        section: str,
        direction: app_commands.Choice[str],
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        if section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        try:
            header = move_section(data, section, direction.value)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Moved **{header}** {direction.name.lower()}.",
        )

    @creator_move.autocomplete("section")
    async def creator_move_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @creator_delete.autocomplete("section")
    async def creator_delete_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        data = load_data()
        choices = []

        for label, value in delete_section_autocomplete_choices(data, current):
            choices.append(
                app_commands.Choice(name=label[:100], value=value[:100])
            )

        return choices

    @queue_group.command(name="add", description="Add a member to a queue section")
    @app_commands.describe(
        section="Queue section to add to",
        member="Member to add",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def add(
        self,
        interaction: discord.Interaction,
        section: str,
        member: discord.Member,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        if section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        entries = data["categories"][section]

        if any(entry_matches_member(existing, member) for existing in entries):
            await interaction.followup.send(
                f"{member.mention} is already in **{section_display(section, data)}**.",
                ephemeral=True,
            )
            return

        entry = member_to_entry(member)
        entries.append(entry)
        cache_member_name(data, member)
        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Added {member.mention} to **{section_display(section, data)}**.",
        )

    @add.autocomplete("section")
    async def add_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @queue_group.command(name="move", description="Move a member from one section to another")
    @app_commands.describe(
        member="Member to move",
        from_section="Section to move from",
        to_section="Section to move to",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def move(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        from_section: str,
        to_section: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        if from_section == to_section:
            await interaction.followup.send(
                "Source and destination sections must be different.",
                ephemeral=True,
            )
            return

        data = load_data()
        if from_section not in data["categories"] or to_section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        source = data["categories"][from_section]
        destination = data["categories"][to_section]
        index = find_entry_index(source, member)

        if index is None:
            await interaction.followup.send(
                f"{member.mention} is not in **{section_display(from_section, data)}**.",
                ephemeral=True,
            )
            return

        if any(entry_matches_member(existing, member) for existing in destination):
            await interaction.followup.send(
                f"{member.mention} is already in **{section_display(to_section, data)}**.",
                ephemeral=True,
            )
            return

        entry = source.pop(index)
        destination.append(entry)
        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Moved {member.mention} from **{section_display(from_section, data)}** "
            f"to **{section_display(to_section, data)}**.",
        )

    @move.autocomplete("from_section")
    async def move_from_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @move.autocomplete("to_section")
    async def move_to_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @queue_group.command(
        name="attach",
        description="Link an existing queue message and import its contents",
    )
    @app_commands.describe(message_id="ID of the queue board message")
    @app_commands.checks.has_permissions(administrator=True)
    async def attach(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        try:
            target_id = int(message_id)
        except ValueError:
            await interaction.followup.send(
                "Message ID must be a number.",
                ephemeral=True,
            )
            return

        try:
            message = await channel.fetch_message(target_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"Message `{target_id}` was not found in {channel.mention}.",
                ephemeral=True,
            )
            return

        data = load_data()
        data["message_id"] = message.id
        data["channel_id"] = channel.id
        if message.embeds:
            parsed = parse_queue_embed(message.embeds[0], message.mentions, data)
        else:
            parsed = parse_queue_message(
                message.content or "",
                message.mentions,
                data,
            )

        categories = data.setdefault("categories", {})
        for key, entries in parsed.items():
            categories[key] = entries

        for member in message.mentions:
            cache_member_name(data, member)
        save_data(data)

        await self._refresh_queue_message(channel)
        await interaction.followup.send(
            f"Attached to message `{message.id}` and imported queue data.",
            ephemeral=True,
        )

    @queue_group.command(name="backup", description="Post a JSON backup to the log channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def backup(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        message = await send_queue_backup(interaction.guild)

        if not message:
            await interaction.followup.send(
                "Log channel not found. Set `LOG_CHANNEL_ID` in `.env` or create `#aaron_bot_log`.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Backup posted to {message.channel.mention}.",
            ephemeral=True,
        )

    @queue_group.command(name="clear", description="Clear all members from a queue section")
    @app_commands.describe(section="Queue section to clear")
    @app_commands.checks.has_permissions(administrator=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        section: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        if section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        data["categories"][section] = []
        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Cleared **{section_display(section, data)}**.",
        )

    @clear.autocomplete("section")
    async def clear_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @queue_group.command(name="list", description="Show the current queue data")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_queue(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        data = load_data()
        content = _build_queue_description(
            data["categories"],
            interaction.guild,
            data.get("name_cache", {}),
            data,
        )

        if len(content) <= 1900:
            await interaction.followup.send(
                f"```\n{content}\n```",
                ephemeral=True,
            )
            return

        payload = io.BytesIO(content.encode("utf-8"))
        file = discord.File(payload, filename="queue_list.txt")
        await interaction.followup.send(
            "Queue is too long for one message. Here is the full list:",
            file=file,
            ephemeral=True,
        )

    @queue_group.command(name="purge", description="Remove a member from every section")
    @app_commands.describe(member="Member to remove everywhere")
    @app_commands.checks.has_permissions(administrator=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        removed_from = remove_member_from_all(data, member)

        if not removed_from:
            await interaction.followup.send(
                f"{member.mention} was not found in any section.",
                ephemeral=True,
            )
            return

        save_data(data)
        sections = ", ".join(
            f"**{section_display(key, data)}**" for key in removed_from
        )
        await self._save_and_refresh(
            interaction,
            channel,
            f"Purged {member.mention} from: {sections}.",
        )

    @queue_group.command(name="remove", description="Remove a member from a queue section")
    @app_commands.describe(
        member="Member to remove",
        section="Queue section to remove from",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        section: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        if section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        entries = data["categories"][section]
        index = find_entry_index(entries, member)

        if index is None:
            await interaction.followup.send(
                f"{member.mention} is not in **{section_display(section, data)}**.",
                ephemeral=True,
            )
            return

        entries.pop(index)
        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Removed {member.mention} from **{section_display(section, data)}**.",
        )

    @remove.autocomplete("section")
    async def remove_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @queue_group.command(
        name="userremoval",
        description="Remove queue users by display name (font-insensitive)",
    )
    @app_commands.describe(
        name="Part of the display name to match (e.g. try matches 𝑻𝑹𝒀)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def userremoval(
        self,
        interaction: discord.Interaction,
        name: str,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        query = name.strip()
        if not query:
            await interaction.followup.send(
                "Type part of a display name to search for.",
                ephemeral=True,
            )
            return

        data = load_data()
        matches = find_queue_users_by_name(data, query, interaction.guild)

        if not matches:
            await interaction.followup.send(
                f"No queue users matched `{query}`.",
                ephemeral=True,
            )
            return

        view = UserRemovalConfirmView(
            cog=self,
            channel=channel,
            author_id=interaction.user.id,
            matches=matches,
            query=query,
        )
        message = await interaction.followup.send(
            _format_userremoval_preview(matches, data, query),
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = message

    @queue_group.command(
        name="remove_all",
        description="Remove a member from every section they appear in",
    )
    @app_commands.describe(member="Member to remove everywhere")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_all(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        removed_from = remove_member_from_all(data, member)

        if not removed_from:
            await interaction.followup.send(
                f"{member.mention} is not in any section.",
                ephemeral=True,
            )
            return

        save_data(data)
        sections = ", ".join(
            f"**{section_display(key, data)}**" for key in removed_from
        )
        await self._save_and_refresh(
            interaction,
            channel,
            f"Removed {member.mention} from: {sections}.",
        )

    @queue_group.command(
        name="reorder",
        description="Move a member to a specific position within a section",
    )
    @app_commands.describe(
        section="Queue section to reorder",
        member="Member to move",
        position="New position (1 = top of section)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def reorder(
        self,
        interaction: discord.Interaction,
        section: str,
        member: discord.Member,
        position: app_commands.Range[int, 1, 100],
    ):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        data = load_data()
        if section not in data["categories"]:
            await interaction.followup.send(
                "Unknown section. Pick one from the list.",
                ephemeral=True,
            )
            return

        entries = data["categories"][section]
        index = find_entry_index(entries, member)

        if index is None:
            await interaction.followup.send(
                f"{member.mention} is not in **{section_display(section, data)}**.",
                ephemeral=True,
            )
            return

        if position > len(entries):
            await interaction.followup.send(
                f"Position must be between 1 and {len(entries)}.",
                ephemeral=True,
            )
            return

        entry = entries.pop(index)
        entries.insert(position - 1, entry)
        save_data(data)
        await self._save_and_refresh(
            interaction,
            channel,
            f"Moved {member.mention} to position **{position}** in "
            f"**{section_display(section, data)}**.",
        )

    @reorder.autocomplete("section")
    async def reorder_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ):
        return await self.section_autocomplete(interaction, current)

    @queue_group.command(name="ping", description="Test if the bot responds to you")
    @app_commands.checks.has_permissions(administrator=True)
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Pong! The bot received your command, {interaction.user.mention}.",
            ephemeral=True,
        )

    @queue_group.command(name="post", description="Post or refresh the queue board")
    @app_commands.checks.has_permissions(administrator=True)
    async def post(self, interaction: discord.Interaction):
        channel = await self._defer_and_get_channel(interaction)
        if not channel:
            return

        try:
            message = await self._refresh_queue_message(channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't update the queue board. Check that I have "
                "**View Channel**, **Send Messages**, **Embed Links**, and "
                "**Manage Messages** in the queue channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Failed to refresh queue board")
            await interaction.followup.send(
                "Failed to update the queue board. Try again in a moment.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Queue board updated in {channel.mention}.",
            ephemeral=True,
        )

        try:
            await message.pin()
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(QueueCog(bot))
