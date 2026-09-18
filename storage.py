"""Tiny JSON-file storage for per-Discord-user Riot sessions and skin collections."""

import asyncio
import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(_DIR, "users.json")
COLLECTIONS_FILE = os.path.join(_DIR, "collections.json")
COUNTING_FILE = os.path.join(_DIR, "counting.json")
CHANNEL_LOCKS_FILE = os.path.join(_DIR, "channel_locks.json")

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


def get_collection(discord_id: int) -> dict:
    return _load(COLLECTIONS_FILE).get(str(discord_id)) or {
        "items": [],
        "charges": 0,
        "last_charge_period_index": None,
    }


def save_collection(discord_id: int, collection: dict) -> None:
    data = _load(COLLECTIONS_FILE)
    data[str(discord_id)] = collection
    _save(COLLECTIONS_FILE, data)


def delete_collection(discord_id: int) -> bool:
    data = _load(COLLECTIONS_FILE)
    if str(discord_id) in data:
        del data[str(discord_id)]
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
