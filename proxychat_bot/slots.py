"""Slots and guild settings: what the bot serves, and where.

A slot is one Discord category hosting one game at a time, with its own state
(claims, channels, the game being played). One guild can have several slots, so
two games can run side by side, and the bot can serve several guilds. Every
Discord action is scoped to one slot: it only touches channels that slot created
inside that slot's category.

Local mode has exactly one slot, built from config.toml.
"""

import asyncio
from dataclasses import dataclass

from proxychat_core.source import LOCAL_SLOT
from proxychat_core.state import Processor


@dataclass(frozen=True)
class GuildSettings:
    guild_id: int
    # Roles that may use /proxychat, besides anyone with Manage Server
    admin_role_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SlotSettings:
    slot_id: str
    guild_id: int
    # An existing category by id (wins), else one found or created by name
    category_id: int = 0
    category: str = "Proxy Chat"
    # Text channel the bot keeps in the category for commands. "" = none
    commands_channel: str = "bot-commands"
    # Optional text channel for "X and Y have established communications". 0 = off
    announce_channel_id: int = 0
    label: str = ""


class Slot:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.processor = Processor(store)
        # The category actually in use (found by id or name), once known
        self.category_id = None
        self.calls = {}         # call channel id -> (frozenset of the two country ids, created at)
        self.arranged = None    # the channel order last applied, to skip the check when nothing changed
        self.dirty = False      # needs a reconcile
        self.refused = None     # why the slot is refused (dangerous permissions), else None
        # Reconcile and the in-game requests both move people and edit channels
        self.lock = asyncio.Lock()

    @property
    def id(self):
        return self.settings.slot_id

    @property
    def guild_id(self):
        return self.settings.guild_id

    def owns(self, channel):
        """A channel this slot created (anywhere: see ProxyChatBot.ours for the category check)."""
        return channel is not None and channel.id in self.store.owned

    def __repr__(self):
        return f"<Slot {self.id} guild={self.guild_id}>"


def local_slot_settings(cfg):
    return SlotSettings(
        slot_id=LOCAL_SLOT,
        guild_id=cfg.guild_id,
        category_id=cfg.category_id,
        category=cfg.category,
        commands_channel=cfg.commands_channel,
        announce_channel_id=cfg.announce_channel_id,
    )


def check_distinct(slots):
    """Two slots must never share a category: each would delete the other's channels' state."""
    seen = {}
    for slot in slots:
        s = slot.settings
        where = (s.guild_id, ("id", s.category_id) if s.category_id else ("name", s.category))
        if where in seen:
            raise ValueError(f"Slots {seen[where]} and {s.slot_id} use the same category in guild {s.guild_id}")
        seen[where] = s.slot_id
