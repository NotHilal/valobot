"""Tiny JSON-file storage for per-Discord-user Riot sessions and skin collections."""

import asyncio
import copy
import json
import os
from pathlib import Path

_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(_DIR, "users.json")
COLLECTIONS_FILE = os.path.join(_DIR, "collections.json")
COUNTING_FILE = os.path.join(_DIR, "counting.json")
CHANNEL_LOCKS_FILE = os.path.join(_DIR, "channel_locks.json")
INSTANT_BANS_FILE = os.path.join(_DIR, "instant_bans.json")
BANNED_WORDS_FILE = os.path.join(_DIR, "banned_words.json")
DELETED_MESSAGES_FILE = os.path.join(_DIR, "deleted_messages.json")
DELETED_MEDIA_DIR = os.path.join(_DIR, "deleted_media")
LOG_CHANNELS_FILE = os.path.join(_DIR, "log_channels.json")

# Guards read-modify-write sequences on collections.json. Any caller that reads a
# collection, mutates it, and writes it back must hold this across the whole
# sequence (including any `await` in between) - otherwise two interleaved
# commands touching the same or different users' collections can race and one
# write silently clobbers the other.
collection_lock = asyncio.Lock()


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_user(discord_id: int) -> dict | None:
    return _load(USERS_FILE).get(str(discord_id))


def save_user(discord_id: int, session: dict) -> None:
    data = _load(USERS_FILE)
    data[str(discord_id)] = session
    _save(USERS_FILE, data)


def delete_user(discord_id: int) -> bool:
    data = _load(USERS_FILE)
    if str(discord_id) in data:
        del data[str(discord_id)]
        _save(USERS_FILE, data)
        return True
    return False


def _collection_key(guild_id: int, discord_id: int) -> str:
    # Collections are scoped per-server: the same person has an independent
    # collection in each server the bot is in, not one shared globally.
    return f"{guild_id}:{discord_id}"


def get_collection(guild_id: int, discord_id: int) -> dict:
    return _load(COLLECTIONS_FILE).get(_collection_key(guild_id, discord_id)) or {
        "items": [],
        "charges": 0,
        "last_charge_period_index": None,
    }


def save_collection(guild_id: int, discord_id: int, collection: dict) -> None:
    data = _load(COLLECTIONS_FILE)
    data[_collection_key(guild_id, discord_id)] = collection
    _save(COLLECTIONS_FILE, data)


def delete_collection(guild_id: int, discord_id: int) -> bool:
    data = _load(COLLECTIONS_FILE)
    key = _collection_key(guild_id, discord_id)
    if key in data:
        del data[key]
        _save(COLLECTIONS_FILE, data)
        return True
    return False


def get_counting_state(guild_id: int) -> dict:
    return _load(COUNTING_FILE).get(str(guild_id)) or {
        "channel_id": None,
        "count": 0,
        "last_user_id": None,
        "best_count": 0,
    }


def save_counting_state(guild_id: int, state: dict) -> None:
    data = _load(COUNTING_FILE)
    data[str(guild_id)] = state
    _save(COUNTING_FILE, data)


def get_channel_lock(guild_id: int, group: str) -> int | None:
    """The channel id a command group is restricted to, or None if unrestricted."""
    return _load(CHANNEL_LOCKS_FILE).get(str(guild_id), {}).get(group)


def set_channel_lock(guild_id: int, group: str, channel_id: int | None) -> None:
    data = _load(CHANNEL_LOCKS_FILE)
    guild_locks = data.get(str(guild_id), {})
    guild_locks[group] = channel_id
    data[str(guild_id)] = guild_locks
    _save(CHANNEL_LOCKS_FILE, data)


def get_instant_ban_list(guild_id: int) -> list[int]:
    return _load(INSTANT_BANS_FILE).get(str(guild_id), [])


def add_instant_ban(guild_id: int, user_id: int) -> bool:
    """Returns False if the user was already on the list."""
    data = _load(INSTANT_BANS_FILE)
    ids = data.get(str(guild_id), [])
    if user_id in ids:
        return False
    ids.append(user_id)
    data[str(guild_id)] = ids
    _save(INSTANT_BANS_FILE, data)
    return True


def get_banned_words(guild_id: int) -> dict[str, int]:
    """Lowercase word -> timeout duration in minutes."""
    return _load(BANNED_WORDS_FILE).get(str(guild_id), {})


def set_banned_word(guild_id: int, word: str, minutes: int) -> None:
    data = _load(BANNED_WORDS_FILE)
    guild_words = data.get(str(guild_id), {})
    guild_words[word.lower()] = minutes
    data[str(guild_id)] = guild_words
    _save(BANNED_WORDS_FILE, data)


def remove_banned_word(guild_id: int, word: str) -> bool:
    data = _load(BANNED_WORDS_FILE)
    guild_words = data.get(str(guild_id), {})
    key = word.lower()
    if key not in guild_words:
        return False
    del guild_words[key]
    data[str(guild_id)] = guild_words
    _save(BANNED_WORDS_FILE, data)
    return True


def add_deleted_message(guild_id: int, user_id: int, entry: dict) -> None:
    data = _load(DELETED_MESSAGES_FILE)
    guild_logs = data.get(str(guild_id), {})
    guild_logs.setdefault(str(user_id), []).append(entry)
    data[str(guild_id)] = guild_logs
    _save(DELETED_MESSAGES_FILE, data)


def get_deleted_messages(guild_id: int, user_id: int) -> list[dict]:
    """Oldest first."""
    return _load(DELETED_MESSAGES_FILE).get(str(guild_id), {}).get(str(user_id), [])


def deleted_media_path(guild_id: int, name: str) -> str:
    return os.path.join(DELETED_MEDIA_DIR, str(guild_id), name)


def save_deleted_media(guild_id: int, name: str, data: bytes) -> None:
    path = deleted_media_path(guild_id, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def get_log_channel(guild_id: int) -> int | None:
    """The channel deleted messages get posted in, or None if logging is off."""
    return _load(LOG_CHANNELS_FILE).get(str(guild_id))


def set_log_channel(guild_id: int, channel_id: int | None) -> None:
    data = _load(LOG_CHANNELS_FILE)
    data[str(guild_id)] = channel_id
    _save(LOG_CHANNELS_FILE, data)


LOGS_PAUSED_FILE = os.path.join(_DIR, "logs_paused.json")


def logs_paused(guild_id: int) -> bool:
    """True while the bot owner has paused logging - the log channel is kept."""
    return _load(LOGS_PAUSED_FILE).get(str(guild_id), False)


def set_logs_paused(guild_id: int, paused: bool) -> None:
    data = _load(LOGS_PAUSED_FILE)
    data[str(guild_id)] = paused
    _save(LOGS_PAUSED_FILE, data)


REACTION_ROLES_FILE = os.path.join(_DIR, "reaction_roles.json")


LEGACY_MENU_NAME = "roles"


def _load_role_menus(guild_id: int) -> dict:
    """"channel_id:menu name" -> menu. Menus saved before channels could hold
    several are keyed by bare channel id, so they get the legacy name."""
    guild_menus = _load(REACTION_ROLES_FILE).get(str(guild_id), {})
    return {(k if ":" in k else f"{k}:{LEGACY_MENU_NAME}"): v for k, v in guild_menus.items()}


def get_role_menu(guild_id: int, channel_id: int, name: str) -> dict:
    """One of the bot's reaction-role menus in a channel: its message id and an
    emoji -> role id map. message_id is None until the menu is posted."""
    return _load_role_menus(guild_id).get(f"{channel_id}:{name}") or {
        "message_id": None,
        "roles": {},
    }


def get_role_menu_names(guild_id: int, channel_id: int) -> list[str]:
    prefix = f"{channel_id}:"
    return [k[len(prefix):] for k in _load_role_menus(guild_id) if k.startswith(prefix)]


def save_role_menu(guild_id: int, channel_id: int, name: str, menu: dict | None) -> None:
    """Pass None to delete the menu."""
    data = _load(REACTION_ROLES_FILE)
    guild_menus = _load_role_menus(guild_id)
    if menu is None:
        guild_menus.pop(f"{channel_id}:{name}", None)
    else:
        guild_menus[f"{channel_id}:{name}"] = menu
    data[str(guild_id)] = guild_menus
    _save(REACTION_ROLES_FILE, data)


def find_role_menu_by_message(guild_id: int, message_id: int) -> dict | None:
    for menu in _load_role_menus(guild_id).values():
        if menu.get("message_id") == message_id:
            return menu
    return None


CUSTOM_GAMES_FILE = Path(_DIR) / "custom_games.json"


# Per-server custom game data (games in progress, leaderboard, map list), used as
# client.store by customgame.py. Copied from the Mikasa bot, along with
# customgame.py itself.
class Store:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            # Starting empty would overwrite every server's settings on the next save.
            raise SystemExit(f"{self.path} is corrupted ({exc}). Fix or delete it, then restart.") from exc

    def get(self, guild_id: int) -> dict:
        # A deep copy, so a caller's edits only count once passed to update().
        return copy.deepcopy(self._data.get(str(guild_id), {}))

    def update(self, guild_id: int, **changes) -> None:
        """Set the given values. A value of None removes that setting."""
        entry = self._data.setdefault(str(guild_id), {})
        for key, value in changes.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        self._save()

    def _save(self) -> None:
        # Write then swap, so a crash mid-write can't leave a truncated file.
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)
