import datetime
import json
import re
from pathlib import Path

import discord

DATA_FILE = Path(__file__).parent / "queue_data.json"
BACKUP_SEED_FILE = Path(__file__).parent / "queues_backup.json"

CATEGORIES = [
    ("packages", "🔥 PACKAGES"),
    ("tattoos", "✒️ TATTOOS"),
    ("chains", "⛓️ CHAINS"),
    ("clothing", "👗 CLOTHING"),
    ("skins", "🥸 SKINS"),
    ("prio", "🧡 PRIO"),
    ("most_hated", "💜 MOST HATED"),
    ("lovely_designs", "💖 LOVELY DESIGNS"),
    ("tb_designs", "🤍 TB DESIGNS"),
    ("blackline_apparel", "💙 Blackline Aparrel"),
    ("liveries", "🚗 Liveries"),
]

CATEGORY_KEYS = {key for key, _ in CATEGORIES}
CATEGORY_HEADERS = {key: header for key, header in CATEGORIES}
MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
CORRUPT_MENTION_PATTERN = re.compile(r"^<@@(?P<value>.+)>$")


def _section_headers(data: dict) -> dict[str, str]:
    headers = dict(CATEGORY_HEADERS)
    for entry in data.get("custom_sections", []):
        headers[entry["key"]] = entry["header"]
    return headers


def ensure_section_order(data: dict) -> list[str]:
    headers = _section_headers(data)
    hidden = set(data.get("hidden_sections", []))
    default_order = list(CATEGORY_HEADERS.keys()) + [
        entry["key"] for entry in data.get("custom_sections", [])
    ]
    order = list(data.get("section_order") or default_order)
    seen: set[str] = set()
    deduped: list[str] = []

    for key in order:
        if key in headers and key not in seen and key not in hidden:
            deduped.append(key)
            seen.add(key)

    for key in default_order:
        if key not in seen and key not in hidden:
            deduped.append(key)
            seen.add(key)

    data["section_order"] = deduped
    return deduped


def get_all_categories(data: dict) -> list[tuple[str, str]]:
    headers = _section_headers(data)
    return [(key, headers[key]) for key in ensure_section_order(data)]


def get_all_category_keys(data: dict) -> set[str]:
    return {key for key, _ in get_all_categories(data)}


def _slugify_header(header: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", header.casefold()).strip("_")
    return slug[:40] if slug else "section"


def create_custom_section(data: dict, header_text: str) -> tuple[str, str]:
    header = header_text.strip()
    if not header:
        raise ValueError("Section text cannot be empty.")

    for _, existing_header in get_all_categories(data):
        if existing_header.casefold() == header.casefold():
            raise ValueError("A section with that name already exists.")

    base = _slugify_header(header)
    key = f"custom_{base}"
    existing_keys = set(data.get("categories", {}).keys())
    suffix = 1
    while key in existing_keys or key in CATEGORY_KEYS:
        key = f"custom_{base}_{suffix}"
        suffix += 1

    data.setdefault("custom_sections", []).append({"key": key, "header": header})
    data.setdefault("categories", {})[key] = []
    ensure_section_order(data)
    return key, header


def find_section_matches(data: dict, query: str) -> list[tuple[str, str]]:
    query_fold = query.strip().casefold()
    if not query_fold:
        return []

    matches: list[tuple[str, str]] = []
    for key, header in get_all_categories(data):
        header_fold = header.casefold()
        slug = _slugify_header(header)
        if (
            query_fold in header_fold
            or query_fold in slug
            or query_fold in key.casefold()
            or query == key
        ):
            matches.append((key, header))

    return matches


def find_custom_section_matches(data: dict, query: str) -> list[tuple[str, str]]:
    query_fold = query.strip().casefold()
    if not query_fold:
        return []

    matches: list[tuple[str, str]] = []
    for entry in data.get("custom_sections", []):
        key = entry["key"]
        header = entry["header"]
        header_fold = header.casefold()
        slug = _slugify_header(header)

        if (
            query_fold in header_fold
            or query_fold in slug
            or query_fold in key.casefold()
        ):
            matches.append((key, header))

    return matches


def custom_section_keys(data: dict) -> set[str]:
    return {entry["key"] for entry in data.get("custom_sections", [])}


def delete_queue_section(data: dict, query: str) -> str:
    query = query.strip()
    custom_keys = custom_section_keys(data)
    visible_keys = {key for key, _ in get_all_categories(data)}

    if query in visible_keys:
        key = query
        header = section_display(key, data)
    else:
        matches = find_section_matches(data, query)
        if not matches:
            raise ValueError("No section matched that name.")
        if len(matches) > 1:
            names = ", ".join(f"**{header}**" for _, header in matches)
            raise ValueError(
                f"That matches multiple sections: {names}. Be more specific."
            )
        key, header = matches[0]

    if key in custom_keys:
        data["custom_sections"] = [
            entry for entry in data.get("custom_sections", []) if entry["key"] != key
        ]
    elif key in CATEGORY_KEYS:
        hidden = data.setdefault("hidden_sections", [])
        if key not in hidden:
            hidden.append(key)

    data.setdefault("categories", {}).pop(key, None)
    data["section_order"] = [
        section_key
        for section_key in data.get("section_order", [])
        if section_key != key
    ]
    ensure_section_order(data)
    return header


def delete_custom_section(data: dict, query: str) -> str:
    return delete_queue_section(data, query)


def delete_section_autocomplete_choices(
    data: dict,
    current: str,
) -> list[tuple[str, str]]:
    current_lower = current.casefold()
    choices: list[tuple[str, str]] = []

    for key, header in get_all_categories(data):
        count = len(data["categories"].get(key, []))
        member_label = "member" if count == 1 else "members"
        label = f"{header} · {count} {member_label}"

        if (
            not current
            or current_lower in header.casefold()
            or current_lower in key.casefold()
        ):
            choices.append((label, key))

    return choices[:25]


def move_section(data: dict, section_key: str, direction: str) -> str:
    order = ensure_section_order(data)
    if section_key not in order:
        raise ValueError("Unknown section.")

    index = order.index(section_key)
    if direction == "up":
        if index == 0:
            raise ValueError("This section is already at the top.")
        order[index], order[index - 1] = order[index - 1], order[index]
    elif direction == "down":
        if index >= len(order) - 1:
            raise ValueError("This section is already at the bottom.")
        order[index], order[index + 1] = order[index + 1], order[index]
    else:
        raise ValueError("Direction must be up or down.")

    data["section_order"] = order
    return section_display(section_key, data)


def ensure_section_for_header(data: dict, header: str) -> str:
    clean = _strip_bold(header.strip())
    for key, existing in get_all_categories(data):
        if existing == clean:
            return key
    key, _ = create_custom_section(data, clean)
    return key


def default_data() -> dict:
    return {
        "message_id": None,
        "channel_id": None,
        "initial_backup_sent": False,
        "seeded_from_backup": False,
        "name_cache": {},
        "custom_sections": [],
        "hidden_sections": [],
        "section_order": [key for key, _ in CATEGORIES],
        "categories": {key: [] for key in CATEGORY_KEYS},
    }


def load_data() -> dict:
    if not DATA_FILE.exists():
        return default_data()

    with DATA_FILE.open(encoding="utf-8") as file:
        data = json.load(file)

    categories = data.setdefault("categories", {})
    for key in get_all_category_keys(data):
        entries = categories.setdefault(key, [])
        categories[key] = [_normalize_entry(entry) for entry in entries]

    data.setdefault("custom_sections", [])
    data.setdefault("hidden_sections", [])
    data.setdefault("message_id", None)
    data.setdefault("channel_id", None)
    data.setdefault("initial_backup_sent", False)
    data.setdefault("seeded_from_backup", False)
    data.setdefault("name_cache", {})
    ensure_section_order(data)
    return data


def save_data(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def _normalize_entry(entry: object) -> str:
    if isinstance(entry, int):
        return str(entry)

    name = str(entry).strip()
    if not name:
        return name

    corrupt = CORRUPT_MENTION_PATTERN.match(name)
    if corrupt:
        name = corrupt.group("value")

    if name.startswith("<@@") and name.endswith(">"):
        name = name[3:-1]

    if name.startswith("<@") and name.endswith(">"):
        match = MENTION_PATTERN.search(name)
        if match:
            return match.group(1)

    if name.startswith("@") and name[1:].isdigit():
        return name[1:]

    if name.isdigit():
        return name

    if not name.startswith("@"):
        return f"@{name}"
    return name


def entry_user_id(entry: str) -> int | None:
    if entry.isdigit():
        return int(entry)
    return None


def _entry_user_id(entry: str) -> int | None:
    return entry_user_id(entry)


def _entry_display_name(
    entry: str,
    guild: discord.Guild | None,
    name_cache: dict[str, str] | None = None,
) -> str:
    user_id = _entry_user_id(entry)
    if user_id is not None:
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name

        if name_cache and str(user_id) in name_cache:
            return name_cache[str(user_id)]

        return str(user_id)

    return normalize_backup_name(entry)


def cache_member_name(data: dict, member: discord.Member) -> None:
    cache = data.setdefault("name_cache", {})
    cache[str(member.id)] = member.display_name


def export_queues_backup_json(
    guild: discord.Guild | None = None,
    name_cache: dict[str, str] | None = None,
) -> str:
    data = load_data()
    cache = name_cache if name_cache is not None else data.get("name_cache", {})
    export = {
        key: [
            _normalize_entry(f"@{_entry_display_name(entry, guild, cache)}")
            for entry in data["categories"].get(key, [])
        ]
        for key, _ in get_all_categories(data)
    }
    return json.dumps(export, indent=2, ensure_ascii=False)


def load_queues_backup() -> dict[str, list[str]] | None:
    if not BACKUP_SEED_FILE.exists():
        return None

    with BACKUP_SEED_FILE.open(encoding="utf-8") as file:
        raw = json.load(file)

    return {
        key: [_normalize_entry(entry) for entry in raw.get(key, [])]
        for key in CATEGORY_KEYS
    }


def import_queues_backup() -> bool:
    backup = load_queues_backup()
    if backup is None:
        return False

    data = load_data()
    data["categories"] = backup
    save_data(data)
    return True


def normalize_backup_name(entry: str) -> str:
    return _normalize_entry(entry).lstrip("@")


def section_display(section_key: str, data: dict | None = None) -> str:
    if data is None:
        data = load_data()
    for key, header in get_all_categories(data):
        if key == section_key:
            return header
    return CATEGORY_HEADERS.get(section_key, section_key)


def member_to_entry(member: discord.Member) -> str:
    return str(member.id)


def entry_matches_member(entry: str, member: discord.Member) -> bool:
    user_id = _entry_user_id(entry)
    if user_id is not None:
        return user_id == member.id

    entry_name = normalize_backup_name(entry).casefold()
    candidates = (
        member.display_name,
        member.global_name,
        member.name,
        member.nick,
    )
    return any(
        candidate and candidate.casefold() == entry_name
        for candidate in candidates
    )


def find_entry_index(entries: list[str], member: discord.Member) -> int | None:
    for index, entry in enumerate(entries):
        if entry_matches_member(entry, member):
            return index
    return None


def remove_member_from_all(data: dict, member: discord.Member) -> list[str]:
    removed_from = []
    for key in data["categories"]:
        entries = data["categories"][key]
        kept = [entry for entry in entries if not entry_matches_member(entry, member)]
        if len(kept) != len(entries):
            data["categories"][key] = kept
            removed_from.append(key)
    return removed_from


def remove_user_id_from_all(
    data: dict,
    guild: discord.Guild,
    user_id: int,
) -> list[str]:
    member = guild.get_member(user_id)
    if member:
        return remove_member_from_all(data, member)

    removed_from = []
    for key in data["categories"]:
        entries = data["categories"][key]
        kept = [
            entry
            for entry in entries
            if _entry_user_id(entry) != user_id
        ]
        if len(kept) != len(entries):
            data["categories"][key] = kept
            removed_from.append(key)
    return removed_from


def parse_queue_message(
    content: str,
    mentions: list[discord.Member] | None = None,
    data: dict | None = None,
) -> dict[str, list[str]]:
    data = data if data is not None else load_data()
    categories: dict[str, list[str]] = {}
    current_key = None
    body = unwrap_queue_message_content(content)
    mention_map = {member.id: member for member in mentions or []}

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        plain = _strip_bold(stripped)
        if "🔥 QUEUE 🔥" in plain or plain == "Updated On an Orderly Basis":
            continue
        if plain.startswith("Queue managed by staff"):
            continue

        if stripped.startswith("•"):
            if not current_key:
                continue

            entry = _parse_queue_bullet(stripped[1:], mention_map)
            if entry and entry not in categories[current_key]:
                categories[current_key].append(entry)
            continue

        if plain == "None":
            continue

        current_key = ensure_section_for_header(data, plain)
        categories.setdefault(current_key, [])

    return categories


def _parse_queue_bullet(
    bullet: str,
    mention_map: dict[int, discord.Member],
) -> str | None:
    bullet = _strip_bold(bullet.strip())
    corrupt = CORRUPT_MENTION_PATTERN.match(bullet)
    if corrupt:
        bullet = corrupt.group("value")

    mention_ids = MENTION_PATTERN.findall(bullet)
    if mention_ids:
        user_id = int(mention_ids[0])
        member = mention_map.get(user_id)
        return str(user_id) if member is None else str(member.id)
    if bullet.isdigit():
        return bullet
    if bullet.startswith("@"):
        return _normalize_entry(bullet)
    if bullet:
        return _normalize_entry(bullet)
    return None


def _parse_queue_content_lines(
    lines: list[str],
    mention_map: dict[int, discord.Member],
    data: dict,
) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}
    header_to_key = {header: key for key, header in get_all_categories(data)}
    current_key = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("# ") and not stripped.startswith("## "):
            header = stripped[2:].strip()
            if header == QUEUE_EMBED_TITLE:
                continue
            current_key = header_to_key.get(header)
            if not current_key:
                current_key = ensure_section_for_header(data, header)
                header_to_key[header] = current_key
            categories.setdefault(current_key, [])
            continue

        plain = _strip_bold(stripped)
        if plain == "Updated On an Orderly Basis":
            continue
        if plain.startswith("Queue managed by staff"):
            continue
        if plain == "None":
            continue

        if stripped.startswith("•") and current_key:
            entry = _parse_queue_bullet(stripped[1:], mention_map)
            if entry and entry not in categories[current_key]:
                categories[current_key].append(entry)

    return categories


def parse_queue_embed(
    embed: discord.Embed,
    mentions: list[discord.Member] | None = None,
    data: dict | None = None,
) -> dict[str, list[str]]:
    data = data if data is not None else load_data()
    mention_map = {member.id: member for member in mentions or []}
    lines: list[str] = []

    if embed.description:
        lines.extend(embed.description.splitlines())

    for field in embed.fields:
        if field.name == "\u200b" and field.value == "\u200b":
            continue

        field_name = _strip_bold(field.name.strip())
        if field_name.startswith("▬▬ "):
            field_name = field_name[4:].strip()

        if field.name != "\u200b":
            lines.append(f"# {field_name}")
        lines.extend(field.value.splitlines())

    return _parse_queue_content_lines(lines, mention_map, data)


QUEUE_EMBED_TITLE = "🔥 QUEUE 🔥"
QUEUE_EMBED_COLOR = discord.Color.from_rgb(153, 50, 204)


def _format_section_lines(
    key: str,
    header: str,
    entries: list[str],
    guild: discord.Guild | None,
    name_cache: dict[str, str] | None,
) -> list[str]:
    lines = ["", f"# {header}", ""]

    if entries:
        lines.extend(_entry_to_line(entry, guild, name_cache) for entry in entries)
    else:
        lines.append("**None**")

    return lines


def _build_queue_description(
    categories: dict[str, list[str]],
    guild: discord.Guild | None,
    name_cache: dict[str, str] | None,
    data: dict,
) -> str:
    lines = [
        f"# {QUEUE_EMBED_TITLE}",
        "",
        "**Updated On an Orderly Basis**",
    ]

    for key, header in get_all_categories(data):
        lines.extend(
            _format_section_lines(
                key,
                header,
                categories.get(key, []),
                guild,
                name_cache,
            )
        )

    return "\n".join(lines)


def _apply_queue_embed_style(
    embed: discord.Embed,
    guild: discord.Guild | None,
    bot: discord.Client | None,
    *,
    footer_text: str,
) -> None:
    if bot and bot.user:
        embed.set_author(
            name=bot.user.display_name or bot.user.name,
            icon_url=bot.user.display_avatar.url,
        )
    elif guild:
        embed.set_author(name=guild.name)

    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    footer_icon = bot.user.display_avatar.url if bot and bot.user else None
    embed.set_footer(text=footer_text, icon_url=footer_icon)


def build_queue_embed(
    categories: dict[str, list[str]],
    guild: discord.Guild | None = None,
    name_cache: dict[str, str] | None = None,
    data: dict | None = None,
    bot: discord.Client | None = None,
) -> discord.Embed:
    data = data if data is not None else load_data()
    embed = discord.Embed(
        color=QUEUE_EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    _apply_queue_embed_style(
        embed,
        guild,
        bot,
        footer_text="Queue managed by staff • Use /queue to make changes",
    )

    description = _build_queue_description(
        categories,
        guild,
        name_cache,
        data,
    )
    embed.description = description[:4096]

    return embed


def queue_embeds_match(
    existing: discord.Embed,
    desired: discord.Embed,
) -> bool:
    if existing.title != desired.title or existing.description != desired.description:
        return False

    existing_footer = existing.footer.text if existing.footer else None
    desired_footer = desired.footer.text if desired.footer else None
    if existing_footer != desired_footer:
        return False

    existing_author = existing.author.name if existing.author else None
    desired_author = desired.author.name if desired.author else None
    if existing_author != desired_author:
        return False

    existing_thumb = existing.thumbnail.url if existing.thumbnail else None
    desired_thumb = desired.thumbnail.url if desired.thumbnail else None
    if existing_thumb != desired_thumb:
        return False

    if len(existing.fields) != len(desired.fields):
        return False

    return all(
        left.name == right.name and left.value == right.value
        for left, right in zip(existing.fields, desired.fields)
    )


def is_queue_board_message(message: discord.Message) -> bool:
    if message.embeds:
        embed = message.embeds[0]
        if "QUEUE" in (embed.title or ""):
            return True
        if "QUEUE" in (embed.description or ""):
            return True

    body = unwrap_queue_message_content(message.content or "")
    return "🔥 QUEUE 🔥" in body


def unwrap_queue_message_content(content: str) -> str:
    stripped = content.strip()

    if stripped.startswith(">>>"):
        stripped = stripped[3:].lstrip()

    if stripped.startswith("**") and stripped.endswith("**"):
        inner = stripped[2:-2].strip()
        if inner.startswith("``"):
            stripped = inner
        elif not inner.startswith("```"):
            return inner

    for fence in ("```", "``"):
        if not stripped.startswith(fence):
            continue

        lines = stripped.splitlines()
        if lines[0].strip() == fence:
            lines = lines[1:]
        if lines and lines[-1].strip() == fence:
            lines = lines[:-1]
        return "\n".join(lines)

    return stripped


def _strip_bold(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("**") and stripped.endswith("**"):
        return stripped[2:-2].strip()
    return stripped


def _entry_to_line(
    entry: str,
    guild: discord.Guild | None,
    name_cache: dict[str, str] | None = None,
) -> str:
    display_name = _entry_display_name(entry, guild, name_cache)
    return f"• **{display_name}**"


def format_queue_message(
    categories: dict[str, list[str]],
    guild: discord.Guild | None = None,
    name_cache: dict[str, str] | None = None,
    data: dict | None = None,
) -> str:
    data = data if data is not None else load_data()
    lines = [
        "🔥 QUEUE 🔥",
        "Updated On an Orderly Basis",
        "",
    ]

    for key, header in get_all_categories(data):
        lines.append(header)
        entries = categories.get(key, [])

        if entries:
            lines.extend(
                _entry_to_line(entry, guild, name_cache) for entry in entries
            )
        else:
            lines.append("None")

        lines.append("")

    lines.append("Queue managed by staff • Use /queue to make changes")
    body = "\n".join(lines)
    return f"**{body}**"
