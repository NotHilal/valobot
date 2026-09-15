"""
Minimal Riot / VALORANT client-API wrapper.

Login uses the same browser-based flow playvalorant.com itself uses
(client_id "play-valorant-web-prod"): the user is sent to Riot's own
hosted login page, logs in there, and is redirected back with an
access_token/id_token pair in the URL fragment. We never see the
password - only the URL the user pastes back to us. This also sidesteps
Riot blocking direct username/password logins that originate from
recognized hosting/VPS IP ranges (which a bot's own server always is).

These are Riot's undocumented internal client endpoints (not the
public developer RSO API), used by every third-party VALORANT shop
tool. Riot can change them without notice.
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

AUTH_BASE = "https://auth.riotgames.com"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
GEO_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"
VALORANT_API_BASE = "https://valorant-api.com/v1"

VP_CURRENCY_ID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"

_CLIENT_PLATFORM = base64.b64encode(
    json.dumps(
        {
            "platformType": "PC",
            "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit",
            "platformChipset": "Unknown",
        }
    ).encode()
).decode()

_client_version_cache: dict = {"value": None, "fetched_at": 0.0}


class RiotAuthError(Exception):
    """Raised when the pasted redirect URL can't be turned into a session."""


class SessionExpiredError(Exception):
    """Raised when a stored session is no longer accepted by Riot."""


def build_login_url() -> str:
    params = {
        "client_id": "play-valorant-web-prod",
        "redirect_uri": "https://playvalorant.com/opt_in",
        "response_type": "token id_token",
        "scope": "account openid",
        "nonce": "1",
    }
    return f"{AUTH_BASE}/authorize?{urlencode(params)}"


def _parse_tokens_from_url(url: str) -> tuple[str, str]:
    """Pull access_token and id_token out of the pasted redirect URL."""
    url = url.strip()
    fragment = urlparse(url).fragment
    if not fragment and "#" in url:
        # Some clients hand back params after a literal '#' that urlparse
        # won't treat as a fragment if the URL is otherwise malformed.
        fragment = url.split("#", 1)[1]
    params = parse_qs(fragment)
    access_token = params.get("access_token", [None])[0]
    id_token = params.get("id_token", [None])[0]
    if not access_token or not id_token:
        raise RiotAuthError(
            "That doesn't look like a valid redirect URL. Run /login again, click the link, "
            "log in, then paste the full URL you land on afterward."
        )
    return access_token, id_token


def _jwt_expiry(token: str) -> float:
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return float(payload.get("exp", time.time() + 3600))
    except Exception:
        return time.time() + 3600


async def _get_client_version(http: aiohttp.ClientSession) -> str:
    cached = _client_version_cache["value"]
    if cached and (time.time() - _client_version_cache["fetched_at"]) < 3600:
        return cached
    async with http.get(f"{VALORANT_API_BASE}/version") as resp:
        resp.raise_for_status()
        data = await resp.json()
    version = data["data"]["riotClientVersion"]
    _client_version_cache["value"] = version
    _client_version_cache["fetched_at"] = time.time()
    return version


async def _finish_session(http: aiohttp.ClientSession, access_token: str, id_token: str) -> dict:
    """Turn a fresh access_token/id_token pair into a storable session dict."""
    headers = {"Authorization": f"Bearer {access_token}"}

    async with http.post(ENTITLEMENTS_URL, headers=headers, json={}) as resp:
        if resp.status != 200:
            raise RiotAuthError("Riot rejected that login. Please try /login again.")
        entitlements = (await resp.json())["entitlements_token"]

    async with http.post(f"{AUTH_BASE}/userinfo", headers=headers, json={}) as resp:
        if resp.status != 200:
            raise RiotAuthError("Riot rejected that login. Please try /login again.")
        userinfo = await resp.json()
    puuid = userinfo["sub"]
    acct = userinfo.get("acct") or {}
    riot_id = None
    if acct.get("game_name"):
        riot_id = f"{acct['game_name']}#{acct.get('tag_line', '')}"

    async with http.put(GEO_URL, headers=headers, json={"id_token": id_token}) as resp:
        if resp.status != 200:
            raise RiotAuthError("Could not determine your account region.")
        geo = await resp.json()
    shard = geo.get("affinities", {}).get("live")
    if not shard:
        raise RiotAuthError("Could not determine your account region.")

    return {
        "puuid": puuid,
        "riot_id": riot_id,
        "shard": shard,
        "access_token": access_token,
        "entitlements_token": entitlements,
        "expires_at": _jwt_expiry(access_token),
    }


async def create_session(redirect_url: str) -> dict:
    """Exchange a pasted browser-redirect URL for a stored Riot session dict."""
    access_token, id_token = _parse_tokens_from_url(redirect_url)
    async with aiohttp.ClientSession() as http:
        return await _finish_session(http, access_token, id_token)


async def get_storefront(http: aiohttp.ClientSession, session: dict) -> dict:
    if time.time() >= session.get("expires_at", 0):
        raise SessionExpiredError()

    client_version = await _get_client_version(http)
    headers = {
        "Authorization": f"Bearer {session['access_token']}",
        "X-Riot-Entitlements-JWT": session["entitlements_token"],
        "X-Riot-ClientPlatform": _CLIENT_PLATFORM,
        "X-Riot-ClientVersion": client_version,
    }
    url = f"https://pd.{session['shard']}.a.pvp.net/store/v3/storefront/{session['puuid']}"
    async with http.post(url, headers=headers, json={}) as resp:
        if resp.status in (400, 401):
            raise SessionExpiredError()
        resp.raise_for_status()
        return await resp.json()


def parse_daily_offers(storefront: dict) -> tuple[list[dict], int]:
    """Returns (list of {offer_id, cost, item_id}, seconds_until_refresh)."""
    panel = storefront["SkinsPanelLayout"]
    offers = []
    for offer in panel["SingleItemStoreOffers"]:
        cost = offer.get("Cost", {}).get(VP_CURRENCY_ID)
        item_id = None
        for reward in offer.get("Rewards", []):
            item_id = reward.get("ItemID")
            break
        offers.append({"offer_id": offer["OfferID"], "cost": cost, "item_id": item_id})
    remaining = panel.get("SingleItemOffersRemainingDurationInSeconds", 0)
    return offers, remaining


async def get_skin_details(http: aiohttp.ClientSession, level_uuid: str) -> dict:
    url = f"{VALORANT_API_BASE}/weapons/skinlevels/{level_uuid}"
    async with http.get(url, params={"language": "en-US"}) as resp:
        if resp.status != 200:
            return {"name": "Unknown Skin", "icon": None}
        data = (await resp.json())["data"]
    return {"name": data.get("displayName", "Unknown Skin"), "icon": data.get("displayIcon")}
