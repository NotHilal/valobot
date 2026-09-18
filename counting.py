"""Counting game: take turns posting the next number, don't mess up."""

import random

# Used for any mistake (wrong number or double-post).
ROAST_MESSAGES = [
    "{mention} counted like they were still using their fingers.",
    "{mention} just single-handedly reset {count} people's progress.",
    "{mention} really said \"trust me\" and then wasn't even close.",
    "Everyone thank {mention} for ruining our streak 🙄",
    "Who invited bro {mention}??",
    "Send {mention} to the wall of shame nowww",
    "Mods ban {mention}",
    "You think you're funny {mention}?",
    "Party pooper {mention}",
    "ofc it had to be {mention}",
    "{mention} Get outttttt",
    "wtf are u saying {mention}",
]

# Only used when the mistake was the same person posting twice in a row -
# picked from exclusively (not mixed with the general pool above).
DOUBLE_POST_ROAST_MESSAGES = [
    "{mention} went twice in a row like nobody would notice.",
    "no need to double send {mention} 🤡",
]


def random_roast(mention: str, count: int, is_double_post: bool = False) -> str:
    pool = DOUBLE_POST_ROAST_MESSAGES if is_double_post else ROAST_MESSAGES
    return random.choice(pool).format(mention=mention, count=count)
