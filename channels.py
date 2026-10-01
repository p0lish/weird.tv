"""TV channels: filtered views over the clip library.

Each channel may restrict `boards`, match `keywords` at the start of a word in
the thread title or file name, and apply a `filter`: "top" (upvoted), "fresh"
(last 24h) or "vault" (archived clips). Channels without `boards` never show
NSFW boards unless they are marked `"nsfw": true`. Channel 0 is the shared live
broadcast.
Override the list with a JSON file of the same shape (WEIRDTV_CHANNELS).
"""
import json

# 4chan's not-worksafe boards
NSFW_BOARDS = ("aco", "b", "bant", "d", "e", "f", "gif", "h", "hc", "hm", "hr", "i", "ic", "pol",
               "r", "r9k", "s", "s4s", "soc", "t", "trash", "u", "y")

LIVE = {"number": 0, "slug": "live", "name": "LIVE", "live": True}

DEFAULT_CHANNELS = [
    {"number": 1, "slug": "mix", "name": "WEIRD MIX"},
    {"number": 2, "slug": "ylyl", "name": "YLYL", "keywords": ["ylyl", "laugh", "funny", "lol"]},
    {"number": 3, "slug": "music", "name": "MUSIC", "keywords": ["ygyl", "music", "song", "remix", "dance", "ear"]},
    {"number": 4, "slug": "animals", "name": "ANIMALS",
     "keywords": ["animal", "cat", "dog", "bird", "pet", "critter", "doggo"]},
    {"number": 5, "slug": "wcgw", "name": "WCGW", "keywords": ["wcgw", "fail", "wrong", "oops"]},
    {"number": 6, "slug": "top", "name": "TOP RATED", "filter": "top"},
    {"number": 7, "slug": "fresh", "name": "FRESH", "filter": "fresh"},
    {"number": 8, "slug": "vault", "name": "THE VAULT", "filter": "vault"},
    {"number": 9, "slug": "adult", "name": "ADULT 18+", "boards": ["gif", "hc"], "nsfw": True},
]


def load_channels(path=None, boards=None):
    """The channel list; with `boards`, channels limited to boards that aren't
    scraped are left out."""
    channels = DEFAULT_CHANNELS
    if path:
        with open(path) as f:
            channels = json.load(f)
    channels = [dict(c) for c in channels if c.get("slug") != LIVE["slug"]]
    if boards is not None:
        channels = [c for c in channels if not c.get("boards") or set(c["boards"]) & set(boards)]
    numbers = [c["number"] for c in channels]
    if len(set(numbers)) != len(numbers) or not all(1 <= n <= 9 for n in numbers):
        raise ValueError("channel numbers must be unique and between 1 and 9")
    return [dict(LIVE)] + sorted(channels, key=lambda c: c["number"])


def public_channel(channel):
    return {k: channel[k] for k in ("number", "slug", "name")} | {"live": bool(channel.get("live")),
                                                                   "nsfw": bool(channel.get("nsfw"))}
