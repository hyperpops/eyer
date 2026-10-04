"""
EYE vouch bot
-------------
/vouch  -> anonymous vouch embed ("New Vouch !" embed with 1-5 stars, a message, optional image links)
/vouches -> public stats (total, average, star breakdown)
/start, /stop -> DEMO vouches for testing the layout (clearly labeled, test channel only)

Anonymous by design: the reply to /vouch is private, and the vouch itself is posted
by the bot with no name, mention or avatar of the person who wrote it.

Run:  pip install -r requirements.txt
      put DISCORD_TOKEN in a .env file (see .env.example)
      python bot.py
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1500924795477430272
VOUCH_CHANNEL_ID: Optional[int] = None   # set your vouch channel ID; None = post where /vouch is used
STAFF_LOG_CHANNEL_ID: Optional[int] = None  # optional and OFF by default: private log of who posted (breaks anonymity for staff)
DEMO_CHANNEL_ID: Optional[int] = 1556099528363606037    # test channel for /start (required, /start refuses without it)
DEMO_INTERVAL_MINUTES = 20               # one DEMO vouch every 20 minutes while /start is running
COOLDOWN_SECONDS = 20                    # anti-spam between vouches from the same person
MAX_IMAGES = 3
MAX_MESSAGE = 1000

# Discord attachment links expire. Host the logo somewhere permanent
# (for example on your website) and paste the new link here.
LOGO = (
    "https://media.discordapp.net/attachments/1551693461147746328/1556103325022429225/"
    "eye-logo-white-1024.png?backend=b2&ex=6ac2f1a7&is=6ac1a027&"
    "hm=b09867f6bf9af76b35711877b19c602b731c76f74d7b9a3b5a84f0f8bb8e423b&=&format=webp"
    "&quality=lossless"
)
FOOTER_TEXT = "Provided by @ Eye all rights reserved."
EMBED_COLOR = discord.Color.from_rgb(255, 255, 255)

DATA_FILE = Path(__file__).with_name("vouch_data.json")
SEP = "▬" * 16
GALLERY_URL = "https://discord.gg/eyed"  # same url on several embeds makes Discord group their images
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

COOLDOWNS: dict[int, float] = {}
LOCK: Optional[asyncio.Lock] = None   # created once the bot's event loop exists


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
def make_embed(title: Optional[str] = None, description: Optional[str] = None, *, image: Optional[str] = None) -> discord.Embed:
    """Every embed goes through here so color, header and footer are always identical."""
    e = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    e.set_author(name="EYE", icon_url=LOGO)
    e.set_footer(text=FOOTER_TEXT, icon_url=LOGO)
    if image:
        e.set_image(url=image)
    return e


def star_line(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def load_data() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text())
    except Exception:
        data = {}
    data.setdefault("counter", 0)
    stars = data.setdefault("stars", {})
    for n in range(1, 6):
        stars.setdefault(str(n), 0)
    return data


def save_data(data: dict) -> None:
    try:
        DATA_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def parse_images(raw: Optional[str]) -> tuple[list[str], list[str]]:
    """Returns (valid direct image links, rejected tokens)."""
    good: list[str] = []
    bad: list[str] = []
    for token in re.split(r"[\s,]+", (raw or "").strip()):
        if not token:
            continue
        parsed = urlparse(token)
        ok = parsed.scheme in ("http", "https") and bool(parsed.netloc) and parsed.path.lower().endswith(IMAGE_EXT)
        (good if ok else bad).append(token)
    return good[:MAX_IMAGES], bad


async def reply(interaction: discord.Interaction, embed: discord.Embed):
    """Private message, whether or not the interaction was already answered."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def vouch_embeds(stars: int, message: str, images: list[str]) -> list[discord.Embed]:
    quote = "\n".join(f"> {line}" if line.strip() else ">" for line in message.splitlines())
    description = f"# New Vouch !\n## {star_line(stars)}\n**{stars}/5**\n{SEP}\n{quote}"
    main = make_embed(description=description)
    main.timestamp = discord.utils.utcnow()
    embeds = [main]
    if images:
        main.set_image(url=images[0])
        if len(images) > 1:
            main.url = GALLERY_URL
            for url in images[1:]:
                extra = make_embed(image=url)
                extra.url = GALLERY_URL
                embeds.append(extra)
    return embeds


# --------------------------------------------------------------------------
# BOT
# --------------------------------------------------------------------------
class VouchBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents(guilds=True),
            activity=discord.Activity(type=discord.ActivityType.watching, name="vouches"),
            chunk_guilds_at_startup=False,
        )

    async def setup_hook(self):
        global LOCK
        LOCK = asyncio.Lock()
        await self.tree.sync(guild=discord.Object(id=GUILD_ID))

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")


bot = VouchBot()
GUILD = discord.Object(id=GUILD_ID)


# --------------------------------------------------------------------------
# DEMO VOUCHES (for testing the look of the channel; always labeled DEMO)
# --------------------------------------------------------------------------
DEMO_TAG = "Thanks for using Eye."

KEY_PRODUCTS = ["Hunter", "Eclipse", "Oneclick", "Emulator"]
UNBAN_GAMES = ["Valorant", "Fortnite", "Roblox"]
ALL_TOPICS = KEY_PRODUCTS + ["DMA"] + UNBAN_GAMES
DURATIONS = ["3 day", "weekly", "monthly", "lifetime"]

# ---- key products (Hunter, Eclipse, Oneclick, Emulator) --------------------
KEY_OPENERS = [
    "Bought a {dur} {product} key and was set up in minutes.",
    "{product} {dur} key arrived right after payment.",
    "Got the {dur} {product} key and the setup guide was easy to follow.",
    "Tried {product} with a {dur} key and it was a smooth start.",
    "The {dur} {product} key was good value for what you get.",
    "{product} is simple to use, so the {dur} key was an easy choice.",
    "Ordered a {dur} {product} key through a ticket and it was painless.",
    "First time buying here, took the {dur} {product} key and it went well.",
    "Went back for another {dur} {product} key, that says it all.",
    "Picked up {product} on a {dur} key and the whole process was clear.",
    "Delivery of my {product} key was fast, {dur} plan.",
    "Honestly happy with {product}, the {dur} key was the right call.",
    "My {dur} {product} key activated on the first try.",
    "Needed a {dur} {product} key quickly and got it without any waiting.",
    "Super easy purchase, {dur} {product} key sorted in a few minutes.",
    "A friend told me about {product}, so I grabbed the {dur} key.",
    "Grabbed a {dur} {product} key on a whim and I'm glad I did.",
    "Been wanting {product} for a while, finally got the {dur} key.",
    "Bought {product} ({dur}) and had my key within minutes.",
    "{product} {dur} key, no issues from purchase to activation.",
    "Went with the {dur} {product} key and it was the right fit for me.",
    "Everything about my {product} order was clear, from the price to the {dur} key delivery.",
    "Just activated my {dur} {product} key and it was effortless.",
    "Quick order for a {dur} {product} key, done before I finished my coffee.",
    "{product} {dur} key purchased and working, simple as that.",
    "Wasn't sure about {product} at first, the {dur} key convinced me.",
    "Second time ordering {product}, same smooth experience with the {dur} key.",
    "Bought the {dur} {product} key for a friend and they were happy with it.",
    "The {dur} {product} key came exactly as described.",
    "Ordered {product} late at night and still got my {dur} key quickly.",
    "Setting up {product} with the {dur} key took less time than I expected.",
    "Straightforward order, {dur} {product} key delivered with clear instructions.",
    "I went for the {dur} {product} key and everything lined up with what was promised.",
    "Great first impression with {product}, the {dur} key was easy to redeem.",
    "{product} with a {dur} key, ordering took two minutes.",
    "Treated myself to a {dur} {product} key and zero regrets.",
    "Picked the {dur} {product} key after reading the price list and it matched everything.",
    "No confusion at all when buying my {dur} {product} key.",
    "Easy deal on the {dur} {product} key.",
    "The {dur} {product} key was exactly what I was looking for.",
]
KEY_FLAVOR = {
    "Hunter": [
        "The interface is easy to find your way around.",
        "Settings are simple to find from the first launch.",
        "It runs well on my setup.",
        "Everything I needed was explained clearly.",
        "Everything feels well thought out.",
        "The options are laid out in a way that makes sense.",
        "Learning my way around took no time.",
        "It does what the description says.",
        "Updates and info were easy to find.",
        "Nothing felt overcomplicated.",
    ],
    "Eclipse": [
        "Clean layout with nothing confusing.",
        "Easy to configure the way I like.",
        "Eclipse feels polished and well put together.",
        "Runs well on my machine.",
        "Simple to set up and simple to use.",
        "Everything is where you expect it to be.",
        "The whole thing feels well made.",
        "Easy to get comfortable with.",
        "Nothing felt confusing on my first day.",
        "Customizing it was easy.",
    ],
    "Oneclick": [
        "One click setup, just like the name says.",
        "No technical knowledge needed at all.",
        "Everything was ready in a couple of clicks.",
        "Perfect if you want something simple.",
        "Even my non technical friend could set it up.",
        "Honestly the easiest one to start with.",
        "Takes almost no effort to get going.",
        "Clear steps, nothing to figure out.",
        "Setup really is quick.",
        "The simplicity is the best part.",
    ],
    "Emulator": [
        "The install guide covered every step.",
        "Setup took me a short evening and that was it.",
        "Runs smoothly on my machine.",
        "Instructions were clear even for a beginner.",
        "Followed the steps one by one and it just worked.",
        "Clear instructions made all the difference.",
        "It took me one sitting to finish the whole setup.",
        "No guesswork, everything is documented.",
        "Easy to follow even if you have never done it before.",
        "Install went without any surprises.",
    ],
}
KEY_NOTES = [  # small notes for 4 star vouches
    "Took a bit to get going the first time but support sorted it quickly.",
    "Only not five stars because I waited a little for the first reply.",
    "Needed a second read of the guide, otherwise great.",
    "Minor hiccup at the start, fixed fast by the team.",
    "Lost a few minutes figuring out one step, otherwise great.",
    "Slight delay on the first reply, everything after that was fast.",
    "Would like a few more setup tips, but support filled in the gaps.",
    "Not perfect on the first try, but the team stayed with me until it was sorted.",
    "One small question at the start, answered quickly.",
    "Waited a little for delivery, nothing major.",
    "Instructions could be more detailed, but support helped.",
    "Took two tries to get set up, then all good.",
]

# ---- DMA -------------------------------------------------------------------
DMA_OPENERS = [
    "Ordered the DMA and it arrived well packed.",
    "The DMA matched the listing exactly.",
    "Got my DMA and everything was in the box, clearly labeled.",
    "The DMA setup guide was clear, took me an evening to get through it.",
    "Shipping update on the DMA came quickly and communication was good.",
    "First DMA order and it went smoothly.",
    "The DMA hardware feels solid and well made.",
    "Really easy ordering process for the DMA, everything handled in the ticket.",
    "The DMA arrived quicker than I expected.",
    "Packaging on the DMA was clean and protective.",
    "Ordered the DMA through a ticket and the whole thing was clear and fast.",
    "Great communication while my DMA was on the way.",
    "Unboxing the DMA was a good experience, everything was labeled.",
    "The DMA came with clear instructions that actually made sense.",
    "Happy with the DMA hardware quality.",
    "I was nervous about ordering hardware online, but the DMA order went well.",
    "DMA arrived exactly as listed on the website.",
    "Everything in the DMA order matched what I paid for.",
    "The team kept me updated on the DMA shipping.",
    "Clean, simple DMA order from start to finish.",
]
DMA_FLAVOR = [
    "Instructions were easy to follow.",
    "Packaging was neat and secure.",
    "Price was fair for the hardware.",
    "Delivery matched what they told me.",
    "Build quality feels premium.",
    "Tracking info was shared without me asking.",
    "Everything was sealed and secure.",
    "The setup guide was easy to follow step by step.",
    "Easy to work with from day one.",
    "Nothing was missing from the box.",
]

# ---- account unban ---------------------------------------------------------
UNBAN_OPENERS = [
    "Opened a ticket for my {game} account and only my username was needed.",
    "They never asked for a password or login for my {game} account, just the username.",
    "{game} account unban ticket was handled professionally from the start.",
    "Clear communication on my {game} account the whole way through.",
    "Fast replies in the ticket for my {game} account case.",
    "Used the {game} account unban service and the process was explained clearly.",
    "Happy with how they handled my {game} account, no login details needed.",
    "Was worried about sharing details for my {game} account, but only a username was asked for.",
    "Straightforward {game} account unban order, no password needed.",
    "Never had to share a password for my {game} account, only the username.",
    "Opened an account unban ticket for {game} and got a clear explanation right away.",
    "The {game} account unban process was explained clearly before I paid.",
    "Professional handling of my {game} account from the first message.",
    "Only needed to send my {game} username, which made me comfortable ordering.",
    "The team kept me informed the whole time on my {game} account.",
    "Clear steps for the {game} account unban order, no confusion.",
    "Asked a few questions about the {game} account unban before ordering and got honest answers.",
    "Quick response when I opened the {game} account unban ticket.",
    "Felt looked after throughout my {game} account unban order.",
    "They only asked for my {game} username, nothing else.",
    "Easy to order the {game} account unban, the ticket guided me through it.",
    "Friendly staff handling my {game} account case.",
    "Good experience with the {game} account unban service.",
    "Transparent about the process for my {game} account from the start.",
]
UNBAN_FLAVOR = [
    "They explained what to expect before I paid.",
    "Updates in the ticket were quick and clear.",
    "Felt safe since no login details were involved.",
    "Good communication, I always knew where things stood.",
    "Prices were clear up front.",
    "Never felt rushed or pressured.",
    "They were straightforward about how it works.",
    "Questions were answered patiently.",
    "Everything stayed inside the ticket.",
    "Communication was the best part.",
]
UNBAN_NOTES = [
    "Took a little patience but communication stayed clear.",
    "Only not five stars because I waited a bit for updates.",
    "A few questions at the start, all answered politely.",
    "Wish updates came a little more often, but they were polite.",
    "Answers took a bit at times, always honest though.",
    "I had a couple of extra questions and they were patient.",
    "The process needed some patience but the communication was solid.",
    "First reply was a little slow, the rest was good.",
]

# ---- shared lines (all products and games) ---------------------------------
CONTEXT_LINES = [
    "Found the server through a friend.",
    "Saw the price list on the website and decided to try.",
    "Joined the Discord a few days ago and finally ordered.",
    "Read through the channels first and everything looked organized.",
    "Heard good things so I tried it myself.",
    "Was comparing prices and this one fit my budget.",
    "Checked the website before ordering and it was all clearly laid out.",
    "Opened a ticket after reading how to buy on the site.",
    "Took my time deciding and I'm glad I went for it.",
    "Ordered during the weekend.",
    "Wasn't sure what to expect at first.",
    "Placed my order today.",
    "Joined recently and ordered the same day.",
    "Read the steps on the website and followed them.",
    "Was nervous about ordering online at first.",
]
TIMING_LINES = [
    "Everything was done within minutes.",
    "Delivery was quicker than expected.",
    "From ticket to done in no time.",
    "No waiting around at all.",
    "Processed almost instantly after payment.",
    "Got everything the same day.",
    "Faster than I thought it would be.",
    "Zero delays.",
    "Payment confirmed quickly and the rest followed.",
    "Hardly any wait.",
    "Quick turnaround.",
    "It all happened quickly.",
]
VALUE_LINES = [
    "Price was fair for what you get.",
    "Worth every dollar.",
    "Good value for the money.",
    "Prices are clear and there are no surprises.",
    "The price list on the website matched what I paid.",
    "Fair pricing.",
    "Great price for the quality.",
    "No hidden costs.",
    "Pricing is transparent.",
    "Value is solid.",
]
SUPPORT_LINES = [
    "Support in the ticket answered fast.",
    "The team walked me through it step by step.",
    "Staff were polite and replied within minutes.",
    "Ordering through the ticket was quick and clear.",
    "Questions got answered without any fuss.",
    "Payment was easy and confirmed quickly.",
    "Really responsive team.",
    "Good communication throughout.",
    "Helpful staff, no pressure.",
    "Everything handled in the ticket, very organized.",
    "Staff replied fast even late in the day.",
    "The team was patient with all my questions.",
    "Support felt personal, not scripted.",
    "Fast replies, friendly tone.",
    "The ticket system made everything easy.",
    "I never had to wait long for an answer.",
    "Staff were clear about each step.",
    "Quick help whenever I had a question.",
    "Great attention to detail from the staff.",
    "They checked in to make sure everything was fine.",
    "No runaround, just straight answers.",
    "The whole process was handled calmly and clearly.",
    "Appreciated how organized the ticket was.",
    "Replies were quick and polite.",
    "Support went the extra mile.",
    "Very welcoming staff.",
    "Ticket was opened and answered within minutes.",
    "Handled with care by the team.",
    "Clear updates from staff the whole time.",
]
CLOSERS = [
    "Would recommend.",
    "Smooth purchase from start to finish.",
    "Will be back for another.",
    "Happy with it.",
    "Good experience overall.",
    "Solid service.",
    "Thanks to the team.",
    "Recommended.",
    "Fair prices and fast service.",
    "Not my last order.",
    "Great service.",
    "Definitely ordering again.",
    "Highly recommend the server.",
    "Thanks again to the team.",
    "Really pleased with the service.",
    "Glad I found this server.",
    "Nothing but good things to say.",
    "Honest and reliable service.",
    "Pleasant experience all around.",
    "Quick, clean and professional.",
    "Would buy again without thinking twice.",
    "Great value, great service.",
    "Easy decision to order here.",
    "Thumbs up from me.",
    "Impressed overall.",
    "Smooth from the first message to the last.",
    "Appreciate the help.",
    "Good people running this.",
    "Worth it.",
]

_bag: list[str] = []
_last_topic: Optional[str] = None
_recent_demo: deque = deque(maxlen=150)      # whole messages already used
_recent_openers: deque = deque(maxlen=14)    # opener templates already used


def _next_topic() -> str:
    """Shuffle-bag rotation: every product and game comes up once before any repeats."""
    global _bag
    if not _bag:
        _bag = ALL_TOPICS[:]
        random.shuffle(_bag)
        if len(_bag) > 1 and _bag[-1] == _last_topic:   # pop() takes from the end
            _bag[0], _bag[-1] = _bag[-1], _bag[0]
    return _bag.pop()


def _compose(topic: str, stars: int) -> list[str]:
    if topic in UNBAN_GAMES:
        openers, flavor, notes, fmt = UNBAN_OPENERS, UNBAN_FLAVOR, UNBAN_NOTES, {"game": topic}
    elif topic == "DMA":
        openers, flavor, notes, fmt = DMA_OPENERS, DMA_FLAVOR, KEY_NOTES, {}
    else:
        openers, flavor, notes = KEY_OPENERS, KEY_FLAVOR[topic], KEY_NOTES
        fmt = {"product": topic, "dur": random.choice(DURATIONS)}

    fresh = [o for o in openers if o not in _recent_openers] or openers
    opener = random.choice(fresh)
    _recent_openers.append(opener)

    lead = random.choice(CONTEXT_LINES) if random.random() < 0.25 else None
    mids: list[str] = []
    if random.random() < 0.8:                       # most posts get some detail, 1 in 5 stay short
        if random.random() < 0.55:
            mids.append(random.choice(flavor).format(**fmt))
        if random.random() < 0.3:
            mids.append(random.choice(TIMING_LINES))
        if random.random() < 0.3:
            mids.append(random.choice(VALUE_LINES))
        if random.random() < 0.5:
            mids.append(random.choice(SUPPORT_LINES))
        random.shuffle(mids)
        mids = mids[:2]                             # keep every vouch easy to read

    parts = ([lead] if lead else []) + [opener.format(**fmt)] + mids
    if stars == 4 and random.random() < 0.8:
        parts.append(random.choice(notes))          # 4 star vouches carry a small, honest-sounding note
    elif random.random() < 0.75:
        parts.append(random.choice(CLOSERS))
    return parts


def _style(parts: list[str]) -> str:
    """Varies the tone so test posts do not all look the same."""
    text = " ".join(parts)
    first = text.split()[0].strip(",.")
    if random.random() < 0.25 and first not in ALL_TOPICS:   # relaxed lowercase tone
        text = text[0].lower() + text[1:]
        text = text.rstrip(".")
    return text


def demo_message(stars: int) -> str:
    global _last_topic
    topic = _next_topic()
    _last_topic = topic
    text = ""
    for _ in range(40):
        text = _style(_compose(topic, stars))
        if text not in _recent_demo:
            break
    _recent_demo.append(text)
    return text


def demo_embed() -> discord.Embed:
    stars = random.choices([5, 4], weights=[7, 3])[0]
    quote = "\n".join(f"> {line}" for line in demo_message(stars).splitlines())
    description = f"# New Vouch !\n## {star_line(stars)}\n**{stars}/5**\n{DEMO_TAG}\n{SEP}\n{quote}"
    e = make_embed(description=description)
    e.timestamp = discord.utils.utcnow()
    return e


@tasks.loop(minutes=DEMO_INTERVAL_MINUTES)
async def demo_loop():
    channel = bot.get_channel(DEMO_CHANNEL_ID) if DEMO_CHANNEL_ID else None
    if channel is None:
        return
    try:
        await channel.send(embed=demo_embed(), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        traceback.print_exc()


@bot.tree.command(name="start", description="Start posting DEMO vouches in the test channel", guild=GUILD)
@app_commands.default_permissions(administrator=True)
async def start(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await reply(interaction, make_embed("Administrators only"))
        return
    channel = interaction.guild.get_channel(DEMO_CHANNEL_ID) if DEMO_CHANNEL_ID else None
    if channel is None:
        await reply(interaction, make_embed("Set a test channel first", "Put your test channel ID in DEMO_CHANNEL_ID at the top of bot.py, then restart the bot."))
        return
    if demo_loop.is_running():
        await reply(interaction, make_embed("Already running", "Use /stop to stop the demo vouches."))
        return
    demo_loop.start()
    await reply(interaction, make_embed("Demo started", f"A DEMO vouch will post in {channel.mention} now and then every {DEMO_INTERVAL_MINUTES} minutes. Use /stop to stop."))


@bot.tree.command(name="stop", description="Stop the DEMO vouches", guild=GUILD)
@app_commands.default_permissions(administrator=True)
async def stop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await reply(interaction, make_embed("Administrators only"))
        return
    if not demo_loop.is_running():
        await reply(interaction, make_embed("Not running", "There are no demo vouches running."))
        return
    demo_loop.cancel()
    await reply(interaction, make_embed("Demo stopped", "No more demo vouches will be posted."))


STAR_CHOICES = [
    app_commands.Choice(name=f"{star_line(n)}  ({n}/5)", value=n) for n in range(5, 0, -1)
]


@bot.tree.command(name="vouch", description="Leave an anonymous vouch", guild=GUILD)
@app_commands.describe(
    stars="Your rating from 1 to 5 stars",
    message="What do you want to say?",
    images="Optional: up to 3 direct image links, separated by spaces",
)
@app_commands.choices(stars=STAR_CHOICES)
async def vouch(
    interaction: discord.Interaction,
    stars: app_commands.Choice[int],
    message: str,
    images: Optional[str] = None,
):
    # quick checks first, all private
    last = COOLDOWNS.get(interaction.user.id, 0.0)
    if time.monotonic() - last < COOLDOWN_SECONDS:
        await reply(interaction, make_embed("One moment", "Please wait a few seconds before sending another vouch."))
        return

    message = message.strip()
    if not message:
        await reply(interaction, make_embed("Message missing", "Write a short message with your vouch."))
        return
    if len(message) > MAX_MESSAGE:
        await reply(interaction, make_embed("Message too long", f"Please keep it under {MAX_MESSAGE} characters."))
        return

    urls, bad = parse_images(images)
    if bad:
        await reply(
            interaction,
            make_embed(
                "Image link problem",
                "Image links must be direct links ending in .png, .jpg, .jpeg, .gif or .webp.\n"
                "Separate several links with spaces.",
            ),
        )
        return

    # private "thinking" state: nobody sees who used the command
    await interaction.response.defer(ephemeral=True)

    channel = (interaction.guild.get_channel(VOUCH_CHANNEL_ID) if VOUCH_CHANNEL_ID else None) or interaction.channel
    try:
        async with LOCK:
            data = load_data()
            sent = await channel.send(
                embeds=vouch_embeds(stars.value, message, urls),
                allowed_mentions=discord.AllowedMentions.none(),   # nobody can ping through a vouch
            )
            data["counter"] += 1
            data["stars"][str(stars.value)] += 1
            save_data(data)
        COOLDOWNS[interaction.user.id] = time.monotonic()
    except discord.Forbidden:
        await reply(interaction, make_embed("Can't post here", "I don't have permission to send messages in the vouch channel."))
        return
    except Exception:
        traceback.print_exc()
        await reply(interaction, make_embed("Something went wrong", "Your vouch could not be posted. Please try again."))
        return

    if STAFF_LOG_CHANNEL_ID:
        log = interaction.guild.get_channel(STAFF_LOG_CHANNEL_ID)
        if log:
            try:
                await log.send(
                    embed=make_embed("Vouch log", f"A vouch was posted by {interaction.user.mention}.\n{sent.jump_url}"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    await reply(
        interaction,
        make_embed("Vouch posted", f"Thank you. Your vouch was posted anonymously.\n{sent.jump_url}"),
    )


@bot.tree.command(name="vouches", description="See the vouch stats", guild=GUILD)
async def vouches(interaction: discord.Interaction):
    data = load_data()
    counts = {n: int(data["stars"].get(str(n), 0)) for n in range(1, 6)}
    total = sum(counts.values())
    if total == 0:
        await interaction.response.send_message(embed=make_embed("Vouches", "No vouches yet."))
        return
    average = sum(n * c for n, c in counts.items()) / total
    rows = []
    for n in range(5, 0, -1):
        bar = "█" * round(counts[n] / total * 16)
        rows.append(f"{n} ★  {bar.ljust(16)}  {counts[n]}")
    description = (
        f"## {average:.1f} / 5\n{star_line(round(average))}\n**{total}** vouches\n{SEP}\n```\n" + "\n".join(rows) + "\n```"
    )
    await interaction.response.send_message(embed=make_embed("Vouches", description))


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    await reply(interaction, make_embed("Something went wrong", "Please try again in a moment."))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or in a .env file.")
    bot.run(TOKEN)