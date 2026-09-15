"""Minimal Discord bot: /login, /shop, /logout for a personal VALORANT shop viewer."""

import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import riot
import storage

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()  # no privileged intents needed - slash commands only
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="login", description="Link your Riot account to see your daily VALORANT shop")
@app_commands.describe(url="After logging in via the link, paste the full URL you land on here")
async def login(interaction: discord.Interaction, url: str = None):
    if url is None:
        await interaction.response.send_message(
            f"1. Tap this link and log in with Riot (your password never touches this bot): {riot.build_login_url()}\n"
            "2. You'll arrive on a **404 error page** after logging in - that's normal, we just need the link "
            "from that page to continue. Please tap **Share** (or the **•••** menu) and copy the URL.\n"
            "3. Switch back here, run `/login` again, and paste it into the `url` option.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        session = await riot.create_session(url)
    except riot.RiotAuthError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    except Exception:
        await interaction.followup.send(
            "❌ Something went wrong talking to Riot. Please try /login again.", ephemeral=True
        )
        return

    storage.save_user(interaction.user.id, session)
    name = session.get("riot_id") or "your account"
    await interaction.followup.send(
        f"✅ Logged in as **{name}**. Use `/shop` to see your daily store.", ephemeral=True
    )


@tree.command(name="shop", description="Show your daily VALORANT storefront")
async def shop(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    session = storage.get_user(interaction.user.id)
    if not session:
        await interaction.followup.send("You're not logged in. Run `/login` first.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as http:
        try:
            storefront = await riot.get_storefront(http, session)
        except riot.SessionExpiredError:
            storage.delete_user(interaction.user.id)
            await interaction.followup.send("Your Riot session expired. Run `/login` again.", ephemeral=True)
            return
        except Exception as exc:
            print(f"shop error for user {interaction.user.id}: {exc!r}")
            await interaction.followup.send(
                "Couldn't reach Riot's servers right now. Try again shortly.", ephemeral=True
            )
            return

        offers, remaining_seconds = riot.parse_daily_offers(storefront)

        embeds = []
        for offer in offers:
            if offer["item_id"]:
                details = await riot.get_skin_details(http, offer["item_id"])
            else:
                details = {"name": "Unknown Skin", "icon": None}

            price = f"{offer['cost']} VP" if offer["cost"] is not None else "Price unavailable"
            embed = discord.Embed(title=details["name"], description=price, color=discord.Color.red())
            if details["icon"]:
                embed.set_thumbnail(url=details["icon"])
            embeds.append(embed)

    remaining_seconds = max(remaining_seconds, 0)
    hours, rem = divmod(remaining_seconds, 3600)
    minutes = rem // 60

    header = f"🎮 **{interaction.user.display_name}'s Daily VALORANT Store** — refreshes in {hours}h {minutes}m"
    await interaction.followup.send(content=header, embeds=embeds[:10], ephemeral=False)


@tree.command(name="logout", description="Remove your saved Riot login from this bot")
async def logout(interaction: discord.Interaction):
    deleted = storage.delete_user(interaction.user.id)
    if deleted:
        await interaction.response.send_message("✅ Your Riot session has been removed.", ephemeral=True)
    else:
        await interaction.response.send_message("You weren't logged in.", ephemeral=True)


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
