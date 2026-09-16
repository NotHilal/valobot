"""Tiny JSON-file storage for per-Discord-user Riot sessions and skin collections."""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(_DIR, "users.json")
COLLECTIONS_FILE = os.path.join(_DIR, "collections.json")


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
    return _load(COLLECTIONS_FILE).get(str(discord_id)) or {"last_roll": None, "items": []}


def save_collection(discord_id: int, collection: dict) -> None:
    data = _load(COLLECTIONS_FILE)
    data[str(discord_id)] = collection
    _save(COLLECTIONS_FILE, data)
