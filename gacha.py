"""Daily skin roll / collection game, layered on top of the real Valorant skin catalog."""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import aiohttp

VALORANT_API_BASE = "https://valorant-api.com/v1"
CUSTOM_SKINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_skins.json")

# Rarity tiers, lowest to highest - matches valorant-api.com's real content tiers.
RARITY_TIERS = ["Select", "Deluxe", "Premium", "Exclusive", "Ultra"]
RARITY_RANK = {name: i for i, name in enumerate(RARITY_TIERS)}
RARITY_WEIGHTS = {"Select": 45, "Deluxe": 30, "Premium": 15, "Exclusive": 7, "Ultra": 3}

_pool_cache: dict = {"value": None, "fetched_at": 0.0}
_POOL_TTL_SECONDS = 24 * 3600


async def _fetch_official_pool(http: aiohttp.ClientSession) -> dict[str, list[dict]]:
    async with http.get(f"{VALORANT_API_BASE}/contenttiers") as resp:
        tiers = (await resp.json())["data"]
    tier_names = {t["uuid"]: t["devName"] for t in tiers}

    async with http.get(f"{VALORANT_API_BASE}/weapons/skins", params={"language": "en-US"}) as resp:
        skins = (await resp.json())["data"]

    pool: dict[str, list[dict]] = {name: [] for name in RARITY_TIERS}
    for skin in skins:
        rarity = tier_names.get(skin.get("contentTierUuid"))
        icon = skin.get("displayIcon")
        if rarity not in pool or not icon:
            continue
        pool[rarity].append({"id": skin["uuid"], "name": skin["displayName"], "icon": icon, "rarity": rarity})
    return pool


def load_custom_skins_raw() -> list[dict]:
    if not os.path.exists(CUSTOM_SKINS_FILE):
        return []
    try:
        with open(CUSTOM_SKINS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_custom_skins_raw(items: list[dict]) -> None:
    with open(CUSTOM_SKINS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


def _load_custom_skins() -> dict[str, list[dict]]:
    extra: dict[str, list[dict]] = {name: [] for name in RARITY_TIERS}
    for item in load_custom_skins_raw():
        rarity = str(item.get("rarity", "")).strip().title()
        if rarity not in extra or not item.get("name") or not item.get("icon"):
            continue
        extra[rarity].append(
            {
                "id": f"custom-{item['name'].lower().replace(' ', '-')}",
                "name": item["name"],
                "icon": item["icon"],
                "rarity": rarity,
            }
        )
    return extra


async def get_pool(http: aiohttp.ClientSession) -> dict[str, list[dict]]:
    """Combined official + custom skin pool, grouped by rarity tier."""
    cached = _pool_cache["value"]
    if not cached or (time.time() - _pool_cache["fetched_at"]) > _POOL_TTL_SECONDS:
        cached = await _fetch_official_pool(http)
        _pool_cache["value"] = cached
        _pool_cache["fetched_at"] = time.time()

    pool = {name: list(items) for name, items in cached.items()}
    for name, items in _load_custom_skins().items():
        pool[name].extend(items)
    return pool


def roll(pool: dict[str, list[dict]]) -> dict:
    """Pick one random item, weighted by rarity tier, then stamp it with an obtained-at time."""
    available = {name: items for name, items in pool.items() if items}
    rarity = random.choices(list(available.keys()), weights=[RARITY_WEIGHTS[n] for n in available], k=1)[0]
    return stamp(random.choice(available[rarity]))


def top_items(items: list[dict], n: int = 5) -> list[dict]:
    return sorted(items, key=lambda it: (RARITY_RANK.get(it["rarity"], -1), it.get("obtained_at", "")), reverse=True)[
        :n
    ]


def find_in_pool(pool: dict[str, list[dict]], name: str) -> dict | None:
    name_lower = name.lower()
    for items in pool.values():
        for item in items:
            if item["name"].lower() == name_lower:
                return item
    return None


def stamp(item: dict) -> dict:
    stamped = dict(item)
    stamped["obtained_at"] = datetime.now(timezone.utc).isoformat()
    return stamped


def find_item(collection: dict, name: str) -> dict | None:
    for item in collection.get("items", []):
        if item["name"].lower() == name.lower():
            return item
    return None


def pop_item(collection: dict, name: str) -> dict | None:
    items = collection.get("items", [])
    for i, item in enumerate(items):
        if item["name"].lower() == name.lower():
            return items.pop(i)
    return None


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def seconds_until_next_utc_day() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())
