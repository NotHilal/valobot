"""Minimal Discord bot: /login, /shop, /logout for a personal VALORANT shop viewer,
plus /rollskin, /collection, /trade for a daily skin-collecting side game."""

import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import gacha
import riot
import storage

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()  # no privileged intents needed - slash commands only
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


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
            f"✅ Logged in as **{name}**. Use `/shop` to see your daily store.", ephemeral=True
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
        name="1️⃣ Log in with Riot",
        value=(
            f"**[Click here to log in](<{riot.build_login_url()}>)**\n"
            "You're logging in on Riot's own website — your password never touches this bot."
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ You'll land on a broken-looking page",
        value="⚠️ **That's completely normal, not a bug!** We only need the web address of that page.",
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Copy that page's address",
        value=(
            "💻 **Computer:** click the address bar, then press `Ctrl+C` (`Cmd+C` on Mac)\n"
            "📱 **Phone:** tap the address bar, then tap **Copy**"
        ),
        inline=False,
    )
    embed.add_field(
        name="4️⃣ Paste it back here",
        value="Click the **Paste login link** button below and paste it in.",
        inline=False,
    )
    embed.set_footer(text="Your Riot password is never seen or stored by this bot.")
    return embed


@tree.command(name="login", description="Link your Riot account to see your daily VALORANT shop")
async def login(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=_build_login_embed(),
        view=LoginView(),
        ephemeral=True,
    )


@tree.command(name="shop", description="Show your daily VALORANT storefront")
async def shop(interaction: discord.Interaction):
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


@tree.command(name="logout", description="Remove your saved Riot login from this bot")
async def logout(interaction: discord.Interaction):
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
    async with storage.collection_lock:
        collection = storage.get_collection(interaction.user.id)
        gacha.sync_roll_charges(collection)
        storage.save_collection(interaction.user.id, collection)

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
        storage.save_collection(interaction.user.id, collection)

    await interaction.response.defer(thinking=True)

    try:
        async with aiohttp.ClientSession() as http:
            pool = await gacha.get_pool(http)
        item = gacha.roll(pool)
    except Exception as exc:
        print(f"rollskin error for user {interaction.user.id}: {exc!r}")
        async with storage.collection_lock:
            collection = storage.get_collection(interaction.user.id)
            collection["charges"] = min(gacha.MAX_ROLL_CHARGES, collection.get("charges", 0) + 1)
            storage.save_collection(interaction.user.id, collection)
        await interaction.followup.send("Couldn't fetch the skin pool right now. Try again shortly.")
        return

    async with storage.collection_lock:
        collection = storage.get_collection(interaction.user.id)
        collection["items"].append(item)
        storage.save_collection(interaction.user.id, collection)

    rolls_left = collection["charges"]
    embed = discord.Embed(
        title=f"🎉 {interaction.user.display_name} rolled: {item['name']}",
        description=f"Rarity: **{item['rarity']}**\n{rolls_left} charge{'s' if rolls_left != 1 else ''} left.",
        color=discord.Color.gold(),
    )
    embed.set_image(url=item["icon"])
    await interaction.followup.send(embed=embed)


@tree.command(name="collection", description="Show your top 5 rarest Valorant skins")
async def collection_cmd(interaction: discord.Interaction):
    async with storage.collection_lock:
        collection = storage.get_collection(interaction.user.id)
        gacha.sync_roll_charges(collection)
        storage.save_collection(interaction.user.id, collection)

    charges_line = f"🔋 Roll charges: **{collection['charges']}/{gacha.MAX_ROLL_CHARGES}**"

    items = collection.get("items", [])
    if not items:
        await interaction.response.send_message(
            f"You haven't rolled any skins yet. Try `/rollskin`!\n{charges_line}", ephemeral=True
        )
        return

    top = gacha.top_items(items, 5)
    embeds = []
    for item in top:
        embed = discord.Embed(
            title=item["name"], description=f"Rarity: **{item['rarity']}**", color=discord.Color.purple()
        )
        embed.set_image(url=item["icon"])
        embeds.append(embed)

    header = (
        f"🏆 **{interaction.user.display_name}'s Top {len(top)} Skins** — {len(items)} total in collection\n"
        f"{charges_line}"
    )
    await interaction.response.send_message(content=header, embeds=embeds)


# Maps a user id to the TradeView they're currently tied up in (as proposer or
# responder), so we can block new trades involving someone who already has one
# pending. Cleared on accept, decline, or timeout.
active_trades: dict[int, "TradeView"] = {}

TRADE_TIMEOUT_SECONDS = 3600  # 1 hour


class TradeView(discord.ui.View):
    def __init__(self, proposer: discord.Member, responder: discord.Member, offer_item: dict, request_item: dict):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
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

        proposer_collection = storage.get_collection(self.proposer.id)
        responder_collection = storage.get_collection(self.responder.id)

        given = gacha.pop_item(proposer_collection, self.offer_item["name"])
        received = gacha.pop_item(responder_collection, self.request_item["name"])

        for child in self.children:
            child.disabled = True
        self._release()

        if not given or not received:
            await interaction.response.edit_message(
                content="This trade is no longer valid (an item was already traded away).", view=self
            )
            return

        proposer_collection["items"].append(received)
        responder_collection["items"].append(given)
        storage.save_collection(self.proposer.id, proposer_collection)
        storage.save_collection(self.responder.id, responder_collection)

        await interaction.response.edit_message(
            content=f"✅ Trade completed between {self.proposer.display_name} and {self.responder.display_name}!",
            view=self,
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


async def _offer_autocomplete(interaction: discord.Interaction, current: str):
    collection = storage.get_collection(interaction.user.id)
    names = sorted({item["name"] for item in collection.get("items", [])})
    matches = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


async def _member_collection_autocomplete(interaction: discord.Interaction, current: str):
    """Autocompletes item names from whichever member is bound to the command's `user` option."""
    target = interaction.namespace.user
    if not target:
        return []
    collection = storage.get_collection(target.id)
    names = sorted({item["name"] for item in collection.get("items", [])})
    matches = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


@tree.command(name="trade", description="Offer to trade one of your skins for one of a friend's")
@app_commands.describe(
    user="Who to trade with", offer="Your skin to give", request="Their skin you want in return"
)
@app_commands.autocomplete(offer=_offer_autocomplete, request=_member_collection_autocomplete)
async def trade_cmd(interaction: discord.Interaction, user: discord.Member, offer: str, request: str):
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

    my_collection = storage.get_collection(interaction.user.id)
    their_collection = storage.get_collection(user.id)

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

    view = TradeView(proposer=interaction.user, responder=user, offer_item=my_item, request_item=their_item)
    active_trades[interaction.user.id] = view
    active_trades[user.id] = view
    embed = discord.Embed(title="🔄 Trade Offer", color=discord.Color.blue())
    embed.add_field(name=f"{interaction.user.display_name} gives", value=f"{my_item['name']} ({my_item['rarity']})")
    embed.add_field(name=f"{user.display_name} gives", value=f"{their_item['name']} ({their_item['rarity']})")

    await interaction.response.send_message(content=f"{user.mention}, you have a trade offer!", embed=embed, view=view)
    view.message = await interaction.original_response()


@tree.command(name="addskin", description="(Server owner only) Add a custom skin to the roll pool")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="Skin name", rarity="Rarity tier", image="Upload an image for this skin")
@app_commands.choices(rarity=[app_commands.Choice(name=tier, value=tier) for tier in gacha.RARITY_TIERS])
async def addskin_cmd(
    interaction: discord.Interaction, name: str, rarity: app_commands.Choice[str], image: discord.Attachment
):
    if not _is_owner(interaction):
        await interaction.response.send_message("Only the server owner can add custom skins.", ephemeral=True)
        return

    if not (image.content_type or "").startswith("image/"):
        await interaction.response.send_message("Please upload an image file.", ephemeral=True)
        return

    custom_skins = gacha.load_custom_skins_raw()
    custom_skins.append({"name": name, "rarity": rarity.value, "icon": image.url})
    gacha.save_custom_skins_raw(custom_skins)

    embed = discord.Embed(
        title=f"✅ Added custom skin: {name}", description=f"Rarity: **{rarity.value}**", color=discord.Color.green()
    )
    embed.set_image(url=image.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


@tree.command(name="give", description="(Server owner only) Give a user a specific skin")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Who to give the skin to", skin="The skin to give (start typing to search)")
@app_commands.autocomplete(skin=_pool_skin_autocomplete)
async def give_cmd(interaction: discord.Interaction, user: discord.Member, skin: str):
    if not _is_owner(interaction):
        await interaction.response.send_message("Only the server owner can do that.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as http:
        pool = await gacha.get_pool(http)
    item = gacha.find_in_pool(pool, skin)
    if not item:
        await interaction.followup.send(f"No skin named \"{skin}\" found.", ephemeral=True)
        return

    collection = storage.get_collection(user.id)
    collection["items"].append(gacha.stamp(item))
    storage.save_collection(user.id, collection)
    await interaction.followup.send(
        f"🎁 Gave **{item['name']}** ({item['rarity']}) to {user.display_name}.", ephemeral=True
    )


@tree.command(name="removeskin", description="(Server owner only) Remove a skin from a user's collection")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Whose collection to remove from", skin="The skin to remove")
@app_commands.autocomplete(skin=_member_collection_autocomplete)
async def removeskin_cmd(interaction: discord.Interaction, user: discord.Member, skin: str):
    if not _is_owner(interaction):
        await interaction.response.send_message("Only the server owner can do that.", ephemeral=True)
        return

    collection = storage.get_collection(user.id)
    removed = gacha.pop_item(collection, skin)
    if not removed:
        await interaction.response.send_message(
            f"{user.display_name} doesn't have a skin named \"{skin}\".", ephemeral=True
        )
        return

    storage.save_collection(user.id, collection)
    await interaction.response.send_message(
        f"🗑️ Removed **{removed['name']}** from {user.display_name}'s collection.", ephemeral=True
    )


@tree.command(name="removeallcollection", description="(Server owner only) Wipe a user's entire skin collection")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Whose collection to wipe")
async def removeallcollection_cmd(interaction: discord.Interaction, user: discord.Member):
    if not _is_owner(interaction):
        await interaction.response.send_message("Only the server owner can do that.", ephemeral=True)
        return

    deleted = storage.delete_collection(user.id)
    if not deleted:
        await interaction.response.send_message(
            f"{user.display_name} doesn't have a collection to wipe.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🗑️ Wiped {user.display_name}'s entire skin collection.", ephemeral=True
    )


async def _custom_skin_autocomplete(interaction: discord.Interaction, current: str):
    names = [item["name"] for item in gacha.load_custom_skins_raw()]
    matches = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in matches]


@tree.command(name="deleteskin", description="(Server owner only) Delete a custom skin from the pool")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(skin="The custom skin to delete")
@app_commands.autocomplete(skin=_custom_skin_autocomplete)
async def deleteskin_cmd(interaction: discord.Interaction, skin: str):
    if not _is_owner(interaction):
        await interaction.response.send_message("Only the server owner can do that.", ephemeral=True)
        return

    custom_skins = gacha.load_custom_skins_raw()
    remaining = [s for s in custom_skins if s["name"].lower() != skin.lower()]
    if len(remaining) == len(custom_skins):
        await interaction.response.send_message(f"No custom skin named \"{skin}\" found.", ephemeral=True)
        return

    gacha.save_custom_skins_raw(remaining)
    await interaction.response.send_message(f"🗑️ Deleted custom skin **{skin}** from the pool.", ephemeral=True)


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
