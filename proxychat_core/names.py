"""Cleaning names that come from games (empires, rulers, federations) before Discord sees them.

Names reach channel names, DMs and announcements. They come from the game, and on
the central server from a relay, so they're treated as untrusted text: no control
or invisible characters, no Discord markup or mention syntax, a bounded length.
"""

import unicodedata

# Discord's limit for channel names, and a sensible cap for names in messages
MAX_NAME = 100

# Mention and markdown syntax: @everyone, <@id>, #channel, **bold**, `code`, spoilers...
_STRIP_CHARS = set("@#<>*_~|`\\")
# Control, format (zero-width, bidi overrides), unassigned, private-use and surrogates
_DROP_CATEGORIES = {"Cc", "Cf", "Cn", "Co", "Cs"}


def clean_name(text, fallback="", limit=MAX_NAME):
    """text made safe to show in Discord, or fallback if nothing usable is left."""
    text = unicodedata.normalize("NFC", text or "")
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch.isspace() or category in ("Zs", "Zl", "Zp"):
            kept.append(" ")
        elif category not in _DROP_CATEGORIES and ch not in _STRIP_CHARS:
            kept.append(ch)
    cleaned = " ".join("".join(kept).split())
    return cleaned[:limit].rstrip() or fallback[:limit]
