"""Minimal Discord bot: /login, /store, /logout for a personal VALORANT shop viewer,
plus /rollskin, /collection, /trade for a daily skin-collecting side game,
plus a counting game in a designated channel."""

import os

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
client = discord.Client(intents=intents)
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
            item = gacha.roll(pool, boost=roll_boost)
        except Exception as exc:
            print(f"roll error for user {interaction.user.id}: {exc!r}")
            async with storage.collection_lock:
                collection = storage.get_collection(guild_id, interaction.user.id)
                collection["charges"] = min(gacha.MAX_ROLL_CHARGES, collection.get("charges", 0) + 1)
                # Restore the boost to its pre-decrement state too, since this
                # roll attempt never actually happened.
                if boost_before_use is not None:
                    collection["roll_boost"] = boost_before_use
                storage.save_collection(guild_id, interaction.user.id, collection)
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
        color=discord.Color.gold(),
    )
    embed.set_author(name=item["rarity"], icon_url=item.get("tier_icon"))
    embed.set_image(url=item["icon"])
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
            embed = discord.Embed(
                title=item["name"], description=f"Rarity: **{item['rarity']}**", color=discord.Color.purple()
            )
            embed.set_image(url=item["icon"])
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


@tree.command(name="collection", description="Browse your Valorant skin collection")
async def collection_cmd(interaction: discord.Interaction):
    if not await _check_channel_lock(interaction, "roll"):
        return
    if interaction.guild is None:
        return

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.guild.id, interaction.user.id)
        gacha.sync_roll_charges(collection)
        storage.save_collection(interaction.guild.id, interaction.user.id, collection)

    charges_line = f"🔋 Roll charges: **{collection['charges']}/{gacha.MAX_ROLL_CHARGES}**"

    items = collection.get("items", [])
    if not items:
        await interaction.response.send_message(
            f"You haven't rolled any skins yet. Try `/rollskin`!\n{charges_line}", ephemeral=True
        )
        return

    counts_line = " · ".join(f"{count} {rarity}" for rarity, count in gacha.rarity_counts(items))
    header = (
        f"🏆 **{interaction.user.display_name}'s Collection** — {len(items)} total\n"
        f"{counts_line}\n"
        f"{charges_line}"
    )

    sorted_items = gacha.sort_items_by_rarity(items)
    view = CollectionView(owner_id=interaction.user.id, items=sorted_items, header=header)
    content, embeds = view.render()
    if view.max_page == 0:
        view.stop()
        await interaction.response.send_message(content=content, embeds=embeds)
    else:
        await interaction.response.send_message(content=content, embeds=embeds, view=view)
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
    ("/collection", "Browse your full skin collection and charges", None),
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


@tree.command(name="instantban", description="(Mods/Admins only) Instantly ban this user id if/when they join")
@app_commands.describe(id="The Discord user id to ban on sight")
async def instantban_cmd(interaction: discord.Interaction, id: str):
    if not _is_mod_or_admin(interaction):
        await interaction.response.send_message("Only mods or admins can do that.", ephemeral=True)
        return

    if interaction.guild is None:
        return

    if not id.isdigit():
        await interaction.response.send_message("That doesn't look like a valid user id.", ephemeral=True)
        return
    user_id = int(id)

    added = storage.add_instant_ban(interaction.guild.id, user_id)
    if not added:
        await interaction.response.send_message(f"`{user_id}` is already on the instant-ban list.", ephemeral=True)
        return

    # Ban immediately if they're already in the server, not just future joins.
    member = interaction.guild.get_member(user_id)
    if member is not None:
        try:
            await interaction.guild.ban(member, reason="Added to the instant-ban list")
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        f"🔨 `{user_id}` will now be instantly banned if they join (or were just banned, if already here).",
        ephemeral=True,
    )


@client.event
async def on_member_join(member: discord.Member):
    if member.id in storage.get_instant_ban_list(member.guild.id):
        try:
            await member.ban(reason="On the instant-ban list")
        except discord.HTTPException as exc:
            print(f"instantban failed for {member.id} in guild {member.guild.id}: {exc!r}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
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
    print(f"Logged in as {client.user} (id: {client.user.id})")


if __name__ == "__main__":
    client.run(TOKEN)
