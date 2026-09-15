"""Tiny JSON-file storage for per-Discord-user Riot sessions."""

import json
import os

FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")


def _load() -> dict:
    if not os.path.exists(FILE_PATH):
        return {}
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save(data: dict) -> None:
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_user(discord_id: int) -> dict | None:
    return _load().get(str(discord_id))


def save_user(discord_id: int, session: dict) -> None:
    data = _load()
    data[str(discord_id)] = session
    _save(data)


def delete_user(discord_id: int) -> bool:
    data = _load()
    if str(discord_id) in data:
        del data[str(discord_id)]
        _save(data)
        return True
    return False
