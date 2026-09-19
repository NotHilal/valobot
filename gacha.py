"""Daily skin roll / collection game, layered on top of the real Valorant skin catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

VALORANT_API_BASE = "https://valorant-api.com/v1"
CUSTOM_SKINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_skins.json")
ICON_OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon_overrides.json")

# Rarity tiers, lowest to highest - matches valorant-api.com's real content tiers.
# These are the only tiers offered as choices for admin-added custom skins.
RARITY_TIERS = ["Select", "Deluxe", "Premium", "Exclusive", "Ultra"]

# Agents are a separate category from skin rarities (not selectable for custom
# skins), but they're folded into the same weighted roll and slot into the
# rarity ranking between Exclusive and Ultra to reflect their drop odds.
AGENT_CATEGORY = "Agent"
_DISPLAY_RARITY_ORDER = ["Select", "Deluxe", "Premium", "Exclusive", AGENT_CATEGORY, "Ultra"]
RARITY_RANK = {name: i for i, name in enumerate(_DISPLAY_RARITY_ORDER)}
# Weights double as exact percentages (they sum to 100).
RARITY_WEIGHTS = {"Select": 36, "Deluxe": 25, "Premium": 17, "Exclusive": 8, AGENT_CATEGORY: 6, "Ultra": 8}

# Embed accent color per rarity (hex, 0xRRGGBB) - kept mid-toned, not neon or dark.
RARITY_COLORS = {
    "Select": 0x22D3EE,  # cyan
    "Deluxe": 0x2DD4BF,  # green-blue (teal)
    "Premium": 0xE0529C,  # pink
    "Exclusive": 0xCD7F32,  # bronze
    AGENT_CATEGORY: 0xE63946,  # red
    "Ultra": 0xE8B34A,  # gold
}

_pool_cache: dict = {"value": None, "fetched_at": 0.0, "tier_icons": {}}
_POOL_TTL_SECONDS = 24 * 3600


def load_icon_overrides() -> dict[str, str]:
    """Maps a skin/agent name to a replacement icon URL, used instead of the
    official one - for cases like an oversized official image."""
    if not os.path.exists(ICON_OVERRIDES_FILE):
        return {}
    try:
        with open(ICON_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


async def _fetch_agents(http: aiohttp.ClientSession) -> list[dict]:
    async with http.get(
        f"{VALORANT_API_BASE}/agents", params={"isPlayableCharacter": "true", "language": "en-US"}
    ) as resp:
        agents = (await resp.json())["data"]

    result = []
    for agent in agents:
        icon = agent.get("fullPortrait") or agent.get("displayIcon")
        if not icon:
            continue
        result.append(
            {
                "id": agent["uuid"],
                "name": agent["displayName"],
                "icon": icon,
                "rarity": AGENT_CATEGORY,
                "tier_icon": agent.get("displayIcon"),
            }
        )
    return result


async def _fetch_official_pool(http: aiohttp.ClientSession) -> tuple[dict[str, list[dict]], dict[str, str | None]]:
    async with http.get(f"{VALORANT_API_BASE}/contenttiers") as resp:
        tiers = (await resp.json())["data"]
    tier_names = {t["uuid"]: t["devName"] for t in tiers}
    tier_icons = {t["devName"]: t["displayIcon"] for t in tiers}

    async with http.get(f"{VALORANT_API_BASE}/weapons/skins", params={"language": "en-US"}) as resp:
        skins = (await resp.json())["data"]

    pool: dict[str, list[dict]] = {name: [] for name in RARITY_TIERS}
    for skin in skins:
        rarity = tier_names.get(skin.get("contentTierUuid"))
        icon = skin.get("displayIcon")
        if rarity not in pool or not icon:
            continue
        pool[rarity].append(
            {
                "id": skin["uuid"],
                "name": skin["displayName"],
                "icon": icon,
                "rarity": rarity,
                "tier_icon": tier_icons.get(rarity),
            }
        )

    pool[AGENT_CATEGORY] = await _fetch_agents(http)

    overrides = load_icon_overrides()
    all_items = [item for items in pool.values() for item in items]
    sem = asyncio.Semaphore(30)
    # Riot/valorant-api.com serves the exact same placeholder image for
    # several unrelated skins that don't have real art yet (confirmed: 4
    # different skins byte-for-byte identical). Any icon whose content hash
    # is shared by more than one item is almost certainly one of these
    # placeholders, not real art, so hash everything and drop the dupes.
    hash_to_ids: dict[str, list[str]] = {}

    async def _check(item: dict) -> None:
        override_url = overrides.get(item["name"])
        if override_url:
            item["icon"] = override_url
            return  # manually-provided replacement - trust it, skip the hash check
        async with sem:
            try:
                async with http.get(item["icon"], timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.read()
            except aiohttp.ClientError:
                return  # can't tell - don't exclude a skin over a transient network hiccup
        digest = hashlib.sha256(data).hexdigest()
        hash_to_ids.setdefault(digest, []).append(item["id"])

    await asyncio.gather(*(_check(item) for item in all_items))

    placeholder_ids = {item_id for ids in hash_to_ids.values() if len(ids) > 1 for item_id in ids}
    if placeholder_ids:
        pool = {
            rarity: [item for item in items if item["id"] not in placeholder_ids] for rarity, items in pool.items()
        }

    return pool, tier_icons


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


def _load_custom_skins(tier_icons: dict[str, str | None]) -> dict[str, list[dict]]:
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
                "tier_icon": tier_icons.get(rarity),
            }
        )
    return extra


async def get_pool(http: aiohttp.ClientSession) -> dict[str, list[dict]]:
    """Combined official + custom skin pool, grouped by rarity tier."""
    cached = _pool_cache["value"]
    if not cached or (time.time() - _pool_cache["fetched_at"]) > _POOL_TTL_SECONDS:
        cached, tier_icons = await _fetch_official_pool(http)
        _pool_cache["value"] = cached
        _pool_cache["fetched_at"] = time.time()
        _pool_cache["tier_icons"] = tier_icons

    pool = {name: list(items) for name, items in cached.items()}
    for name, items in _load_custom_skins(_pool_cache["tier_icons"]).items():
        pool[name].extend(items)
    return pool


class EmptyPoolError(Exception):
    """Raised when there are no skins available to roll (e.g. the API fetch failed)."""


def roll(pool: dict[str, list[dict]], boost: dict | None = None) -> dict:
    """Pick one random item, weighted by rarity tier, then stamp it with an obtained-at time.

    `boost` (optional) is {"item_id": ..., "percent": N}: *if* the rolled
    tier contains that item, it has exactly an N% chance of being the one
    picked out of that tier (the remaining (100-N)% is split evenly across
    every other item in the tier). It has no effect if a different tier gets
    rolled that time - the boost only matters when its own tier comes up.
    """
    available = {name: items for name, items in pool.items() if items}
    if not available:
        raise EmptyPoolError()
    rarity = random.choices(list(available.keys()), weights=[RARITY_WEIGHTS[n] for n in available], k=1)[0]
    candidates = available[rarity]

    if boost and len(candidates) > 1 and any(c["id"] == boost["item_id"] for c in candidates):
        percent = boost["percent"]
        other_weight = (100 - percent) / (len(candidates) - 1)
        weights = [percent if c["id"] == boost["item_id"] else other_weight for c in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]
    else:
        chosen = random.choice(candidates)
    return stamp(chosen)


def sort_items_by_rarity(items: list[dict]) -> list[dict]:
    """Rarest (and, within a rarity, most recently obtained) first."""
    return sorted(items, key=lambda it: (RARITY_RANK.get(it["rarity"], -1), it.get("obtained_at", "")), reverse=True)


def rarity_counts(items: list[dict]) -> list[tuple[str, int]]:
    """(rarity, count) pairs for every rarity present, rarest first."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item["rarity"]] = counts.get(item["rarity"], 0) + 1
    return sorted(counts.items(), key=lambda pair: RARITY_RANK.get(pair[0], -1), reverse=True)


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


PARIS_TZ = ZoneInfo("Europe/Paris")
MAX_ROLL_CHARGES = 2

# --- Demo mode --------------------------------------------------------
# Flip TEST_MODE to True before a live demo: every charge sync instantly sets
# your charges to TEST_MODE_CHARGES instead of waiting for the real Paris
# midnight/noon boundary, so you can roll repeatedly on the spot. Set it back
# to False (and MAX_ROLL_CHARGES back to 2) for normal play.
TEST_MODE = False
TEST_MODE_CHARGES = 5


def _current_period_index() -> int:
    """Half-day ticks in Paris local time: one boundary at midnight, one at
    noon. DST-aware, so it tracks Paris wall-clock time year-round rather than
    a fixed UTC offset."""
    now = datetime.now(PARIS_TZ)
    half = 0 if now.hour < 12 else 1
    return now.date().toordinal() * 2 + half


def sync_roll_charges(collection: dict) -> dict:
    """Regenerates roll charges in place: +1 for every midnight/noon boundary
    crossed since the collection was last synced, capped at MAX_ROLL_CHARGES.
    There's no background timer, so this is computed lazily whenever the
    collection is touched. In TEST_MODE this is skipped in favor of letting
    charges decrement normally from TEST_MODE_CHARGES down to 0, then
    topping back up to TEST_MODE_CHARGES the next time you're out - so you
    can demo spending charges without ever getting soft-locked."""
    if TEST_MODE:
        if collection.get("charges", 0) <= 0:
            collection["charges"] = TEST_MODE_CHARGES
        return collection

    now_idx = _current_period_index()
    last_idx = collection.get("last_charge_period_index")
    if last_idx is None:
        last_idx = now_idx - 1  # brand-new collection: start with 1 charge available
    elapsed = max(0, now_idx - last_idx)
    if elapsed:
        collection["charges"] = min(MAX_ROLL_CHARGES, collection.get("charges", 0) + elapsed)
        collection["last_charge_period_index"] = now_idx
    collection.setdefault("charges", 0)
    return collection


def seconds_until_next_roll_period() -> int:
    now = datetime.now(PARIS_TZ)
    if now.hour < 12:
        next_reset = now.replace(hour=12, minute=0, second=0, microsecond=0)
    else:
        next_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((next_reset - now).total_seconds())
