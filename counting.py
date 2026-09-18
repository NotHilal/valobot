"""Counting game: take turns posting the next number, don't mess up."""

import random

ROAST_MESSAGES = [
    "{mention} counted like they were still using their toes. Back to 1.",
    "{mention} just single-handedly reset {count} people's progress.",
    "{mention} really thought that was the next number. It was not.",
    "RIP the count. {mention} killed it at {count}.",
    "{mention} forgot how numbers work. The streak of {count} is no more.",
    "{mention} went twice in a row like nobody would notice.",
    "{mention} broke it. {count} numbers, gone, because of one person.",
    "Legendary blunder by {mention}. {count} → 0 in one message.",
    "{mention} really said \"trust me\" and then wasn't even close.",
    "Everyone thank {mention} for ruining a perfectly good streak of {count}.",
]


def random_roast(mention: str, count: int) -> str:
    return random.choice(ROAST_MESSAGES).format(mention=mention, count=count)
