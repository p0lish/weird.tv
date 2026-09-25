"""TV channels: filtered views over the clip library.

Each channel may restrict `boards`, match `keywords` in the thread title or
file name, and apply a `filter`: "top" (upvoted), "fresh" (last 24h) or
"vault" (archived clips). Channel 0 is the shared live broadcast.
Override the list with a JSON file of the same shape (WEIRDTV_CHANNELS).
"""
import json

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
]


def load_channels(path=None):
    channels = DEFAULT_CHANNELS
    if path:
        with open(path) as f:
            channels = json.load(f)
    channels = [dict(c) for c in channels if c.get("slug") != LIVE["slug"]]
    numbers = [c["number"] for c in channels]
    if len(set(numbers)) != len(numbers) or not all(1 <= n <= 9 for n in numbers):
        raise ValueError("channel numbers must be unique and between 1 and 9")
    return [dict(LIVE)] + sorted(channels, key=lambda c: c["number"])


def public_channel(channel):
    return {k: channel[k] for k in ("number", "slug", "name")} | {"live": bool(channel.get("live"))}
