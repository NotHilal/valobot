"""Minimal Discord bot: /login, /store, /logout for a personal VALORANT shop viewer,
plus /rollskin, /collection, /trade for a daily skin-collecting side game,
plus a counting game in a designated channel."""

import asyncio
import io
import math
import os
import re
from datetime import datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import counting
import gacha
import riot
import storage

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True  # needed to read plain chat messages for the counting game
intents.members = True  # needed to see people joining, for /instantban
# Bigger message cache so /checkdeleted can recover older messages - discord.py
# only reports the content of deleted messages it still has cached.
client = discord.Client(intents=intents, max_messages=5000)
tree = app_commands.CommandTree(client)


async def _check_channel_lock(interaction: discord.Interaction, group: str) -> bool:
    """True if this command may proceed. Otherwise sends an ephemeral error
    naming the channel it's restricted to and returns False."""
    if interaction.guild is None:
        return True
    locked_channel_id = storage.get_channel_lock(interaction.guild.id, group)
    if locked_channel_id is None or interaction.channel_id == locked_channel_id:
        return True
    channel = interaction.guild.get_channel(locked_channel_id)
    where = channel.mention if channel else "the designated channel"
    await interaction.response.send_message(f"This command can only be used in {where}.", ephemeral=True)
    return False


class LoginLinkModal(discord.ui.Modal, title="Paste your login link"):
    url = discord.ui.TextInput(
        label="The URL you landed on after logging in",
        style=discord.TextStyle.paragraph,
        placeholder="https://playvalorant.com/opt_in#access_token=...",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            session = await riot.create_session(self.url.value)
        except riot.RiotAuthError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            print(f"login error for user {interaction.user.id}: {exc!r}")
            await interaction.followup.send(
                "❌ Something went wrong talking to Riot. Please try /login again.", ephemeral=True
            )
            return

        storage.save_user(interaction.user.id, session)
        name = session.get("riot_id") or "your account"
        await interaction.followup.send(
            f"✅ Logged in as **{name}**. Use `/store` to see your daily store.", ephemeral=True
        )


class LoginView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Paste login link", style=discord.ButtonStyle.primary, emoji="🔗")
    async def paste_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LoginLinkModal())


def _build_login_embed(title: str = "🔗 Link Your Riot Account") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description="Follow these steps to connect your VALORANT account. It only takes a minute!",
        color=discord.Color.blurple(),
    )
    if client.user:
        embed.set_thumbnail(url=client.user.display_avatar.url)

    embed.add_field(
        name="1️⃣ Log in",
        value=f"**[Click here to log in with Riot](<{riot.build_login_url()}>)**",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Copy the link you land on",
        value=(
            "You'll land on a page that looks broken (error 404) — that's normal! "
            "Just copy its link from your address bar."
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Come back and paste it",
        value="Click the **Paste login link** button below and paste it in.",
        inline=False,
    )
    embed.set_footer(text="Your Riot password is never seen or stored by this bot.")
    return embed


@tree.command(name="login", description="Link your Riot account to see your daily VALORANT shop")
async def login(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "shop"):
        return

    await interaction.response.send_message(
        embed=_build_login_embed(),
        view=LoginView(),
        ephemeral=True,
    )


@tree.command(name="store", description="Show your daily VALORANT storefront")
async def shop(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "shop"):
        return

    session = storage.get_user(interaction.user.id)
    if not session:
        await interaction.response.send_message("You're not logged in. Run `/login` first.", ephemeral=True)
        return

    if riot.is_session_expired(session):
        storage.delete_user(interaction.user.id)
        await interaction.response.send_message(
            embed=_build_login_embed("⏰ Your Riot session expired"),
            view=LoginView(),
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    async with aiohttp.ClientSession() as http:
        try:
            storefront = await riot.get_storefront(http, session)
        except riot.SessionExpiredError:
            storage.delete_user(interaction.user.id)
            await interaction.followup.send(
                embed=_build_login_embed("⏰ Your Riot session expired"),
                view=LoginView(),
                ephemeral=True,
            )
            return
        except Exception as exc:
            print(f"shop error for user {interaction.user.id}: {exc!r}")
            await interaction.followup.send("Couldn't reach Riot's servers right now. Try again shortly.")
            return

        offers, remaining_seconds = riot.parse_daily_offers(storefront)

        embeds = []
        for offer in offers:
            if offer["item_id"]:
                details = await riot.get_skin_details(http, offer["item_id"])
            else:
                details = {"name": "Unknown Skin", "icon": None, "tier_icon": None}

            price = f"{offer['cost']} VP" if offer["cost"] is not None else "Price unavailable"
            embed = discord.Embed(title=details["name"], color=discord.Color.red())
            embed.set_author(name=price, icon_url=details.get("tier_icon"))
            if details["icon"]:
                embed.set_thumbnail(url=details["icon"])
            embeds.append(embed)

    remaining_seconds = max(remaining_seconds, 0)
    hours, rem = divmod(remaining_seconds, 3600)
    minutes = rem // 60

    header = f"🎮 **{interaction.user.display_name}'s Daily VALORANT Store** — refreshes in {hours}h {minutes}m"
    await interaction.followup.send(content=header, embeds=embeds[:10], ephemeral=False)


@tree.command(name="nightmarket", description="Show your Night Market bonus offers, if the event is currently running")
async def nightmarket(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "shop"):
        return

    session = storage.get_user(interaction.user.id)
    if not session:
        await interaction.response.send_message("You're not logged in. Run `/login` first.", ephemeral=True)
        return

    if riot.is_session_expired(session):
        storage.delete_user(interaction.user.id)
        await interaction.response.send_message(
            embed=_build_login_embed("⏰ Your Riot session expired"),
            view=LoginView(),
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    async with aiohttp.ClientSession() as http:
        try:
            storefront = await riot.get_storefront(http, session)
        except riot.SessionExpiredError:
            storage.delete_user(interaction.user.id)
            await interaction.followup.send(
                embed=_build_login_embed("⏰ Your Riot session expired"),
                view=LoginView(),
                ephemeral=True,
            )
            return
        except Exception as exc:
            print(f"nightmarket error for user {interaction.user.id}: {exc!r}")
            await interaction.followup.send("Couldn't reach Riot's servers right now. Try again shortly.")
            return

        offers, remaining_seconds = riot.parse_night_market_offers(storefront)
        if not offers:
            await interaction.followup.send("🌙 The Night Market isn't running right now. Check back later!")
            return

        embeds = []
        for offer in offers:
            if offer["item_id"]:
                details = await riot.get_skin_details(http, offer["item_id"])
            else:
                details = {"name": "Unknown Skin", "icon": None, "tier_icon": None}

            if offer["cost"] is not None and offer["discounted_cost"] is not None:
                price = f"~~{offer['cost']} VP~~ **{offer['discounted_cost']} VP** (-{offer['discount_percent']}%)"
            else:
                price = "Price unavailable"

            embed = discord.Embed(title=details["name"], color=discord.Color.gold())
            embed.set_author(name=price, icon_url=details.get("tier_icon"))
            if details["icon"]:
                embed.set_thumbnail(url=details["icon"])
            embeds.append(embed)

    remaining_seconds = max(remaining_seconds, 0)
    hours, rem = divmod(remaining_seconds, 3600)
    minutes = rem // 60

    header = f"🌙 **{interaction.user.display_name}'s Night Market** — ends in {hours}h {minutes}m"
    await interaction.followup.send(content=header, embeds=embeds[:10], ephemeral=False)


@tree.command(name="logout", description="logout your account")
async def logout(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "shop"):
        return

    deleted = storage.delete_user(interaction.user.id)
    if deleted:
        await interaction.response.send_message("✅ Your Riot session has been removed.", ephemeral=True)
    else:
        await interaction.response.send_message("You weren't logged in.", ephemeral=True)



@tree.command(
    name="rollskin",
    description="Roll for a random Valorant skin (up to 2 charges, +1 at midnight & noon Paris time)",
)
async def roll_cmd(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "roll"):
        return
    if interaction.guild is None:
        return
    guild_id = interaction.guild.id

    forced_item = None
    roll_boost = None
    boost_before_use = None
    async with storage.collection_lock:
        collection = storage.get_collection(guild_id, interaction.user.id)
        gacha.sync_roll_charges(collection)
        storage.save_collection(guild_id, interaction.user.id, collection)

        if collection["charges"] <= 0:
            remaining = gacha.seconds_until_next_roll_period()
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            await interaction.response.send_message(
                f"You're out of roll charges. Next charge in {hours}h {minutes}m.", ephemeral=True
            )
            return

        # Spend a charge now, before the network fetch below, so a second
        # /rollskin fired while this one is still in flight can't spend the
        # same charge twice.
        collection["charges"] -= 1
        owned_ids = {it["id"] for it in collection.get("items", []) if "id" in it}

        # A guaranteed /setnextroll skin takes priority over a /nr odds boost.
        forced_item = collection.pop("forced_roll", None)
        if forced_item is None:
            active_boost = collection.get("roll_boost")
            if active_boost:
                boost_before_use = dict(active_boost)
                roll_boost = {"item_id": active_boost["item_id"], "percent": active_boost["percent"]}
                active_boost["rolls_left"] -= 1
                if active_boost["rolls_left"] <= 0:
                    collection.pop("roll_boost", None)
                else:
                    collection["roll_boost"] = active_boost
        storage.save_collection(guild_id, interaction.user.id, collection)

    await interaction.response.defer(thinking=True)

    if forced_item is not None:
        item = gacha.stamp(forced_item)
    else:
        try:
            async with aiohttp.ClientSession() as http:
                pool = await gacha.get_pool(http)
            item = gacha.roll(pool, boost=roll_boost, exclude_ids=owned_ids)
        except Exception as exc:
            collected_all = isinstance(exc, gacha.EmptyPoolError) and bool(owned_ids)
            if not collected_all:
                print(f"roll error for user {interaction.user.id}: {exc!r}")
            async with storage.collection_lock:
                collection = storage.get_collection(guild_id, interaction.user.id)
                collection["charges"] = min(gacha.MAX_ROLL_CHARGES, collection.get("charges", 0) + 1)
                # Restore the boost to its pre-decrement state too, since this
                # roll attempt never actually happened.
                if boost_before_use is not None:
                    collection["roll_boost"] = boost_before_use
                storage.save_collection(guild_id, interaction.user.id, collection)
            if collected_all:
                await interaction.followup.send("🏆 You already own every skin in the pool — nothing left to roll!")
            else:
                await interaction.followup.send("Couldn't fetch the skin pool right now. Try again shortly.")
            return

    async with storage.collection_lock:
        collection = storage.get_collection(guild_id, interaction.user.id)
        collection["items"].append(item)
        storage.save_collection(guild_id, interaction.user.id, collection)

    rolls_left = collection["charges"]
    embed = discord.Embed(
        title=f"🎉 {interaction.user.display_name} rolled: {item['name']}",
        description=f"{rolls_left} charge{'s' if rolls_left != 1 else ''} left.",
        color=gacha.RARITY_COLORS.get(item["rarity"], discord.Color.gold()),
    )
    embed.set_author(name=item["rarity"], icon_url=item.get("tier_icon"))
    embed.set_thumbnail(url=item["icon"])
    await interaction.followup.send(embed=embed)


class CollectionView(discord.ui.View):
    PAGE_SIZE = 5

    def __init__(self, owner_id: int, items: list[dict], header: str):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.items = items
        self.header = header
        self.page = 0
        self.max_page = max(0, (len(items) - 1) // self.PAGE_SIZE)
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def render(self) -> tuple[str, list[discord.Embed]]:
        start = self.page * self.PAGE_SIZE
        page_items = self.items[start : start + self.PAGE_SIZE]
        embeds = []
        for item in page_items:
            color = gacha.RARITY_COLORS.get(item["rarity"], discord.Color.purple())
            embed = discord.Embed(title=item["name"], description=f"Rarity: **{item['rarity']}**", color=color)
            embed.set_thumbnail(url=item["icon"])
            embeds.append(embed)
        content = f"{self.header}\nPage {self.page + 1}/{self.max_page + 1}"
        return content, embeds

    async def _turn_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your collection.", ephemeral=True)
            return
        self.page = max(0, min(self.max_page, self.page + delta))
        self._update_buttons()
        content, embeds = self.render()
        await interaction.response.edit_message(content=content, embeds=embeds, view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn_page(interaction, 1)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@tree.command(name="collection", description="Browse your Valorant skin collection (or someone else's)")
@app_commands.describe(user="(optional) whose collection to view - defaults to yours")
async def collection_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    if not await _check_channel_lock(interaction, "roll"):
        return
    if interaction.guild is None:
        return

    target = user or interaction.user
    is_self = target.id == interaction.user.id

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.guild.id, target.id)
        gacha.sync_roll_charges(collection)
        storage.save_collection(interaction.guild.id, target.id, collection)

    charges_line = f"🔋 Roll charges: **{collection['charges']}/{gacha.MAX_ROLL_CHARGES}**"

    items = collection.get("items", [])
    if not items:
        empty_line = "You haven't" if is_self else f"{target.display_name} hasn't"
        await interaction.response.send_message(
            f"{empty_line} rolled any skins yet. Try `/rollskin`!\n{charges_line}", ephemeral=True
        )
        return

    counts_line = " · ".join(f"{count} {rarity}" for rarity, count in gacha.rarity_counts(items))
    possessive = "Your" if is_self else f"{target.display_name}'s"
    header = (
        f"🏆 **{possessive} Collection** — {len(items)} total\n"
        f"{counts_line}\n"
        f"{charges_line}"
    )

    sorted_items = gacha.sort_items_by_rarity(items)
    view = CollectionView(owner_id=interaction.user.id, items=sorted_items, header=header)
    content, embeds = view.render()
    if view.max_page == 0:
        view.stop()
        await interaction.response.send_message(content=content, embeds=embeds, ephemeral=True)
    else:
        await interaction.response.send_message(content=content, embeds=embeds, view=view, ephemeral=True)
        view.message = await interaction.original_response()


# Maps a user id to the TradeView they're currently tied up in (as proposer or
# responder), so we can block new trades involving someone who already has one
# pending. Cleared on accept, decline, or timeout.
active_trades: dict[int, "TradeView"] = {}

TRADE_TIMEOUT_SECONDS = 3600  # 1 hour


class TradeView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        proposer: discord.Member,
        responder: discord.Member,
        offer_item: dict,
        request_item: dict,
    ):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.proposer = proposer
        self.responder = responder
        self.offer_item = offer_item
        self.request_item = request_item
        self.message: discord.Message | None = None

    def _release(self):
        if active_trades.get(self.proposer.id) is self:
            del active_trades[self.proposer.id]
        if active_trades.get(self.responder.id) is self:
            del active_trades[self.responder.id]

    async def _notify_proposer(self, content: str):
        try:
            await self.proposer.send(content)
        except discord.Forbidden:
            pass

    async def on_timeout(self):
        self._release()
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="Trade offer expired.", view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.responder.id:
            await interaction.response.send_message("This trade offer isn't for you.", ephemeral=True)
            return

        proposer_collection = storage.get_collection(self.guild_id, self.proposer.id)
        responder_collection = storage.get_collection(self.guild_id, self.responder.id)

        given = gacha.pop_item(proposer_collection, self.offer_item["name"])
        received = gacha.pop_item(responder_collection, self.request_item["name"])

        for child in self.children:
            child.disabled = True
        self._release()

        if not given or not received:
            await interaction.response.edit_message(
                content="This trade is no longer valid (an item was already traded away).", view=self
            )
            await self._notify_proposer(
                f"⚠️ Your trade offer to {self.responder.display_name} is no longer valid "
                "(an item was already traded away)."
            )
            return

        proposer_collection["items"].append(received)
        responder_collection["items"].append(given)
        storage.save_collection(self.guild_id, self.proposer.id, proposer_collection)
        storage.save_collection(self.guild_id, self.responder.id, responder_collection)

        await interaction.response.edit_message(
            content=f"✅ Trade completed between {self.proposer.display_name} and {self.responder.display_name}!",
            view=self,
        )
        await self._notify_proposer(
            f"✅ {self.responder.display_name} accepted your trade! You received **{received['name']}** "
            f"for your **{given['name']}**."
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.responder.id:
            await interaction.response.send_message("This trade offer isn't for you.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        self._release()
        await interaction.response.edit_message(content="❌ Trade declined.", view=self)
        await self._notify_proposer(f"❌ {self.responder.display_name} declined your trade offer.")


async def _offer_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    collection = storage.get_collection(interaction.guild.id, interaction.user.id)
    names = sorted({item["name"] for item in collection.get("items", [])})
    matches = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


async def _member_collection_autocomplete(interaction: discord.Interaction, current: str):
    """Autocompletes item names from whichever member is bound to the command's `user` option."""
    target = interaction.namespace.user
    if not target or interaction.guild is None:
        return []
    collection = storage.get_collection(interaction.guild.id, target.id)
    names = sorted({item["name"] for item in collection.get("items", [])})
    matches = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


@tree.command(name="showoff", description="Publicly show off 1-3 skins from your collection")
@app_commands.describe(
    skin1="A skin from your collection",
    skin2="(optional) another skin",
    skin3="(optional) another skin",
)
@app_commands.autocomplete(skin1=_offer_autocomplete, skin2=_offer_autocomplete, skin3=_offer_autocomplete)
async def showoff_cmd(
    interaction: discord.Interaction,
    skin1: str,
    skin2: str | None = None,
    skin3: str | None = None,
):
    if not await _check_channel_lock(interaction, "roll"):
        return
    if interaction.guild is None:
        return

    collection = storage.get_collection(interaction.guild.id, interaction.user.id)

    seen: set[str] = set()
    names: list[str] = []
    for name in (skin1, skin2, skin3):
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)

    items = []
    for name in names:
        item = gacha.find_item(collection, name)
        if not item:
            await interaction.response.send_message(
                f"You don't have a skin named \"{name}\". Check `/collection`.", ephemeral=True
            )
            return
        items.append(item)

    embeds = []
    for item in items:
        color = gacha.RARITY_COLORS.get(item["rarity"], discord.Color.purple())
        embed = discord.Embed(title=item["name"], description=f"Rarity: **{item['rarity']}**", color=color)
        embed.set_thumbnail(url=item["icon"])
        embeds.append(embed)

    await interaction.response.send_message(
        content=f"✨ **{interaction.user.display_name}** is showing off their collection!", embeds=embeds
    )


def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


def _is_mod_or_admin(interaction: discord.Interaction) -> bool:
    if _is_owner(interaction):
        return True
    if interaction.guild is None:
        return False
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_messages


@tree.command(name="trade", description="Offer to trade one of your skins for one of a friend's")
@app_commands.describe(
    user="Who to trade with", offer="Your skin to give", request="Their skin you want in return"
)
@app_commands.autocomplete(offer=_offer_autocomplete, request=_member_collection_autocomplete)
async def trade_cmd(interaction: discord.Interaction, user: discord.Member, offer: str, request: str):
    if not await _check_channel_lock(interaction, "roll"):
        return
    if interaction.guild is None:
        return

    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't trade with yourself.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't trade with a bot.", ephemeral=True)
        return

    if interaction.user.id in active_trades:
        await interaction.response.send_message(
            "You already have a trade in progress. Finish or wait for it to expire first.", ephemeral=True
        )
        return
    if user.id in active_trades:
        await interaction.response.send_message(
            f"{user.display_name} already has a trade in progress. Try again once it's resolved.", ephemeral=True
        )
        return

    my_collection = storage.get_collection(interaction.guild.id, interaction.user.id)
    their_collection = storage.get_collection(interaction.guild.id, user.id)

    my_item = gacha.find_item(my_collection, offer)
    their_item = gacha.find_item(their_collection, request)

    if not my_item:
        await interaction.response.send_message(
            f"You don't have a skin named \"{offer}\". Check `/collection`.", ephemeral=True
        )
        return
    if not their_item:
        await interaction.response.send_message(
            f"{user.display_name} doesn't have a skin named \"{request}\".", ephemeral=True
        )
        return

    view = TradeView(
        guild_id=interaction.guild.id,
        proposer=interaction.user,
        responder=user,
        offer_item=my_item,
        request_item=their_item,
    )
    active_trades[interaction.user.id] = view
    active_trades[user.id] = view
    embed = discord.Embed(title="🔄 Trade Offer", color=discord.Color.blue())
    embed.add_field(name=f"{interaction.user.display_name} gives", value=f"{my_item['name']} ({my_item['rarity']})")
    embed.add_field(name=f"{user.display_name} gives", value=f"{their_item['name']} ({their_item['rarity']})")

    # DM'd instead of posted in the channel so nobody else on the server sees
    # the offer or its buttons - only the two people trading do.
    try:
        dm_message = await user.send(
            content=f"{interaction.user.display_name} sent you a trade offer!", embed=embed, view=view
        )
    except discord.Forbidden:
        view._release()
        await interaction.response.send_message(
            f"Couldn't DM {user.display_name} - they may have DMs from server members turned off. "
            "Ask them to enable it and try again.",
            ephemeral=True,
        )
        return

    view.message = dm_message
    await interaction.response.send_message(
        f"✅ Trade offer sent to {user.display_name} via DM.", ephemeral=True
    )


async def _pool_skin_autocomplete(interaction: discord.Interaction, current: str):
    if len(current) < 2:
        return []
    async with aiohttp.ClientSession() as http:
        pool = await gacha.get_pool(http)
    matches = []
    cur = current.lower()
    for items in pool.values():
        for item in items:
            if cur in item["name"].lower() and item["name"] not in matches:
                matches.append(item["name"])
            if len(matches) >= 25:
                break
        if len(matches) >= 25:
            break
    return [app_commands.Choice(name=n, value=n) for n in matches]


@tree.command(name="give", description="(Mods/Admins only) Give a user a specific skin")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Who to give the skin to", skin="The skin to give (start typing to search)")
@app_commands.autocomplete(skin=_pool_skin_autocomplete)
async def give_cmd(interaction: discord.Interaction, user: discord.Member, skin: str):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as http:
        pool = await gacha.get_pool(http)
    item = gacha.find_in_pool(pool, skin)
    if not item:
        await interaction.followup.send(f"No skin named \"{skin}\" found.", ephemeral=True)
        return

    collection = storage.get_collection(interaction.guild.id, user.id)
    collection["items"].append(gacha.stamp(item))
    storage.save_collection(interaction.guild.id, user.id, collection)
    await interaction.followup.send(
        f"🎁 Gave **{item['name']}** ({item['rarity']}) to {user.display_name}.", ephemeral=True
    )


DEFAULT_ROLL_BOOST_PERCENT = 10
MAX_ROLL_BOOST_PERCENT = 20
MAX_ROLL_BOOST_ROLLS = 100


@tree.command(
    name="nr",
    description="test",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    user="user",
    skin="skin",
    percent="odd",
    rolls="amount of rolls",
)
@app_commands.autocomplete(skin=_pool_skin_autocomplete)
async def nr_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    skin: str,
    percent: app_commands.Range[int, 1, MAX_ROLL_BOOST_PERCENT] = DEFAULT_ROLL_BOOST_PERCENT,
    rolls: app_commands.Range[int, 1, MAX_ROLL_BOOST_ROLLS] = 1,
):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as http:
        pool = await gacha.get_pool(http)
    item = gacha.find_in_pool(pool, skin)
    if not item:
        await interaction.followup.send(f"No skin named \"{skin}\" found.", ephemeral=True)
        return

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.guild.id, user.id)
        collection["roll_boost"] = {"item_id": item["id"], "percent": percent, "rolls_left": rolls}
        storage.save_collection(interaction.guild.id, user.id, collection)

    span = "next `/rollskin`" if rolls == 1 else f"next **{rolls}** rolls"
    await interaction.followup.send(
        f"🎯 {user.display_name}'s {span} will each have a **{percent}%** chance of being "
        f"**{item['name']}** ({item['rarity']}) - only applies on rolls that land in the "
        f"{item['rarity']} tier at all.",
        ephemeral=True,
    )


@tree.command(name="setnextroll", description="(Mods/Admins only) Guarantee a user's next roll is a specific skin")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Who this affects", skin="The skin their next /rollskin will give them")
@app_commands.autocomplete(skin=_pool_skin_autocomplete)
async def setnextroll_cmd(interaction: discord.Interaction, user: discord.Member, skin: str):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    await interaction.response.defer(thinking=True)
    async with aiohttp.ClientSession() as http:
        pool = await gacha.get_pool(http)
    item = gacha.find_in_pool(pool, skin)
    if not item:
        await interaction.followup.send(f"No skin named \"{skin}\" found.", ephemeral=True)
        return

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.guild.id, user.id)
        collection["forced_roll"] = item
        storage.save_collection(interaction.guild.id, user.id, collection)

    await interaction.followup.send(
        f"🎯 {user.display_name}'s next `/rollskin` is guaranteed to be **{item['name']}** ({item['rarity']})."
    )


MAX_GIVE_ROLLS_AMOUNT = 100


@tree.command(name="giverolls", description="(Mods/Admins only) Give a user extra roll charges")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Who to give charges to", amount="How many charges to add")
async def giverolls_cmd(
    interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, MAX_GIVE_ROLLS_AMOUNT]
):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.guild.id, user.id)
        gacha.sync_roll_charges(collection)
        collection["charges"] = collection.get("charges", 0) + amount
        storage.save_collection(interaction.guild.id, user.id, collection)
        new_total = collection["charges"]

    await interaction.response.send_message(
        f"🔋 Gave {user.display_name} **+{amount}** roll charge{'s' if amount != 1 else ''} "
        f"(now has **{new_total}**)."
    )


@tree.command(name="removeskin", description="(Mods/Admins only) Remove a skin from a user's collection")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Whose collection to remove from", skin="The skin to remove")
@app_commands.autocomplete(skin=_member_collection_autocomplete)
async def removeskin_cmd(interaction: discord.Interaction, user: discord.Member, skin: str):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    collection = storage.get_collection(interaction.guild.id, user.id)
    removed = gacha.pop_item(collection, skin)
    if not removed:
        await interaction.response.send_message(
            f"{user.display_name} doesn't have a skin named \"{skin}\".", ephemeral=True
        )
        return

    storage.save_collection(interaction.guild.id, user.id, collection)
    await interaction.response.send_message(
        f"🗑️ Removed **{removed['name']}** from {user.display_name}'s collection.", ephemeral=True
    )


@tree.command(name="removeallcollection", description="(Mods/Admins only) Wipe a user's entire skin collection")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Whose collection to wipe")
async def removeallcollection_cmd(interaction: discord.Interaction, user: discord.Member):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    deleted = storage.delete_collection(interaction.guild.id, user.id)
    if not deleted:
        await interaction.response.send_message(
            f"{user.display_name} doesn't have a collection to wipe.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🗑️ Wiped {user.display_name}'s entire skin collection.", ephemeral=True
    )


@tree.command(name="startcount", description="(Mods/Admins only) Set the channel for the counting game")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The channel where people will count 1, 2, 3, ...")
async def startcount_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    state = storage.get_counting_state(interaction.guild.id)
    state["channel_id"] = channel.id
    state["count"] = 0
    state["last_user_id"] = None
    storage.save_counting_state(interaction.guild.id, state)
    await interaction.response.send_message(
        f"✅ Counting game set up in {channel.mention}. Someone start with **1**!"
    )


@tree.command(name="stopcount", description="(Mods/Admins only) Turn off the counting game")
@app_commands.default_permissions(administrator=True)
async def stopcount_cmd(interaction: discord.Interaction):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    state = storage.get_counting_state(interaction.guild.id)
    if state["channel_id"] is None:
        await interaction.response.send_message("The counting game isn't set up.", ephemeral=True)
        return

    state["channel_id"] = None
    state["count"] = 0
    state["last_user_id"] = None
    storage.save_counting_state(interaction.guild.id, state)
    await interaction.response.send_message("🛑 Counting game turned off.")


@tree.command(
    name="startshop",
    description="(Mods/Admins only) Restrict /login, /store, /nightmarket, /logout to one channel",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The only channel shop commands will work in")
async def startshop_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    storage.set_channel_lock(interaction.guild.id, "shop", channel.id)
    await interaction.response.send_message(
        f"✅ Shop commands (/login, /store, /nightmarket, /logout) are now restricted to {channel.mention}."
    )


@tree.command(
    name="startroll",
    description="(Mods/Admins only) Restrict /rollskin, /collection, /trade to one channel",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The only channel roll commands will work in")
async def startroll_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    storage.set_channel_lock(interaction.guild.id, "roll", channel.id)
    await interaction.response.send_message(
        f"✅ Roll commands (/rollskin, /collection, /trade) are now restricted to {channel.mention}."
    )


@tree.command(name="stopshop", description="Remove the channel restriction on shop commands")
@app_commands.default_permissions(administrator=True)
async def stopshop_cmd(interaction: discord.Interaction):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    if interaction.guild is None:
        return

    if storage.get_channel_lock(interaction.guild.id, "shop") is None:
        await interaction.response.send_message("Shop commands aren't restricted to a channel.", ephemeral=True)
        return

    storage.set_channel_lock(interaction.guild.id, "shop", None)
    await interaction.response.send_message(
        "✅ Shop commands (/login, /store, /nightmarket, /logout) can be used anywhere again."
    )


@tree.command(name="stoproll", description="Remove the channel restriction on roll commands")
@app_commands.default_permissions(administrator=True)
async def stoproll_cmd(interaction: discord.Interaction):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    if interaction.guild is None:
        return

    if storage.get_channel_lock(interaction.guild.id, "roll") is None:
        await interaction.response.send_message("Roll commands aren't restricted to a channel.", ephemeral=True)
        return

    storage.set_channel_lock(interaction.guild.id, "roll", None)
    await interaction.response.send_message(
        "✅ Roll commands (/rollskin, /collection, /trade) can be used anywhere again."
    )


# (display text, description, permission check - None means everyone can use it)
HELP_COMMANDS = [
    ("/login", "Link your Riot account to see your daily VALORANT shop", None),
    ("/store", "Show your daily VALORANT storefront", None),
    ("/nightmarket", "Show your Night Market bonus offers, if the event is running", None),
    ("/logout", "logout your account", None),
    ("/rollskin", "Roll for a random skin or agent (2 charges, +1 at Paris midnight/noon)", None),
    ("/collection", "Browse your (or someone else's) full skin collection and charges", None),
    ("/showoff", "Publicly show off 1-3 skins from your collection", None),
    ("/trade", "Propose a skin trade (sent via DM to the other person)", None),
    ("/helpme", "Show this list", None),
    ("/startcount", "Set the channel for the counting game", _is_mod_or_admin),
    ("/stopcount", "Turn off the counting game", _is_mod_or_admin),
    ("/startshop", "Restrict /login, /store, /nightmarket, /logout to one channel", _is_mod_or_admin),
    ("/startroll", "Restrict /rollskin, /collection, /trade to one channel", _is_mod_or_admin),
    ("/stopshop", "Remove the channel restriction on shop commands", _is_mod_or_admin),
    ("/stoproll", "Remove the channel restriction on roll commands", _is_mod_or_admin),
    ("/nr", "Set a user's odds of a specific skin over their next N rolls", _is_mod_or_admin),
    ("/setnextroll", "Guarantee a user's next roll is a specific skin", _is_mod_or_admin),
    ("/giverolls", "Give a user extra roll charges", _is_mod_or_admin),
    ("/instantban", "Instantly ban a user id if/when they join", _is_mod_or_admin),
    ("/banword", "Time out anyone who types a specific word", _is_mod_or_admin),
    ("/unbanword", "Remove a word from the banned-word list", _is_mod_or_admin),
    ("/banwords", "List all banned words", _is_mod_or_admin),
    ("/checkdeleted", "See the messages and images mods have deleted from a user", _is_mod_or_admin),
    ("/setuplogs", "Post server logs (deleted/edited messages, mod actions, joins, voice) in a channel", _is_mod_or_admin),
    ("/stoplogs", "Stop posting server logs", _is_mod_or_admin),
    ("/give", "Give a user a specific skin directly", _is_mod_or_admin),
    ("/removeskin", "Remove one skin from a user's collection", _is_mod_or_admin),
    ("/removeallcollection", "Wipe a user's entire collection", _is_mod_or_admin),
]


@tree.command(name="helpme", description="List every bot command you're allowed to use")
async def helpme_cmd(interaction: discord.Interaction):
    lines = [f"**{name}** — {desc}" for name, desc, check in HELP_COMMANDS if check is None or check(interaction)]
    embed = discord.Embed(
        title="📖 Available commands",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="instantban", description="(Mods/Admins only) Instantly ban this user if/when they join")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    user="Pick a current server member (start typing their name/tag)",
    id="Or a raw Discord user id - needed to ban someone who hasn't joined yet",
)
async def instantban_cmd(
    interaction: discord.Interaction, user: discord.Member | None = None, id: str | None = None
):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    if interaction.guild is None:
        return

    if user is not None:
        user_id = user.id
    elif id is not None:
        if not id.isdigit():
            await interaction.response.send_message("That doesn't look like a valid user id.", ephemeral=True)
            return
        user_id = int(id)
    else:
        await interaction.response.send_message(
            "Pick a server member with `user`, or give a raw `id` for someone not in the server yet.",
            ephemeral=True,
        )
        return

    added = storage.add_instant_ban(interaction.guild.id, user_id)
    if not added:
        await interaction.response.send_message(f"`{user_id}` is already on the instant-ban list.", ephemeral=True)
        return

    # Ban immediately if they're already in the server, not just future joins.
    member = user or interaction.guild.get_member(user_id)
    if member is not None:
        try:
            await interaction.guild.ban(member, reason="Added to the instant-ban list")
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        f"🔨 `{user_id}` will now be instantly banned if they join (or were just banned, if already here)."
    )


DEFAULT_BANWORD_MINUTES = 5
MAX_BANWORD_MINUTES = 40320  # Discord's own cap: 28 days


@tree.command(name="banword", description="(Mods/Admins only) Time out anyone who types this word")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(word="The word (or short phrase) to ban", minutes="Timeout duration in minutes")
async def banword_cmd(
    interaction: discord.Interaction,
    word: str,
    minutes: app_commands.Range[int, 1, MAX_BANWORD_MINUTES] = DEFAULT_BANWORD_MINUTES,
):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    storage.set_banned_word(interaction.guild.id, word, minutes)
    await interaction.response.send_message(
        f"🚫 Anyone who types \"{word}\" now gets timed out for {minutes} minute{'s' if minutes != 1 else ''}."
    )


async def _banned_word_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    words = sorted(storage.get_banned_words(interaction.guild.id).keys())
    matches = [w for w in words if current.lower() in w.lower()][:25]
    return [app_commands.Choice(name=w, value=w) for w in matches]


@tree.command(name="unbanword", description="(Mods/Admins only) Remove a word from the banned-word list")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(word="The banned word to remove")
@app_commands.autocomplete(word=_banned_word_autocomplete)
async def unbanword_cmd(interaction: discord.Interaction, word: str):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    removed = storage.remove_banned_word(interaction.guild.id, word)
    if not removed:
        await interaction.response.send_message(f"\"{word}\" isn't on the banned-word list.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ \"{word}\" is no longer banned.")


@tree.command(name="banwords", description="(Mods/Admins only) List all banned words")
@app_commands.default_permissions(administrator=True)
async def banwords_cmd(interaction: discord.Interaction):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    banned = storage.get_banned_words(interaction.guild.id)
    if not banned:
        await interaction.response.send_message("No words are currently banned.", ephemeral=True)
        return

    lines = [
        f"**{word}** — {minutes} minute{'s' if minutes != 1 else ''}"
        for word, minutes in sorted(banned.items())
    ]
    await interaction.response.send_message("🚫 **Banned words:**\n" + "\n".join(lines), ephemeral=True)


async def _check_banned_words(message: discord.Message) -> bool:
    """Times out the author if their message contains a banned word. Returns
    True if a timeout was applied (whole-word/phrase match, case-insensitive)."""
    banned = storage.get_banned_words(message.guild.id)
    if not banned:
        return False

    for word, minutes in banned.items():
        if re.search(rf"\b{re.escape(word)}\b", message.content, re.IGNORECASE):
            try:
                await message.author.timeout(timedelta(minutes=minutes), reason=f"Used banned word: {word}")
            except discord.HTTPException as exc:
                print(f"banword timeout failed for {message.author.id} in guild {message.guild.id}: {exc!r}")
                return False
            try:
                await message.channel.send(
                    f"🔇 {message.author.mention} got timed out for {minutes} minute"
                    f"{'s' if minutes != 1 else ''} (banned word)."
                )
            except discord.HTTPException:
                pass
            return True
    return False


# Discord's delete events don't say who deleted a message, so we cross-reference
# the audit log (self-deletes never show up there). Repeat deletions of the same
# user's messages in the same channel by the same mod get merged into one audit
# entry whose `count` goes up, so we track how many of each entry's deletions
# we've already matched to a message.
_audit_consumed: dict[int, int] = {}
_audit_lock = asyncio.Lock()
# Audit entries can land a few seconds after the gateway event, so look a few times.
AUDIT_LOG_RETRY_DELAYS = (1.5, 2, 4)
AUDIT_FRESH_SECONDS = 20
MAX_SAVED_ATTACHMENT_BYTES = 25 * 1024 * 1024
CHECKDELETED_PAGE_SIZE = 5
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


async def _read_attachments(message: discord.Message) -> list[tuple[discord.Attachment, bytes | None]]:
    """Grabs attachment bytes right away - Discord pulls a deleted message's
    files off its CDN shortly after deletion, so waiting isn't an option."""
    result = []
    for attachment in message.attachments:
        data = None
        if attachment.size <= MAX_SAVED_ATTACHMENT_BYTES:
            for use_cached in (True, False):
                try:
                    data = await attachment.read(use_cached=use_cached)
                    break
                except discord.HTTPException:
                    pass
        result.append((attachment, data))
    return result


def _upload_name(message: discord.Message, index: int, filename: str) -> str:
    """A safe, unique file name for re-uploading/saving an attachment."""
    ext = os.path.splitext(filename)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ""
    return f"{message.id}_{index}{ext}"


def _deletion_entry(message: discord.Message, deleted_by: int | None, self_deleted: bool) -> dict:
    return {
        "message_id": message.id,
        "author_id": message.author.id,
        "channel_id": message.channel.id,
        "content": message.content,
        "sent_at": message.created_at.isoformat(),
        "deleted_at": discord.utils.utcnow().isoformat(),
        "deleted_by": deleted_by,
        "self_deleted": self_deleted,
    }


def _log_deleted_message(
    message: discord.Message,
    deleted_by: int | None,
    attachments: list[tuple[discord.Attachment, bytes | None]],
) -> None:
    saved = []
    for i, (attachment, data) in enumerate(attachments):
        file = None
        if data is not None:
            file = _upload_name(message, i, attachment.filename)
            storage.save_deleted_media(message.guild.id, file, data)
        saved.append({"filename": attachment.filename, "file": file})

    entry = _deletion_entry(message, deleted_by, self_deleted=False)
    entry["attachments"] = saved
    storage.add_deleted_message(message.guild.id, message.author.id, entry)


# Audit actions whose entries get merged (with a rising `count`) when a mod repeats them.
_COUNTED_AUDIT_ACTIONS = (discord.AuditLogAction.message_delete, discord.AuditLogAction.member_move)


async def _prime_audit_counts() -> None:
    """Marks every existing message-delete/member-move audit entry as fully accounted
    for, so actions that happened while the bot was offline aren't matched later."""
    async with _audit_lock:
        for guild in client.guilds:
            if not guild.me.guild_permissions.view_audit_log:
                print(f"No View Audit Log permission in {guild.name} - can't tell who deletes messages there")
                continue
            try:
                for action in _COUNTED_AUDIT_ACTIONS:
                    async for entry in guild.audit_logs(limit=100, action=action):
                        _audit_consumed[entry.id] = entry.extra.count
            except discord.HTTPException as exc:
                print(f"couldn't read audit log for guild {guild.id}: {exc!r}")


async def _find_audit_actor(guild: discord.Guild, action: discord.AuditLogAction, matches) -> tuple[int | None, bool]:
    """(id of whoever did the not-yet-accounted-for `action` whose entry passes
    `matches`, whether the audit log could be read). A None id with a readable
    audit log means nobody else did it - e.g. a self-delete or a self-switch."""
    for delay in AUDIT_LOG_RETRY_DELAYS:
        await asyncio.sleep(delay)
        async with _audit_lock:
            now = discord.utils.utcnow()
            try:
                async for entry in guild.audit_logs(limit=10, action=action):
                    if not matches(entry):
                        continue
                    count = entry.extra.count
                    consumed = _audit_consumed.get(entry.id)
                    if consumed is None:
                        # Unseen entry: brand new means this deletion created it; an old
                        # one we somehow missed is treated as already accounted for.
                        consumed = 0 if (now - entry.created_at).total_seconds() < AUDIT_FRESH_SECONDS else count
                    if consumed < count:
                        _audit_consumed[entry.id] = consumed + 1
                        return entry.user_id, True
                    _audit_consumed[entry.id] = consumed
            except discord.HTTPException as exc:
                print(f"audit log lookup failed in guild {guild.id}: {exc!r}")
                return None, False
    return None, True


async def _find_message_deleter(guild: discord.Guild, author_id: int, channel_id: int) -> tuple[int | None, bool]:
    """(id of the mod who deleted this user's message, whether the audit log could be read)."""
    return await _find_audit_actor(
        guild,
        discord.AuditLogAction.message_delete,
        lambda e: e.target is not None and e.target.id == author_id and e.extra.channel.id == channel_id,
    )


async def _find_voice_mover(guild: discord.Guild, destination_id: int) -> tuple[int | None, bool]:
    """(id of the mod who dragged someone into this voice channel, whether the audit
    log could be read). Move entries don't say which member was moved, so the
    match is on destination channel and timing."""
    return await _find_audit_actor(
        guild, discord.AuditLogAction.member_move, lambda e: e.extra.channel.id == destination_id
    )


def _log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if storage.logs_paused(guild.id):
        return None
    channel_id = storage.get_log_channel(guild.id)
    return guild.get_channel(channel_id) if channel_id else None


def _deletion_log_card(
    guild: discord.Guild,
    message: discord.Message,
    entry: dict,
    attachments: list[tuple[discord.Attachment, bytes | None]],
) -> tuple[discord.ui.Container, list[discord.File]]:
    sources = [
        (a.filename, _upload_name(message, i, a.filename) if data is not None else None, data)
        for i, (a, data) in enumerate(attachments)
    ]
    card, files, _, _ = _deleted_entry_card(
        guild, entry, sources, 10, guild.filesize_limit, 40, title="-# 🗑️ MESSAGE DELETED", max_content=3000
    )
    return card, files


async def _post_deletion_log(
    channel: discord.TextChannel,
    message: discord.Message,
    entry: dict,
    attachments: list[tuple[discord.Attachment, bytes | None]],
) -> discord.Message | None:
    card, files = _deletion_log_card(channel.guild, message, entry, attachments)
    return await _send_log(channel, card, files)


async def _send_log(
    channel: discord.TextChannel, card: discord.ui.Container, files: list[discord.File] = ()
) -> discord.Message | None:
    view = discord.ui.LayoutView()
    view.add_item(card)
    try:
        return await channel.send(view=view, files=list(files), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        print(f"couldn't post to the log channel in guild {channel.guild.id}: {exc!r}")
        return None


MAX_RESTORED_LOGS = 5


def _is_log_message(message: discord.Message) -> bool:
    """One of the bot's own posts in the log channel."""
    if client.user is None or message.author.id != client.user.id or message.guild is None:
        return False
    log_channel = _log_channel(message.guild)
    return log_channel is not None and message.channel.id == log_channel.id


def _message_text(message: discord.Message) -> str:
    """All the text in a message, including inside a log card's layout."""
    parts = [message.content] if message.content else []

    def walk(components):
        for component in components:
            content = getattr(component, "content", None)
            if isinstance(content, str):
                parts.append(content)
            walk(getattr(component, "children", None) or [])

    walk(message.components)
    return "\n".join(parts)


async def _alert_logs_deleted(
    log_channel: discord.TextChannel, deleted_by: int | None, restored: list[str], count: int
) -> None:
    """Posts a new card when someone deletes the bot's logs, with their text
    restored, so evidence can't just be wiped. (Images from them are gone.)"""
    who = f"<@{deleted_by}>" if deleted_by else "Someone"
    what = "a log entry" if count == 1 else f"{count} log entries"
    card = discord.ui.Container(accent_colour=discord.Colour.red())
    card.add_item(discord.ui.TextDisplay(f"-# 🚨 LOG DELETED\n## {who} deleted {what}"))

    budget = 3000 // max(len(restored), 1)
    for text in restored:
        if len(text) > budget:
            text = text[:budget] + "…"
        card.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        card.add_item(discord.ui.TextDisplay("\n".join(f"> {line}" for line in text.split("\n"))))
    if not restored:
        card.add_item(discord.ui.TextDisplay("-# Content unavailable - it was posted before the bot last started."))
    elif count > len(restored):
        card.add_item(discord.ui.TextDisplay(f"-# …and {count - len(restored)} more not restored."))

    card.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
    card.add_item(discord.ui.TextDisplay(
        f"**Deleted by:** {f'<@{deleted_by}>' if deleted_by else 'unknown'}\n**Date:** {_now_tag()}"
    ))
    await _send_log(log_channel, card)
    print(f"log entries deleted in guild {log_channel.guild.id} by {deleted_by or 'unknown'} - alert posted")


@client.event
async def on_message_delete(message: discord.Message):
    if message.guild is None:
        return
    if message.author.bot:
        if _is_log_message(message):
            deleted_by, audit_ok = await _find_message_deleter(message.guild, client.user.id, message.channel.id)
            # No audit entry with a readable log means the bot removed its own message.
            if deleted_by is not None or not audit_ok:
                await _alert_logs_deleted(message.channel, deleted_by, [_message_text(message)], 1)
        return
    attachments = await _read_attachments(message)

    # Post right away with "checking…" - finding out who deleted it means waiting
    # on the audit log (several seconds for self-deletes, which never show up
    # there) - then fill in the "Deleted by" line once we know.
    log_channel = _log_channel(message.guild)
    posted = None
    if log_channel is not None:
        pending = _deletion_entry(message, None, self_deleted=False)
        pending["pending"] = True
        posted = await _post_deletion_log(log_channel, message, pending, attachments)

    deleted_by, audit_ok = await _find_message_deleter(message.guild, message.author.id, message.channel.id)
    if deleted_by is not None:
        _log_deleted_message(message, deleted_by, attachments)

    who = f"mod {deleted_by}" if deleted_by else ("themselves" if audit_ok else "unknown (audit log unreadable)")
    if log_channel is None:
        print(f"message {message.id} by {message.author} deleted by {who} - no log channel set, run /setuplogs")
        return
    if posted is not None:
        # Rebuild from the posted message (its images already point at the uploaded
        # files) and only swap the placeholder.
        resolved = f"<@{deleted_by}>" if deleted_by else ("themselves" if audit_ok else "unknown")
        view = discord.ui.LayoutView.from_message(posted)
        for item in view.walk_children():
            if isinstance(item, discord.ui.TextDisplay) and "⏳ checking…" in item.content:
                item.content = item.content.replace("⏳ checking…", resolved)
        try:
            await posted.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print(f"couldn't update the deletion log in guild {message.guild.id}: {exc!r}")
    print(f"message {message.id} by {message.author} deleted by {who} - posted in #{log_channel.name}")


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # Deletions of messages the bot never saw (sent before it last started) -
    # all we know is where it was, not what it said or who wrote it.
    if payload.guild_id is None or payload.cached_message is not None:
        return
    guild = client.get_guild(payload.guild_id)
    log_channel = _log_channel(guild) if guild else None
    print(f"message {payload.message_id} deleted but wasn't cached (sent before the bot started?)")
    if log_channel is None:
        return
    if payload.channel_id == log_channel.id:
        # Only an audit entry targeting the bot tells us this was one of its logs.
        deleted_by, _ = await _find_message_deleter(guild, client.user.id, log_channel.id)
        if deleted_by is not None:
            await _alert_logs_deleted(log_channel, deleted_by, [], 1)
        return
    card = discord.ui.Container(accent_colour=discord.Colour.dark_grey())
    card.add_item(discord.ui.TextDisplay(
        "-# 🗑️ MESSAGE DELETED\n"
        "*Content unavailable - it was sent before the bot last started.*"
    ))
    card.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
    card.add_item(discord.ui.TextDisplay(
        f"**Sent in channel:** <#{payload.channel_id}>\n"
        f"**Date:** <t:{int(discord.utils.utcnow().timestamp())}:f>"
    ))
    view = discord.ui.LayoutView()
    view.add_item(card)
    try:
        await log_channel.send(view=view)
    except discord.HTTPException as exc:
        print(f"couldn't post to the log channel in guild {guild.id}: {exc!r}")


MAX_BULK_LOG_POSTS = 25


@client.event
async def on_bulk_message_delete(messages: list[discord.Message]):
    # Bulk deletes (purge commands) always come from a mod or bot, never the author.
    if not messages or messages[0].guild is None:
        return
    guild, channel = messages[0].guild, messages[0].channel
    purged_logs = [m for m in messages if _is_log_message(m)]
    messages = [m for m in messages if not m.author.bot]
    if not messages and not purged_logs:
        return
    attachments = {m.id: await _read_attachments(m) for m in messages}

    await asyncio.sleep(AUDIT_LOG_RETRY_DELAYS[0])
    deleted_by = None
    now = discord.utils.utcnow()
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.message_bulk_delete):
            is_fresh = (now - entry.created_at).total_seconds() < AUDIT_FRESH_SECONDS
            if entry.target is not None and entry.target.id == channel.id and is_fresh:
                deleted_by = entry.user_id
                break
    except discord.HTTPException as exc:
        print(f"bulk-delete audit log lookup failed in guild {guild.id}: {exc!r}")

    for m in messages:
        _log_deleted_message(m, deleted_by, attachments[m.id])

    log_channel = _log_channel(guild)
    if log_channel is None:
        return
    if purged_logs:
        await _alert_logs_deleted(
            log_channel, deleted_by, [_message_text(m) for m in purged_logs[:MAX_RESTORED_LOGS]], len(purged_logs)
        )
    for m in messages[:MAX_BULK_LOG_POSTS]:
        await _post_deletion_log(log_channel, m, _deletion_entry(m, deleted_by, self_deleted=False), attachments[m.id])
    if len(messages) > MAX_BULK_LOG_POSTS:
        try:
            await log_channel.send(
                f"…and {len(messages) - MAX_BULK_LOG_POSTS} more messages purged in {channel.mention}. "
                "Use `/checkdeleted` on a user to see all of theirs."
            )
        except discord.HTTPException:
            pass


def _stored_attachments(guild: discord.Guild, entry: dict) -> list[tuple[str, str | None, str | None]]:
    """A saved entry's attachments as (filename, upload name, file path) sources."""
    sources = []
    for attachment in entry["attachments"]:
        path = storage.deleted_media_path(guild.id, attachment["file"]) if attachment["file"] else None
        if path is not None and os.path.exists(path):
            sources.append((attachment["filename"], attachment["file"], path))
        else:
            sources.append((attachment["filename"], None, None))
    return sources


EMPHASIZE_MAX_LINES = 15


def _emphasize(content: str, max_length: int) -> str:
    """Makes the deleted text the first thing you see: big heading text when
    it's short, a bold quote block when it's longer."""
    if not content.strip():
        return "*(no text)*"
    if len(content) > max_length:
        content = content[:max_length] + "…"
    lines = [line.strip() for line in content.split("\n")]
    if len(lines) > EMPHASIZE_MAX_LINES:
        lines = lines[:EMPHASIZE_MAX_LINES] + ["…"]
    if len(content) <= 200 and len(lines) <= 3:
        return "\n".join(f"## {line}" if line else "" for line in lines)
    return "\n".join(f"> **{line}**" if line else ">" for line in lines)


def _deleted_entry_card(
    guild: discord.Guild,
    entry: dict,
    attachments: list[tuple[str, str | None, str | bytes | None]],
    room_for_files: int,
    byte_budget: int,
    component_budget: int,
    title: str | None = None,
    max_content: int = 350,  # 5 cards per /checkdeleted page must fit Discord's 4000-character limit
) -> tuple[discord.ui.Container, list[discord.File], int, int]:
    """One deletion as a card: the message, its images under it, a gap, then
    who/where/when. `attachments` are (filename, upload name, file path or raw
    bytes - None if it couldn't be saved). Attachments that don't fit the
    remaining budgets are listed by name instead. Returns (card, files, file
    bytes, component count)."""
    components = 4  # card, message text, spacer, details text
    files, total_bytes, image_urls, other_urls, notes = [], 0, [], [], []
    for filename, upload_name, source in attachments:
        name = filename[:40]
        if source is None:
            notes.append(f"-# 📎 {name} (couldn't be saved)")
            continue
        size = len(source) if isinstance(source, bytes) else os.path.getsize(source)
        if size > guild.filesize_limit:
            notes.append(f"-# 📎 {name} (too big to re-upload)")
            continue
        is_image = os.path.splitext(upload_name)[1] in IMAGE_EXTENSIONS
        extra_components = 0 if is_image and image_urls else 1  # images share one gallery
        if (
            len(files) >= room_for_files
            or total_bytes + size > byte_budget
            or components + extra_components > component_budget
        ):
            notes.append(f"-# 📎 {name} (didn't fit on this page)")
            continue
        files.append(discord.File(io.BytesIO(source) if isinstance(source, bytes) else source, filename=upload_name))
        total_bytes += size
        components += extra_components
        (image_urls if is_image else other_urls).append(f"attachment://{upload_name}")
    if len(notes) > 3:
        notes = [f"-# 📎 {len(notes)} attachments couldn't be shown"]

    content = _emphasize(entry["content"], max_content)
    deleted_at = int(datetime.fromisoformat(entry["deleted_at"]).timestamp())
    if entry.get("pending"):
        deleted_by = "⏳ checking…"
    elif entry["deleted_by"]:
        deleted_by = f"<@{entry['deleted_by']}>"
    elif entry.get("self_deleted"):
        deleted_by = "themselves"
    else:
        deleted_by = "unknown"

    details = []
    if title is not None and entry.get("author_id"):
        details.append(f"**Author:** <@{entry['author_id']}>")
    details += [
        f"**Sent in channel:** <#{entry['channel_id']}>",
        f"**Deleted by:** {deleted_by}",
        f"**Date:** <t:{deleted_at}:f>",
    ]

    card = discord.ui.Container(accent_colour=discord.Colour.red())
    card.add_item(discord.ui.TextDisplay("\n".join([*([title] if title else []), content, *notes])))
    if image_urls:
        gallery = discord.ui.MediaGallery()
        for url in image_urls:
            gallery.add_item(media=url)
        card.add_item(gallery)
    for url in other_urls:
        card.add_item(discord.ui.File(url))
    card.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
    card.add_item(discord.ui.TextDisplay("\n".join(details)))
    return card, files, total_bytes, components


@tree.command(name="setuplogs", description="(Mods/Admins only) Post server logs in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The channel logs get posted in")
async def setuplogs_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.attach_files):
        await interaction.response.send_message(
            f"I need **View Channel**, **Send Messages** and **Attach Files** in {channel.mention} first.",
            ephemeral=True,
        )
        return

    storage.set_log_channel(interaction.guild.id, channel.id)
    note = ""
    if not interaction.guild.me.guild_permissions.view_audit_log:
        note = "\n⚠️ I don't have **View Audit Log**, so I can't tell who deleted messages or log bans/kicks/timeouts until you give it to me."
    await interaction.response.send_message(
        f"📋 Logs will now be posted in {channel.mention}: deleted & edited messages, bans, kicks, "
        f"timeouts, joins/leaves and voice activity.{note}", ephemeral=True
    )


@tree.command(name="stoplogs", description="(Mods/Admins only) Stop posting server logs")
@app_commands.default_permissions(administrator=True)
async def stoplogs_cmd(interaction: discord.Interaction):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    storage.set_log_channel(interaction.guild.id, None)
    await interaction.response.send_message("✅ Server logs are no longer posted anywhere.", ephemeral=True)


@tree.command(name="pauselogs", description="Pause or resume server logs")
@app_commands.default_permissions(administrator=True)
async def pauselogs_cmd(interaction: discord.Interaction):
    # Bot owner only, and left out of /helpme. Discord can't hide a command from
    # everyone but one user, so default_permissions keeps it out of non-admins'
    # menus; restrict it to just you in Server Settings > Integrations.
    if interaction.guild is None or interaction.user.id != client.application.owner.id:
        await interaction.response.send_message("You can't use this command.", ephemeral=True)
        return

    paused = not storage.logs_paused(interaction.guild.id)
    storage.set_logs_paused(interaction.guild.id, paused)
    if paused:
        msg = "⏸️ Server logs paused. Run `/pauselogs` again to resume."
    elif storage.get_log_channel(interaction.guild.id) is None:
        msg = "▶️ Logs unpaused, but no log channel is set (was `/stoplogs` used?). Run `/setuplogs` to pick one."
    else:
        msg = "▶️ Server logs resumed."
    await interaction.response.send_message(msg, ephemeral=True)


class DeletedLogView(discord.ui.LayoutView):
    """Public, paginated /checkdeleted result. Anyone can flip pages, with a
    shared cooldown so people can't flip it out from under each other."""

    MAX_COMPONENTS = 40  # Discord's per-message limit, nested components included
    MAX_FILES = 10
    PAGE_COOLDOWN_SECONDS = 5

    def __init__(self, guild: discord.Guild, user: discord.User, logs: list[dict]):
        super().__init__(timeout=600)
        self.guild = guild
        self.user = user
        self.entries = logs[::-1]  # newest first
        self.last_flip: datetime | None = None
        self.page = 0
        self.page_count = (len(self.entries) + CHECKDELETED_PAGE_SIZE - 1) // CHECKDELETED_PAGE_SIZE
        self.message: discord.Message | None = None

    def build_page(self) -> list[discord.File]:
        """Rebuilds the view for the current page and returns the files it needs."""
        self.clear_items()
        count = len(self.entries)
        header = f"### 🗑️ {count} deleted message{'s' if count != 1 else ''} from {self.user.mention}"
        if self.page_count > 1:
            header += f"\n-# Page {self.page + 1}/{self.page_count}"
        self.add_item(discord.ui.TextDisplay(header))

        component_budget = self.MAX_COMPONENTS - 1 - (3 if self.page_count > 1 else 0)  # header, button row
        page_files: list[discord.File] = []
        page_bytes = 0
        start = self.page * CHECKDELETED_PAGE_SIZE
        page_entries = self.entries[start:start + CHECKDELETED_PAGE_SIZE]
        for i, entry in enumerate(page_entries):
            # Keep 4 components in reserve for each card still to come on this page.
            reserved = 4 * (len(page_entries) - i - 1)
            card, files, size, components = _deleted_entry_card(
                self.guild,
                entry,
                _stored_attachments(self.guild, entry),
                self.MAX_FILES - len(page_files),
                self.guild.filesize_limit - page_bytes,
                component_budget - reserved,
            )
            self.add_item(card)
            page_files.extend(files)
            page_bytes += size
            component_budget -= components

        if self.page_count > 1:
            prev_button = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            next_button = discord.ui.Button(
                emoji="▶️", style=discord.ButtonStyle.secondary, disabled=self.page >= self.page_count - 1
            )
            prev_button.callback = self._previous_page
            next_button.callback = self._next_page
            self.add_item(discord.ui.ActionRow(prev_button, next_button))
        return page_files

    async def _show_page(self, interaction: discord.Interaction, page: int):
        now = discord.utils.utcnow()
        if self.last_flip is not None:
            remaining = self.PAGE_COOLDOWN_SECONDS - (now - self.last_flip).total_seconds()
            if remaining > 0:
                await interaction.response.send_message(
                    f"⏳ Slow down - you can flip the page again in {math.ceil(remaining)}s.", ephemeral=True
                )
                return
        self.last_flip = now

        self.page = page
        files = self.build_page()
        await interaction.response.edit_message(
            view=self, attachments=files, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _previous_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, max(self.page - 1, 0))

    async def _next_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, min(self.page + 1, self.page_count - 1))

    async def on_timeout(self):
        if self.message is None:
            return
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


@tree.command(name="checkdeleted", description="(Mods/Admins only) See the messages mods have deleted from a user")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="The user whose deleted messages you want to see")
async def checkdeleted_cmd(interaction: discord.Interaction, user: discord.User):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return
    if interaction.guild is None:
        return

    logs = storage.get_deleted_messages(interaction.guild.id, user.id)
    if not logs:
        await interaction.response.send_message(f"No mod-deleted messages logged for {user.mention}.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    view = DeletedLogView(interaction.guild, user, logs)
    files = view.build_page()
    view.message = await interaction.followup.send(
        view=view, files=files, allowed_mentions=discord.AllowedMentions.none(), wait=True
    )


def _log_card(colour: discord.Colour, label: str, headline: str, details: list[str],
              thumbnail_url: str | None = None) -> discord.ui.Container:
    """A log-channel card: small grey label, big headline, a gap, then details."""
    card = discord.ui.Container(accent_colour=colour)
    top = discord.ui.TextDisplay(f"-# {label}\n{headline}")
    if thumbnail_url:
        card.add_item(discord.ui.Section(top, accessory=discord.ui.Thumbnail(thumbnail_url)))
    else:
        card.add_item(top)
    if details:
        card.add_item(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.large))
        card.add_item(discord.ui.TextDisplay("\n".join(details)))
    return card


def _user_ref(user: discord.abc.Snowflake) -> str:
    """A mention plus the plain name/id - mentions of people who left or got
    banned often render as "unknown user"."""
    name = getattr(user, "name", None)
    return f"<@{user.id}> (`{name or user.id}`)"


def _now_tag() -> str:
    return f"<t:{int(discord.utils.utcnow().timestamp())}:f>"


@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    # The raw event fires for every edit, not just messages still in the bot's
    # cache - for uncached ones we just don't know the old text.
    after, before = payload.message, payload.cached_message
    if after.guild is None or after.author.bot:
        return
    # Discord also sends updates when a link preview loads - only real text edits count.
    if before is not None:
        if before.content == after.content:
            return
        before_text = _emphasize(before.content, 1500)
    else:
        if after.edited_at is None or (discord.utils.utcnow() - after.edited_at).total_seconds() > 60:
            return
        before_text = "*Unknown - the message was sent before the bot last started.*"

    log_channel = _log_channel(after.guild)
    if log_channel is None:
        print(f"message {after.id} by {after.author} edited - no log channel set, run /setuplogs")
        return
    print(f"message {after.id} by {after.author} edited - posted in #{log_channel.name}")
    card = _log_card(
        discord.Colour.gold(),
        "✏️ MESSAGE EDITED",
        f"**Before:**\n{before_text}\n**After:**\n{_emphasize(after.content, 1500)}",
        [
            f"**Author:** {_user_ref(after.author)}",
            f"**Channel:** <#{after.channel.id}> · [Jump to message]({after.jump_url})",
            f"**Date:** {_now_tag()}",
        ],
    )
    await _send_log(log_channel, card)


# Moderation actions come straight from the audit log, so they're caught no matter
# who (or which bot, including this one) did them.
_MOD_ACTIONS = {
    discord.AuditLogAction.ban: (discord.Colour.dark_red(), "🔨 MEMBER BANNED", "was banned"),
    discord.AuditLogAction.unban: (discord.Colour.green(), "🔓 MEMBER UNBANNED", "was unbanned"),
    discord.AuditLogAction.kick: (discord.Colour.orange(), "👢 MEMBER KICKED", "was kicked"),
}
_NO_CHANGE = object()


@client.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    log_channel = _log_channel(entry.guild)
    if log_channel is None or entry.target is None:
        return

    extra = []
    if entry.action in _MOD_ACTIONS:
        colour, label, verb = _MOD_ACTIONS[entry.action]
    elif entry.action == discord.AuditLogAction.member_update:
        timed_out_until = getattr(entry.after, "timed_out_until", _NO_CHANGE)
        if timed_out_until is _NO_CHANGE:
            return  # some other member change (nickname, etc.)
        if timed_out_until is None:
            colour, label, verb = discord.Colour.green(), "🔊 TIMEOUT REMOVED", "is no longer timed out"
        else:
            colour, label, verb = discord.Colour.dark_orange(), "🔇 MEMBER TIMED OUT", "was timed out"
            until = int(timed_out_until.timestamp())
            extra.append(f"**Until:** <t:{until}:f> (<t:{until}:R>)")
    else:
        return

    card = _log_card(colour, label, f"## <@{entry.target.id}> {verb}", [
        f"**Member:** {_user_ref(entry.target)}",
        f"**By:** <@{entry.user_id}>" if entry.user_id else "**By:** unknown",
        f"**Reason:** {entry.reason or '*none given*'}",
        *extra,
        f"**Date:** {_now_tag()}",
    ])
    await _send_log(log_channel, card)


NEW_ACCOUNT_DAYS = 7


async def _log_member_join(member: discord.Member) -> None:
    log_channel = _log_channel(member.guild)
    if log_channel is None:
        return
    created = int(member.created_at.timestamp())
    details = [f"**Account created:** <t:{created}:f> (<t:{created}:R>)"]
    if (discord.utils.utcnow() - member.created_at).days < NEW_ACCOUNT_DAYS:
        details.append("⚠️ **Brand-new account** - possible alt")
    details += [f"**Member count:** {member.guild.member_count}", f"**Date:** {_now_tag()}"]
    card = _log_card(
        discord.Colour.green(), "📥 MEMBER JOINED", f"## {member.mention}\n`{member.name}` · `{member.id}`",
        details, thumbnail_url=member.display_avatar.url,
    )
    await _send_log(log_channel, card)


@client.event
async def on_member_remove(member: discord.Member):
    # Also fires for kicks and bans - those get their own card from the audit log too.
    log_channel = _log_channel(member.guild)
    if log_channel is None:
        return
    details = []
    if member.joined_at:
        details.append(f"**Joined:** <t:{int(member.joined_at.timestamp())}:R>")
    roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
    if roles:
        details.append(f"**Roles:** {' '.join(roles)[:900]}")
    details += [f"**Member count:** {member.guild.member_count}", f"**Date:** {_now_tag()}"]
    card = _log_card(
        discord.Colour.dark_grey(), "📤 MEMBER LEFT", f"## {member.mention}\n`{member.name}` · `{member.id}`",
        details, thumbnail_url=member.display_avatar.url,
    )
    await _send_log(log_channel, card)


# When each member entered their current voice channel, keyed by (guild id, member id).
# In memory only - people already in voice when the bot starts have no entry.
_voice_joined_at: dict[tuple[int, int], datetime] = {}


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Only channel changes - mutes, deafens and streams are too noisy to log.
    if before.channel == after.channel:
        return
    key = (member.guild.id, member.id)
    joined_at = _voice_joined_at.pop(key, None)
    if after.channel is not None:
        _voice_joined_at[key] = discord.utils.utcnow()
    stayed = ""
    if joined_at is not None:
        stayed = f" after {_format_duration((discord.utils.utcnow() - joined_at).total_seconds())}"

    log_channel = _log_channel(member.guild)
    if log_channel is None:
        return
    # Voice is frequent, so joins/leaves are a compact one-line card.
    if before.channel is None:
        card = _log_card(
            discord.Colour.blurple(), "🔊 VOICE JOIN",
            f"{_user_ref(member)} joined voice channel <#{after.channel.id}> · {_now_tag()}", [],
        )
    elif after.channel is None:
        card = _log_card(
            discord.Colour.dark_grey(), "🔇 VOICE LEAVE",
            f"{_user_ref(member)} left voice channel <#{before.channel.id}>{stayed} · {_now_tag()}", [],
        )
    else:
        mover, audit_ok = await _find_voice_mover(member.guild, after.channel.id)
        if mover is not None and mover != member.id:
            headline = f"{_user_ref(member)} was moved to another voice channel"
            moved_by = f"<@{mover}>"
        else:
            headline = f"{_user_ref(member)} switched voice channels"
            moved_by = "themselves" if audit_ok else "unknown"
        card = _log_card(
            discord.Colour.blurple(), "🔀 VOICE CHANNEL SWITCH", headline,
            [
                f"**From:** <#{before.channel.id}>",
                f"**To:** <#{after.channel.id}>",
                f"**Moved by:** {moved_by}",
                f"**Date:** {_now_tag()}",
            ],
        )
    await _send_log(log_channel, card)


@client.event
async def on_member_join(member: discord.Member):
    if member.id in storage.get_instant_ban_list(member.guild.id):
        try:
            await member.ban(reason="On the instant-ban list")
        except discord.HTTPException as exc:
            print(f"instantban failed for {member.id} in guild {member.guild.id}: {exc!r}")
        else:
            if member.guild.system_channel:
                try:
                    await member.guild.system_channel.send(f"🔨 `{member.id}` joined and was instantly banned.")
                except discord.HTTPException:
                    pass

    await _log_member_join(member)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if await _check_banned_words(message):
        return

    state = storage.get_counting_state(message.guild.id)
    if state["channel_id"] != message.channel.id:
        return

    text = message.content.strip()
    expected = state["count"] + 1
    is_next_number = text.isdigit() and int(text) == expected
    is_repeat_poster = state["count"] > 0 and message.author.id == state["last_user_id"]

    if is_next_number and not is_repeat_poster:
        state["count"] = expected
        state["last_user_id"] = message.author.id
        state["best_count"] = max(state["best_count"], expected)
        storage.save_counting_state(message.guild.id, state)
        return

    reached = state["count"]
    state["count"] = 0
    state["last_user_id"] = None
    storage.save_counting_state(message.guild.id, state)

    try:
        await message.add_reaction("💀")
    except discord.HTTPException:
        pass
    await message.channel.send(
        counting.random_roast(message.author.mention, reached, is_double_post=is_repeat_poster)
    )


@client.event
async def on_ready():
    await tree.sync()  # global sync (can take up to ~1hr to propagate)
    for guild in client.guilds:  # instant sync to every server the bot is already in
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"Synced commands to guild: {guild.name} (id: {guild.id})")
    await _prime_audit_counts()
    print(f"Logged in as {client.user} (id: {client.user.id})")


if __name__ == "__main__":
    client.run(TOKEN)
