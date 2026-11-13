"""Discord bot: keeps one embassy voice channel per claimed empire, from the game's events.

It also keeps a channel per federation with a claimed member, open to its members
and associates, and acts on the in-game menus: "Join their Voice Channel" moves the
caller into that empire's embassy, "Join the <federation> Voice Channel" into a
federation's channel, an accepted private call gets a temporary channel for the two
empires that is deleted once it's empty, and the Voice Chat button's menu moves
players back to their own embassy or into the Galactic Community or a federation channel.

The bot serves one or more slots (see slots.py): a category hosting one game. In
local mode there's one, from config.toml, fed by game.log on this PC.

Within a slot's category the bot can share space with the server's own channels:
it only ever deletes, renames or reorders channels that slot created itself
(Store.owned). Its channels sit together at the bottom of the category: the
Galactic Community first, then federations and embassies, each alphabetically,
then any live calls.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands

from proxychat_core.names import clean_name
from proxychat_core.protocol import FederationJoin, GalcomJoin, Home, Join
from proxychat_core.source import LocalLogSource
from proxychat_core.state import (GALCOM_CHANNEL, CountriesChanged, FederationsChanged, GalcomChanged, GameSwitched,
                                  Hailed, PairsChanged, Requested)

from .embassy import GALCOM_NAME, desired_embassies, desired_federations, galcom_users, resolve_occupancy
from .slots import GuildSettings, Slot, check_distinct, local_slot_settings

log = logging.getLogger("proxychat")

# Short, so a channel hides quickly after someone joins it
DEBOUNCE_SECONDS = 0.3
RESYNC_SECONDS = 300.0
# How long an empty call channel is kept, so both players have time to get in
CALL_GRACE_SECONDS = 60.0
# A channel Discord won't let the bot change is reported again this often
REFUSAL_REPEAT_SECONDS = 600.0

EVERYONE = discord.PermissionOverwrite(view_channel=False, connect=False)
SELF = discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True)
MEMBER = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True)
# Voice and stage channels share one ordering within a category
VOICE_TYPES = (discord.ChannelType.voice, discord.ChannelType.stage_voice)
# The only kinds of channel the bot creates, so the only kinds it will ever delete
DELETABLE_TYPES = (discord.ChannelType.voice, discord.ChannelType.text)

# What the bot needs on its category (as a permission overwrite there, not server-wide)
CATEGORY_PERMS = ("view_channel", "manage_channels", "manage_roles", "connect", "speak", "stream", "move_members")
# Server-wide permissions that would let it change things outside its category
RISKY_SERVER_PERMS = ("administrator", "manage_guild", "manage_channels", "manage_roles", "move_members",
                      "kick_members", "ban_members", "manage_messages", "manage_webhooks", "mute_members",
                      "deafen_members", "manage_nicknames", "moderate_members", "manage_events",
                      "manage_expressions", "manage_threads")
# With strict permissions (the central server), any of these server-wide stops the bot
# acting in that guild at all: if its token ever leaked, they'd reach the whole server
REFUSED_SERVER_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
                        "ban_members", "kick_members")


class ProxyChatBot(discord.Client):
    # The central server (ServerBot) sets this: commands for every guild, slots from its database
    server_mode = False

    def __init__(self, cfg, store=None, source=None, *, slots=None, guilds=None, strict_permissions=False):
        """Local mode: cfg and store make the one slot. Server mode passes slots and guilds."""
        # Default intents only (guilds, voice states): no privileged intents needed.
        # No message the bot sends may ping anyone, whatever a game name contains.
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        if slots is None:
            slots = [Slot(local_slot_settings(cfg), store)]
            guilds = [GuildSettings(cfg.guild_id, tuple(cfg.admin_role_ids))]
        check_distinct(slots)
        self.slots = {s.id: s for s in slots}
        self.guild_configs = {g.guild_id: g for g in guilds or ()}
        self.strict_permissions = strict_permissions
        # Where game events come from: game.log on this PC unless told otherwise
        self.source = source or LocalLogSource(cfg.log_path, cfg.poll_seconds)
        self.tree = app_commands.CommandTree(self)
        self.dirty = asyncio.Event()    # wakes the reconcile loop; Slot.dirty says which slots
        self._members = {}      # (guild id, user id) -> member or None
        self._joined = {}       # user id -> when they joined their current channel
        self._refused = {}      # (channel id, action) -> when Discord's refusal was last reported
        register_commands(self)

    # ---- slots ----------------------------------------------------------

    def only_slot(self):
        """The slot, when there's exactly one (local mode)."""
        if len(self.slots) != 1:
            raise RuntimeError(f"{len(self.slots)} slots: say which one")
        return next(iter(self.slots.values()))

    def _slot(self, slot):
        return slot if slot is not None else self.only_slot()

    @property
    def store(self):
        """Local mode's state (the one slot's)."""
        return self.only_slot().store

    def guild_settings(self, guild_id):
        if guild_id in self.guild_configs:
            return self.guild_configs[guild_id]
        if guild_id is None and len(self.guild_configs) == 1:
            return next(iter(self.guild_configs.values()))
        return GuildSettings(guild_id or 0)

    def slot_for(self, interaction):
        """The slot a command is about: the category it's run in, else the guild's only slot."""
        guild_id = getattr(interaction, "guild_id", None)
        candidates = [s for s in self.slots.values() if guild_id is None or s.guild_id == guild_id]
        channel = getattr(interaction, "channel", None)
        category_id = getattr(channel, "category_id", None)
        if category_id:
            for slot in candidates:
                if category_id == self.managed_category_id(slot):
                    return slot
        return candidates[0] if len(candidates) == 1 else None

    def slot_owning(self, channel):
        """The slot that created this channel, if any."""
        if channel is None:
            return None
        return next((s for s in self.slots.values() if s.owns(channel)), None)

    def extra_admin_commands(self, admin):
        """Add mode-specific subcommands to /proxychat (server mode adds slot and role management)."""

    def no_slot_text(self):
        return NO_SLOT

    async def setup_hook(self):
        for guild_id in sorted(self.guild_configs):
            guild = discord.Object(guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        asyncio.create_task(self.watch_events())
        asyncio.create_task(self.reconcile_loop())

    async def on_ready(self):
        log.info("Logged in as %s, watching %s", self.user, self.source.describe())
        for slot in self.slots.values():
            slot.dirty = True
        self.dirty.set()

    # ---- game events ----------------------------------------------------

    async def watch_events(self):
        async for batch in self.source.batches():
            try:
                await self.apply_batch(batch)
            except Exception:
                log.exception("Error applying game events")

    async def apply_batch(self, batch):
        by_slot = {}
        for event in batch.events:
            by_slot.setdefault(event.slot_id, []).append(event)
        for slot_id, events in by_slot.items():
            slot = self.slots.get(slot_id)
            if slot is None:
                log.warning("Ignoring %d event(s) for unknown slot %r", len(events), slot_id)
                continue
            changes = []
            for event in events:
                changes += slot.processor.feed(event.date, event.record)
            if slot.store.current is not None and not batch.replay:
                slot.store.current.seen_at = time.time()
            resumed = False
            if slot.store.paused and not batch.replay:
                # The game is live again after /proxychat session end
                slot.store.paused = False
                resumed = True
                log.info("Slot %s: game is live again, session resumed", slot.id)
            if changes or resumed:
                self.mark_dirty(slot)
                # Replayed records (the existing log on startup) aren't announced again
                if not batch.replay:
                    await self.announce(changes, slot)

    async def announce(self, changes, slot=None):
        slot = self._slot(slot)
        store = slot.store
        for change in changes:
            if isinstance(change, GameSwitched):
                log.info("Slot %s: now tracking game %s", slot.id, change.key)
            elif isinstance(change, PairsChanged):
                game = store.games[change.key]
                for pair in change.added:
                    a, b = (self.country_label(game, c) for c in sorted(pair))
                    log.info("Comms established: %s <-> %s", a, b)
                    await self.post(f"📡 **{a}** and **{b}** have established communications.", slot)
                for pair in change.removed:
                    a, b = (self.country_label(game, c) for c in sorted(pair))
                    log.info("Comms lost: %s <-> %s", a, b)
            elif isinstance(change, CountriesChanged):
                log.info("Player empires updated for game %s", change.key)
            elif isinstance(change, GalcomChanged):
                game = store.games[change.key]
                members = ", ".join(self.country_label(game, c) for c in sorted(game.galcom)) or "none"
                log.info("Galactic Community player members: %s", members)
            elif isinstance(change, FederationsChanged):
                game = store.games[change.key]
                for fed in sorted(game.federations.values(), key=lambda f: f.id):
                    members = ", ".join(self.country_label(game, c) for c in sorted(fed.members)) or "none"
                    assoc = ", ".join(self.country_label(game, c) for c in sorted(fed.associates)) or "none"
                    log.info("Federation %r: players %s; associates %s", fed.name, members, assoc)
            elif isinstance(change, Hailed):
                h = change.hail
                log.info("Call accepted: %s -> %s", h.caller_name, h.target_name)
                await self.handle_call(h.key, h.caller, h.target, slot)
            elif isinstance(change, Requested):
                r = change.record
                game = store.games[r.key]
                if isinstance(r, Join):
                    log.info("%s asked to join the channel of %s",
                             self.country_label(game, r.caller), self.country_label(game, r.target))
                    await self.handle_join(r.key, r.caller, r.target, slot)
                elif isinstance(r, Home):
                    log.info("%s asked to return to their own channel", self.country_label(game, r.country))
                    await self.handle_home(r.key, r.country, slot)
                elif isinstance(r, GalcomJoin):
                    log.info("%s asked to join the Galactic Community channel", self.country_label(game, r.country))
                    await self.handle_galcom(r.key, r.country, slot)
                elif isinstance(r, FederationJoin):
                    log.info("%s asked to join the %s channel", self.country_label(game, r.country),
                             clean_name(r.name, "federation"))
                    await self.handle_federation(r.key, r.country, r.fed, slot)
                else:
                    log.info("%s ignored a call from %s",
                             self.country_label(game, r.target), self.country_label(game, r.caller))

    async def post(self, text, slot=None):
        slot = self._slot(slot)
        channel_id = slot.settings.announce_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        # Only into the slot's own guild, whatever the setting says
        if channel is not None and getattr(getattr(channel, "guild", None), "id", None) == slot.guild_id:
            try:
                await channel.send(text)
            except discord.HTTPException:
                log.exception("Could not post announcement")

    @staticmethod
    def country_label(game, country_id):
        c = game.countries.get(country_id)
        return clean_name(c.name if c else "", f"Empire {country_id}")

    # ---- channel reconciliation -----------------------------------------

    async def reconcile_loop(self):
        await self.wait_until_ready()
        while True:
            resync = False
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=RESYNC_SECONDS)
                await asyncio.sleep(DEBOUNCE_SECONDS)
            except TimeoutError:
                resync = True
            self.dirty.clear()
            for slot in list(self.slots.values()):
                if not (slot.dirty or resync):
                    continue
                slot.dirty = False
                try:
                    async with slot.lock:
                        await self.reconcile(slot)
                except Exception:
                    log.exception("Reconcile failed for slot %s", slot.id)

    async def member(self, guild, user_id):
        key = (guild.id, user_id)
        if key not in self._members:
            m = guild.get_member(user_id)
            if m is None:
                try:
                    m = await guild.fetch_member(user_id)
                except discord.NotFound:
                    m = None
            self._members[key] = m
        return self._members[key]

    async def ensure_category(self, guild, slot=None):
        """The slot's category, or None if the configured one is missing.

        category_id picks an existing category and is never created. Otherwise the
        category is found by name, and created (private) if there isn't one.
        """
        slot = self._slot(slot)
        settings = slot.settings
        if settings.category_id:
            cat = guild.get_channel(settings.category_id)
            if getattr(cat, "type", None) != discord.ChannelType.category:
                log.error("Category %s not found, or the bot can't see it. Check category_id and "
                          "that the bot's role has View Channels on it.", settings.category_id)
                return None
        else:
            cat = next((c for c in guild.categories if c.name == settings.category), None)
            if cat is None:
                log.info("Creating category %r", settings.category)
                cat = await guild.create_category(
                    settings.category, overwrites={guild.default_role: EVERYONE, guild.me: SELF}
                )
        if cat.id != slot.category_id:
            log.info("Slot %s: using category %r (%s)", slot.id, cat.name, cat.id)
            slot.category_id = cat.id
            slot.arranged = None
            self.check_permissions(guild, cat)
        return cat

    def check_permissions(self, guild, category):
        """Log what the bot can do outside its category, and what it's missing inside it."""
        me = getattr(guild.me, "guild_permissions", None)
        if me is not None:
            risky = [p for p in RISKY_SERVER_PERMS if getattr(me, p)]
            if risky:
                log.warning("The bot's server roles allow %s server-wide. It only needs permissions on "
                            "its category; untick these on its role to lock it there.", ", ".join(risky))
            else:
                log.info("Server-wide permissions: none that change anything. Locked to its category.")
        if hasattr(category, "permissions_for"):
            have = category.permissions_for(guild.me)
            missing = [p for p in CATEGORY_PERMS if not getattr(have, p)]
            if missing:
                log.error("Missing on category %r: %s. Add them for the bot's role in the category's "
                          "permissions.", category.name, ", ".join(missing))

    def refused_permissions(self, guild):
        """Server-wide permissions the bot has that strict mode refuses ([] when fine or not strict)."""
        if not self.strict_permissions:
            return []
        me = getattr(guild.me, "guild_permissions", None)
        if me is None:
            return []
        return [p for p in REFUSED_SERVER_PERMS if getattr(me, p)]

    async def permitted(self, guild, slot):
        """Whether the bot may act in this slot. Refusals are reported once, when they start."""
        refused = self.refused_permissions(guild)
        if not refused:
            if slot.refused:
                log.info("Slot %s: dangerous permissions removed, carrying on", slot.id)
            slot.refused = None
            return True
        reason = ", ".join(refused)
        if slot.refused != reason:
            slot.refused = reason
            text = (f"Proxy Chat is paused here: the bot's role has **{reason}** server-wide. It only "
                    "needs permissions on its category. Untick these on the bot's role in Server Settings "
                    "→ Roles, and it carries on by itself.")
            log.error("Slot %s refused: the bot has %s server-wide in guild %s", slot.id, reason, guild.id)
            commands = guild.get_channel(slot.store.commands_channel)
            if commands is not None and self.ours(commands, slot):
                try:
                    await commands.send(text)
                except discord.HTTPException:
                    pass
        return False

    def ours(self, channel, slot=None):
        """A channel the slot created that is still inside its category: the only kind it changes."""
        slot = self._slot(slot)
        return (channel is not None and channel.id in slot.store.owned
                and slot.category_id is not None and channel.category_id == slot.category_id)

    def managed_category_id(self, slot=None):
        """The one category the slot may delete in: the configured id, else the one found by name."""
        slot = self._slot(slot)
        return slot.settings.category_id or slot.category_id

    async def delete_owned(self, guild, channel_id, slot=None):
        """The ONLY place the bot deletes anything. Returns True if it deleted the channel.

        Re-reads the channel from Discord (not the local cache) and refuses unless it
        is a voice or text channel this slot created, in the slot's server, inside the
        slot's category. Never a category, never anything the bot didn't make.
        """
        slot = self._slot(slot)
        category_id = self.managed_category_id(slot)
        if not category_id or channel_id not in slot.store.owned:
            log.warning("Refusing to delete channel %s: not one of ours", channel_id)
            return False
        try:
            fresh = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            slot.store.owned.discard(channel_id)   # already gone
            return False
        checks = {
            "in this server": getattr(getattr(fresh, "guild", None), "id", None) == slot.guild_id,
            "a voice or text channel": fresh.type in DELETABLE_TYPES,
            "inside the bot's category": getattr(fresh, "category_id", None) == category_id,
        }
        failed = [what for what, ok in checks.items() if not ok]
        if failed:
            log.warning("Refusing to delete %r (%s): not %s. Forgetting it instead.",
                        getattr(fresh, "name", "?"), channel_id, ", ".join(failed))
            slot.store.owned.discard(channel_id)
            return False
        try:
            await fresh.delete(reason="Proxy Chat: channel no longer needed")
        except discord.Forbidden as e:
            self.refused(fresh, "delete", e)
            return False
        slot.store.owned.discard(channel_id)
        self._refused = {k: t for k, t in self._refused.items() if k[0] != channel_id}
        return True

    async def create_channel(self, guild, category, name, overwrites, slot=None):
        """Create a voice channel and remember it as the slot's.

        It's created with a copy of the category's permissions, so it carries the
        category's entry for the bot's role (Manage Channels, Manage Permissions...).
        Discord only lets a bot without server-wide rights change or delete a channel
        when it has such an entry in that channel, and it can't add one itself. The
        copy has to be explicit: discord.py always sends a permission list, even an
        empty one, and then Discord doesn't sync the new channel to its category.
        The per-player permissions are applied straight after, keeping that entry.
        If Discord refuses them, the channel is kept (no duplicate next time) and the
        refusal reported.
        """
        slot = self._slot(slot)
        channel = await guild.create_voice_channel(name, category=category, overwrites=dict(category.overwrites))
        slot.store.owned.add(channel.id)
        slot.store.save()
        try:
            await self.apply_overwrites(channel, overwrites)
        except discord.Forbidden as e:
            self.refused(channel, "set permissions on", e)
        return channel

    def refused(self, channel, action, error):
        """Log a permission refusal from Discord, then carry on. Repeats show every 10 minutes."""
        key = (getattr(channel, "id", None), action)
        now = time.monotonic()
        last = self._refused.get(key)
        if last is not None and now - last < REFUSAL_REPEAT_SECONDS:
            log.debug("Still refused: %s %r", action, getattr(channel, "name", "?"))
            return
        self._refused[key] = now
        if last is not None:
            log.warning("Still refused to %s %r: Discord won't let the bot touch it. Delete it by hand, or "
                        "Edit Channel > Permissions > Sync with category.", action, getattr(channel, "name", "?"))
            return
        log.error("Discord refused to %s %r (%s). Check the bot role's permissions on the category. "
                  "A channel created before the permission fix may need deleting by hand, or "
                  "Edit Channel > Permissions > Sync with category.", action, getattr(channel, "name", "?"), error)

    async def ensure_commands_channel(self, guild, category, slot=None):
        """The bot's text channel for commands, created on first start and kept through clean-ups.

        It's created without its own permissions, so it follows the category's: whoever
        the owner lets see the category can see and use it.
        """
        slot = self._slot(slot)
        name = slot.settings.commands_channel
        if not name:
            return None
        store = slot.store
        channel = guild.get_channel(store.commands_channel)
        if channel is not None and self.ours(channel, slot):
            return channel
        if channel is not None:
            # Moved out of the category: leave it alone and make a new one
            store.owned.discard(channel.id)
        log.info("Creating text channel %r", name)
        channel = await guild.create_text_channel(
            name, category=category, overwrites=dict(category.overwrites),
            topic="Proxy Chat: /claim your empire, /empires, /contacts. Admins: /proxychat",
        )
        store.owned.add(channel.id)
        store.commands_channel = channel.id
        store.save()
        return channel

    async def cleanup_plan(self, slot=None):
        """What /proxychat cleanup would do, without doing it.

        Returns (category, [(channel, kind)], claims), or None if the category can't be
        found. The list is exactly the channels cleanup would try to delete: ones the
        slot made that are inside its category, minus the commands channel.
        """
        slot = self._slot(slot)
        store = slot.store
        guild = self.get_guild(slot.guild_id)
        if guild is None:
            return None
        category = await self.ensure_category(guild, slot)
        if category is None:
            return None
        kinds = {}
        for game in store.games.values():
            for key, cid in game.channels.items():
                kinds[cid] = GALCOM_NAME if key == GALCOM_CHANNEL else "Embassy"
            for cid in game.fed_channels.values():
                kinds[cid] = "Federation"
        for cid in slot.calls:
            kinds[cid] = "Private call"
        channels = []
        for channel_id in sorted(store.owned):
            if channel_id == store.commands_channel:
                continue
            channel = guild.get_channel(channel_id)
            if self.ours(channel, slot):
                channels.append((channel, kinds.get(channel_id, "Left over from an earlier game")))
        channels.sort(key=lambda e: (e[1], e[0].name.casefold()))
        claims = sum(len(g.claims) for g in store.games.values())
        return category, channels, claims

    async def cleanup(self, slot=None):
        """Delete every empire, Galactic Community, federation and call channel, and clear all claims.

        The commands channel stays. Known empires and contacts are kept, so players
        can /claim again straight away. Returns (channels deleted, claims cleared).
        """
        slot = self._slot(slot)
        store = slot.store
        async with slot.lock:
            guild = self.get_guild(slot.guild_id)
            if guild is None:
                return 0, 0
            if await self.ensure_category(guild, slot) is None:
                log.error("Clean-up cancelled: the bot's category can't be found, so nothing was touched")
                return None
            claims = 0
            for game in store.games.values():
                claims += len(game.claims)
                game.claims.clear()
                game.channels.clear()
                game.fed_channels.clear()
            slot.calls.clear()
            deleted = 0
            for channel_id in sorted(store.owned):
                if channel_id == store.commands_channel:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    store.owned.discard(channel_id)
                    continue
                log.info("Clean-up: deleting %r", channel.name)
                if await self.delete_owned(guild, channel_id, slot):
                    deleted += 1
            slot.arranged = None
            store.save()
            return deleted, claims

    def clear_claims(self, slot=None):
        """Clear every claim in the slot's current game. Returns how many were cleared.

        The channels go at the next reconcile, as they empty.
        """
        slot = self._slot(slot)
        game = slot.store.current
        if game is None:
            return 0
        cleared = len(game.claims)
        game.claims.clear()
        self.mark_dirty(slot)
        return cleared

    async def end_session(self, clear_claims=False, slot=None):
        """Between sessions: remove the game's voice channels until it's played again.

        The slot is paused: no embassy, federation or Galactic Community channels, and
        in-game requests are ignored. Calls are dropped. Each channel goes now, or once
        the last person leaves it. Claims are kept unless clear_claims, so players don't
        need to /claim again. The game's next live event resumes it. Returns
        (channels to remove, claims cleared).
        """
        slot = self._slot(slot)
        store = slot.store
        async with slot.lock:
            cleared = 0
            if clear_claims and store.current is not None:
                cleared = len(store.current.claims)
                store.current.claims.clear()
            store.paused = True
            slot.calls.clear()
            to_remove = len([c for c in store.owned if c != store.commands_channel])
            store.save()
            log.info("Slot %s: session ended (%d claim(s) cleared)", slot.id, cleared)
        self.mark_dirty(slot)
        return to_remove, cleared

    async def arrange(self, guild, category, ordered, slot=None):
        """Put our channels, in this order, after the server's own voice channels.

        One bulk request, and only when the order is actually wrong.
        """
        slot = self._slot(slot)
        ids = [c.id for c in ordered]
        if ids == slot.arranged:
            return
        siblings = sorted((c for c in category.channels if c.type in VOICE_TYPES), key=lambda c: (c.position, c.id))
        current = [c.id for c in siblings]
        ours = set(ids)
        theirs = [c for c in siblings if c.id not in ours]
        if current != [c.id for c in theirs] + ids:
            # Only our channels move: just past the server's last voice channel
            base = max((c.position for c in theirs), default=-1) + 1
            log.info("Reordering channels in %r", category.name)
            payload = [{"id": cid, "position": base + i} for i, cid in enumerate(ids)]
            try:
                await guild._state.http.bulk_channel_update(guild.id, payload, reason="Proxy Chat channel order")
            except discord.Forbidden as e:
                self.refused(category, "reorder channels in", e)
        slot.arranged = ids

    async def reconcile(self, slot=None):
        slot = self._slot(slot)
        store = slot.store
        guild = self.get_guild(slot.guild_id)
        if guild is None:
            log.warning("Bot is not in guild %s", slot.guild_id)
            return
        if not await self.permitted(guild, slot):
            return
        category = await self.ensure_category(guild, slot)
        if category is None:
            return
        # A paused slot (between sessions) wants no game channels at all
        game = None if store.paused else store.current
        desired = desired_embassies(game)
        embassies = []
        keep = set()
        changed = False

        commands = await self.ensure_commands_channel(guild, category, slot)
        if commands is not None:
            keep.add(commands.id)

        for country, emb in desired.items():
            channel = guild.get_channel(game.channels.get(country, 0))
            exists = getattr(channel, "type", None) == discord.ChannelType.voice and self.ours(channel, slot)

            present = self.occupants(guild, channel) if exists else []
            allowed, kick = resolve_occupancy(game, emb, [m.id for m in present])

            overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
            for user_id in allowed:
                m = await self.member(guild, user_id)
                if m is not None:
                    overwrites[m] = MEMBER

            if not exists:
                log.info("Creating embassy channel %r", emb.name)
                channel = await self.create_channel(guild, category, emb.name, overwrites, slot)
                game.channels[country] = channel.id
                changed = True
            keep.add(channel.id)
            embassies.append((emb.name, channel))   # emb.name: the cache may lag a rename

            try:
                if channel.name != emb.name:
                    log.info("Renaming %r to %r", channel.name, emb.name)
                    await channel.edit(name=emb.name)
                await self.apply_overwrites(channel, overwrites)
                for m in present:
                    if m.id in kick:
                        log.info("Disconnecting %s from %r (no communications with everyone there)", m, emb.name)
                        await m.move_to(None)
            except discord.Forbidden as e:
                self.refused(channel, "update", e)

        galcom = await self.reconcile_galcom(guild, category, game, slot)
        if galcom is not None:
            keep.add(galcom.id)
        federations = await self.reconcile_federations(guild, category, game, slot)
        keep.update(c.id for _, c in federations)

        # Only our own channels are ever deleted; the server's channels are left alone
        now = time.monotonic()
        calls = []
        for channel_id in sorted(store.owned):
            channel = guild.get_channel(channel_id)
            if channel is None:
                store.owned.discard(channel_id)   # deleted by hand
                changed = True
                continue
            if channel_id in keep:
                continue
            if not self.ours(channel, slot):
                # Moved out of the category by someone: not ours to touch any more
                log.warning("Channel %r was moved out of the category; no longer managing it", channel.name)
                store.owned.discard(channel_id)
                slot.calls.pop(channel_id, None)
                changed = True
                continue
            if channel_id in slot.calls:
                _, created = slot.calls[channel_id]
                if self.occupants(guild, channel) or now - created < CALL_GRACE_SECONDS:
                    calls.append(channel)
                    continue
                log.info("Deleting empty call channel %r", channel.name)
                del slot.calls[channel_id]
            elif self.occupants(guild, channel):
                continue   # someone's still in it: wait until it's empty
            else:
                why = "session ended" if store.paused else "not a claimed empire in this game"
                log.info("Deleting channel %r (%s)", channel.name, why)
            if await self.delete_owned(guild, channel_id, slot):
                log.info("Deleted %r", channel.name)
            changed = True
        for channel_id in [c for c in slot.calls if guild.get_channel(c) is None]:
            del slot.calls[channel_id]

        embassies.sort(key=lambda e: (e[0].casefold(), e[1].id))
        federations.sort(key=lambda e: (e[0].casefold(), e[1].id))
        calls.sort(key=lambda c: (c.name.casefold(), c.id))
        ordered = ([galcom] if galcom else []) + [c for _, c in federations] + [c for _, c in embassies] + calls
        await self.arrange(guild, category, [c for c in ordered if self.ours(c, slot)], slot)

        # Forget channel ids for rooms that shouldn't exist (also while paused: their
        # channels go above, or once they empty, and are recreated when it resumes)
        record = store.current
        if record:
            stale = [c for c in record.channels
                     if c not in desired and not (c == GALCOM_CHANNEL and galcom)
                     and not self._still_there(guild, record.channels[c], slot)]
            for c in stale:
                del record.channels[c]
            changed |= bool(stale)
            wanted = desired_federations(game)
            stale = [f for f in record.fed_channels
                     if f not in wanted and not self._still_there(guild, record.fed_channels[f], slot)]
            for f in stale:
                del record.fed_channels[f]
            changed |= bool(stale)
        if changed:
            store.save()

    def _still_there(self, guild, channel_id, slot):
        """An unwanted channel kept only because someone is still in it."""
        channel = guild.get_channel(channel_id)
        return channel is not None and self.ours(channel, slot) and bool(self.occupants(guild, channel))

    async def reconcile_galcom(self, guild, category, game, slot=None):
        """The Galactic Community channel: exists while a member empire is claimed.

        Returns the channel, or None when there shouldn't be one.
        """
        users = galcom_users(game)
        if not users:
            return None
        return await self.reconcile_room(guild, category, game.channels, GALCOM_CHANNEL, GALCOM_NAME, users, slot)

    async def reconcile_federations(self, guild, category, game, slot=None):
        """One channel per federation with a claimed member, for its members and associates.

        Returns [(name, channel)].
        """
        rooms = []
        for fid, room in desired_federations(game).items():
            channel = await self.reconcile_room(guild, category, game.fed_channels, fid, room.name,
                                                room.allowed_users, slot)
            rooms.append((room.name, channel))   # room.name: the cache may lag a rename
        return rooms

    async def reconcile_room(self, guild, category, channels, key, name, users, slot=None):
        """A channel everyone in users may join, with no occupancy rule (Galactic Community, federations).

        channels[key] holds its id. Creates or renames it, and disconnects anyone in it
        who isn't in users any more. Returns the channel.
        """
        slot = self._slot(slot)
        overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
        for user_id in users:
            m = await self.member(guild, user_id)
            if m is not None:
                overwrites[m] = MEMBER
        channel = guild.get_channel(channels.get(key, 0))
        if getattr(channel, "type", None) != discord.ChannelType.voice or not self.ours(channel, slot):
            log.info("Creating %r channel", name)
            channel = await self.create_channel(guild, category, name, overwrites, slot)
            channels[key] = channel.id
            slot.store.save()
        try:
            if channel.name != name:
                log.info("Renaming %r to %r", channel.name, name)
                await channel.edit(name=name)
            await self.apply_overwrites(channel, overwrites)
            for m in self.occupants(guild, channel):
                if m.id not in users:
                    log.info("Disconnecting %s from %r (not a member any more)", m, name)
                    await m.move_to(None)
        except discord.Forbidden as e:
            self.refused(channel, "update", e)
        return channel

    async def apply_overwrites(self, channel, desired):
        # One request per changed entry rather than rewriting the whole channel,
        # since joins and leaves can change these often. The bot's own entries are
        # left exactly as they are: they're what lets it manage the channel. That's
        # its roles' entries and, once the channel has one, its own member entry
        # (server owners may grant the category's rights to the bot user instead of
        # its role; "correcting" that entry would take its own rights away).
        me = channel.guild.me
        own = {r.id for r in getattr(me, "roles", ())} - {channel.guild.default_role.id}
        if any(target.id == me.id for target in channel.overwrites):
            own.add(me.id)
        current = {target.id: (target, ow) for target, ow in channel.overwrites.items() if target.id not in own}
        wanted = {target.id: (target, ow) for target, ow in desired.items() if target.id not in own}
        for tid, (target, ow) in wanted.items():
            if tid not in current or current[tid][1].pair() != ow.pair():
                await channel.set_permissions(target, overwrite=ow)
        for tid, (target, _) in current.items():
            if tid not in wanted:
                await channel.set_permissions(target, overwrite=None)

    def mark_dirty(self, slot=None):
        """Save the slot's state and have it reconciled soon."""
        slot = self._slot(slot)
        slot.store.save()
        self.wake(slot)

    def wake(self, slot):
        slot.dirty = True
        self.dirty.set()

    def occupants(self, guild, channel):
        """Members in a voice channel, earliest joiner first (bot excluded)."""
        others = [m for m in channel.members if m.id != guild.me.id]
        return sorted(others, key=lambda m: (self._joined.get(m.id, 0.0), m.id))

    # ---- in-game requests -------------------------------------------------

    def _request_context(self, slot, key):
        """Guild and game for a request, or (None, None) if it's for a game the slot isn't on."""
        store = slot.store
        guild = self.get_guild(slot.guild_id)
        game = store.games.get(key)
        if guild is None or game is None or key != store.current_key or store.paused:
            return None, None
        if self.refused_permissions(guild):
            return None, None
        return guild, game

    async def tell(self, guild, user_id, text):
        m = await self.member(guild, user_id)
        if m is None:
            return
        try:
            await m.send(text)
        except discord.HTTPException:
            log.info("Could not DM %s: %s", m, text)

    async def bring(self, guild, user_id, channel, what):
        """Move a user into channel if they're in voice, else DM them a link.

        Uses the gateway cache (get_member), which has everyone currently in
        voice with their voice state; members fetched over the API have none.
        """
        m = guild.get_member(user_id)
        if m is None or m.voice is None or m.voice.channel is None:
            await self.tell(guild, user_id, f"{what} Join here: {channel.jump_url}")
            return False
        if m.voice.channel.id != channel.id:
            try:
                await m.move_to(channel)
            except discord.Forbidden:
                # Can't pull them out of a channel the bot has no rights in: send a link instead
                await self.tell(guild, user_id, f"{what} Join here: {channel.jump_url}")
                return False
            self._joined[m.id] = time.monotonic()
        return True

    async def handle_join(self, key, caller, target, slot=None):
        """In-game "Join their Voice Channel": move the caller's players into target's embassy."""
        slot = self._slot(slot)
        async with slot.lock:
            guild, game = self._request_context(slot, key)
            if game is None:
                return
            users = sorted(game.claimers(caller))
            if not users:
                log.info("Join from %s ignored: nobody has claimed it with /claim", self.country_label(game, caller))
                return
            await self.reconcile(slot)   # make sure the channel and its permissions are current
            emb = desired_embassies(game).get(target)
            channel = guild.get_channel(game.channels.get(target, 0))
            name = self.country_label(game, target)
            if emb is None or not self.ours(channel, slot):
                for u in users:
                    await self.tell(guild, u, f"**{name}** has no voice channel: none of their players has used `/claim`.")
                return
            present = [m.id for m in self.occupants(guild, channel)]
            for u in users:
                allowed, _ = resolve_occupancy(game, emb, present)
                if u not in allowed:
                    await self.tell(guild, u, f"You can't join **{name}**'s channel right now: someone in it hasn't met your empire.")
                    continue
                if await self.bring(guild, u, channel, f"You asked to join **{name}**'s voice channel."):
                    present.append(u)
            self.wake(slot)

    async def handle_home(self, key, country, slot=None):
        """Voice Chat menu "Return to my Voice Channel": move the empire's players home."""
        slot = self._slot(slot)
        async with slot.lock:
            guild, game = self._request_context(slot, key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Return home from %s ignored: nobody has claimed it with /claim", self.country_label(game, country))
                return
            await self.reconcile(slot)
            channel = guild.get_channel(game.channels.get(country, 0))
            if not self.ours(channel, slot):
                return
            for u in users:
                await self.bring(guild, u, channel, "Back to your own voice channel.")
            self.wake(slot)

    async def handle_galcom(self, key, country, slot=None):
        """Voice Chat menu "Join the Galactic Community Voice Channel"."""
        slot = self._slot(slot)
        async with slot.lock:
            guild, game = self._request_context(slot, key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Galactic Community join from %s ignored: nobody has claimed it with /claim",
                         self.country_label(game, country))
                return
            await self.reconcile(slot)
            channel = guild.get_channel(game.channels.get(GALCOM_CHANNEL, 0))
            if not self.ours(channel, slot):
                return
            for u in users:
                await self.bring(guild, u, channel, f"You asked to join the {GALCOM_NAME} voice channel.")
            self.wake(slot)

    async def handle_federation(self, key, country, fed, slot=None):
        """Subspace Channel or Voice Chat menu "Join the <federation> Voice Channel"."""
        slot = self._slot(slot)
        async with slot.lock:
            guild, game = self._request_context(slot, key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Federation join from %s ignored: nobody has claimed it with /claim",
                         self.country_label(game, country))
                return
            await self.reconcile(slot)
            room = desired_federations(game).get(fed)
            channel = guild.get_channel(game.fed_channels.get(fed, 0))
            federation = game.federations.get(fed)
            name = room.name if room else clean_name(federation.name if federation else "", "That federation")
            if room is None or not self.ours(channel, slot):
                for u in users:
                    await self.tell(guild, u, f"**{name}** has no voice channel: none of its member empires "
                                              "has a player who used `/claim`.")
                return
            for u in users:
                if u in room.allowed_users:
                    await self.bring(guild, u, channel, f"You asked to join the **{name}** voice channel.")
            self.wake(slot)

    async def handle_call(self, key, caller, target, slot=None):
        """Accepted private call: a temporary channel for the two empires, with both moved in."""
        slot = self._slot(slot)
        async with slot.lock:
            guild, game = self._request_context(slot, key)
            if game is None:
                return
            a, b = self.country_label(game, caller), self.country_label(game, target)
            users = game.claimers(caller) | game.claimers(target)
            if not game.claimers(caller) or not game.claimers(target):
                log.info("Call %s -> %s: both empires need a /claim, no channel made", a, b)
                for u in users:
                    await self.tell(guild, u, f"The call between **{a}** and **{b}** can't connect: both empires need a player who used `/claim`.")
                return
            pair = frozenset((caller, target))
            channel = next(
                (guild.get_channel(cid) for cid, (p, _) in slot.calls.items()
                 if p == pair and self.ours(guild.get_channel(cid), slot)),
                None,
            )
            if channel is None:
                category = await self.ensure_category(guild, slot)
                if category is None:
                    return
                overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
                for u in users:
                    m = await self.member(guild, u)
                    if m is not None:
                        overwrites[m] = MEMBER
                name = f"Call: {a} ↔ {b}"[:100]
                log.info("Creating call channel %r", name)
                channel = await self.create_channel(guild, category, name, overwrites, slot)
            slot.calls[channel.id] = (pair, time.monotonic())
            self.wake(slot)   # reconcile puts it in place at the bottom
            for u in sorted(users):
                await self.bring(guild, u, channel, f"Private call between **{a}** and **{b}**.")

    # ---- voice occupancy ------------------------------------------------

    def in_category(self, channel):
        """Whether channel is one of the bot's (a category may hold the server's own channels too)."""
        return self.slot_owning(channel) is not None

    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return  # mute/deafen etc.
        if self.in_category(after.channel):
            self._joined[member.id] = time.monotonic()
        else:
            self._joined.pop(member.id, None)
        for channel in (before.channel, after.channel):
            slot = self.slot_owning(channel)
            if slot is not None:
                self.wake(slot)


def _country_choice(game, country_id):
    c = game.countries[country_id]
    name = clean_name(c.name, f"Empire {country_id}")
    ruler = clean_name(c.ruler)
    label = f"{name} ({ruler})" if ruler else name
    if not c.human:
        label += " [AI]"
    return label[:100]


def is_admin(settings, user):
    """Manage Server, or one of the guild's admin roles."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.manage_guild:
        return True
    return any(role.id in settings.admin_role_ids for role in getattr(user, "roles", ()))


class AdminGroup(app_commands.Group):
    """/proxychat: checked here, since Discord's own default only knows permissions, not roles."""

    def __init__(self, bot, **kwargs):
        super().__init__(**kwargs)
        self.bot = bot

    async def interaction_check(self, interaction):
        if is_admin(self.bot.guild_settings(getattr(interaction, "guild_id", None)), interaction.user):
            return True
        await interaction.response.send_message(
            "Only server managers and the Proxy Chat admin roles can use `/proxychat`.", ephemeral=True
        )
        return False


def cleanup_preview(category, channels, claims, limit=1900):
    """The /proxychat cleanup preview message."""
    if not channels and not claims:
        return f"Nothing to clean up in **{category.name}**: the bot has no channels there and nobody has claimed an empire."
    head = (f"**/proxychat cleanup** would delete **{len(channels)}** channel(s) in **{category.name}** "
            f"and clear **{claims}** claim(s):")
    lines = [f"• {name_of(c)} ({kind})" for c, kind in channels] or ["• (no channels)"]
    tail = ("Nothing else is touched: only channels the bot created inside its category, never the "
            "server's own channels. Run `/proxychat cleanup confirm:True` to go ahead.")
    body = []
    used = len(head) + len(tail) + 40
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            body.append(f"…and {len(lines) - i} more")
            break
        body.append(line)
        used += len(line) + 1
    return "\n".join([head, *body, "", tail])


def name_of(channel):
    icon = "#" if channel.type == discord.ChannelType.text else "🔊"
    return f"{icon} {channel.name}"


NO_SLOT = ("Run this in a channel inside a Proxy Chat category, so the bot knows which game you mean.")


def register_commands(bot):
    tree = bot.tree

    @tree.error
    async def on_command_error(interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            return   # already answered by the check
        log.error("Command %s failed", getattr(interaction.command, "qualified_name", "?"), exc_info=error)
        text = "Something went wrong running that command. The error is in the bot's console."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def no_slot(interaction):
        await interaction.response.send_message(bot.no_slot_text(), ephemeral=True)

    async def empire_autocomplete(interaction, current):
        slot = bot.slot_for(interaction)
        game = slot.store.current if slot else None
        if game is None:
            return []
        current = current.lower()
        choices = [
            app_commands.Choice(name=_country_choice(game, cid), value=str(cid))
            for cid in sorted(game.countries)
            if current in _country_choice(game, cid).lower()
        ]
        return choices[:25]

    def lookup(slot, empire):
        """Empire by id (what autocomplete sends, or typed) or exact name, case-insensitive."""
        game = slot.store.current
        if game is None:
            return None, None, "No game is being tracked yet. Load a game with the mod and wait for the monthly sweep."
        text = empire.strip()
        if text.isdigit():
            cid = int(text)
        else:
            matches = [c.id for c in game.countries.values() if c.name.lower() == text.lower()]
            cid = matches[0] if len(matches) == 1 else None
        if cid not in game.countries:
            return game, None, "That isn't a player empire in the current game. See `/empires` for names and ids."
        return game, cid, None

    @tree.command(name="empires", description="List the player empires in the current game, with their ids")
    async def empires(interaction: discord.Interaction):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        if game is None or not game.countries:
            return await interaction.response.send_message(
                "No player empires known yet. They appear after the first in-game month with the mod.", ephemeral=True
            )
        lines = [f"Player empires in game `{game.key}` (date {game.last_date or '?'}):"]
        for cid in sorted(game.countries):
            users = ", ".join(f"<@{u}>" for u in sorted(game.claimers(cid))) or "unclaimed"
            lines.append(f"`{cid:>2}` **{_country_choice(game, cid)}**: {users}")
        lines.append("Claim yours with `/claim` (pick from the list, or type the id or exact name).")
        await interaction.response.send_message(
            "\n".join(lines)[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @tree.command(name="claim", description="Link yourself to your empire in the current Stellaris game")
    @app_commands.describe(empire="Your empire")
    @app_commands.autocomplete(empire=empire_autocomplete)
    async def claim(interaction: discord.Interaction, empire: str):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game, cid, error = lookup(slot, empire)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        others = game.claimers(cid) - {interaction.user.id}
        if others:
            return await interaction.response.send_message(
                "That empire is already claimed. Ask an admin to use `/proxychat assign` if you share it.",
                ephemeral=True,
            )
        game.claims[interaction.user.id] = cid
        bot.mark_dirty(slot)
        when = ("It will appear when the game is played again." if slot.store.paused
                else "Your embassy channel will appear shortly.")
        await interaction.response.send_message(
            f"You are now **{_country_choice(game, cid)}**. {when}", ephemeral=True,
        )

    @tree.command(name="unclaim", description="Unlink yourself from your empire")
    async def unclaim(interaction: discord.Interaction):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        if game is None or game.claims.pop(interaction.user.id, None) is None:
            return await interaction.response.send_message("You haven't claimed an empire.", ephemeral=True)
        bot.mark_dirty(slot)
        await interaction.response.send_message("Unclaimed.", ephemeral=True)

    @tree.command(name="contacts", description="Show the empires you have communications with")
    async def contacts(interaction: discord.Interaction):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        cid = game.claims.get(interaction.user.id) if game else None
        if cid is None:
            return await interaction.response.send_message("Claim an empire first with `/claim`.", ephemeral=True)
        names = sorted(bot.country_label(game, c) for c in game.contacts(cid))
        body = "\n".join(f"• {n}" for n in names) or "No communications with other players yet."
        await interaction.response.send_message(
            f"**{bot.country_label(game, cid)}** has communications with:\n{body}", ephemeral=True
        )

    # With admin roles configured, Discord shows the commands to everyone (it can't
    # filter by role on a bot's behalf) and AdminGroup.interaction_check enforces it.
    any_roles = any(g.admin_role_ids for g in bot.guild_configs.values())
    admin = AdminGroup(
        bot,
        name="proxychat",
        description="Proxy chat admin commands",
        default_permissions=None if any_roles or bot.server_mode else discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="status", description="Show the tracked game, player empires, claims and contacts")
    async def status(interaction: discord.Interaction):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        lines = []
        if slot.refused:
            lines.append(f"⚠️ Paused: the bot's role has {slot.refused} server-wide. Untick these on its role.")
        if game is None:
            lines.append("No game tracked yet.")
            return await interaction.response.send_message("\n".join(lines), ephemeral=True)
        state = " (session ended: channels return when the game is played again)" if slot.store.paused else ""
        lines.append(f"Game `{game.key}`, last date {game.last_date or '?'}{state}")
        for cid in sorted(game.countries):
            users = ", ".join(f"<@{u}>" for u in sorted(game.claimers(cid))) or "unclaimed"
            contacts_ = ", ".join(bot.country_label(game, c) for c in sorted(game.contacts(cid))) or "none"
            lines.append(f"**{_country_choice(game, cid)}**: {users}, comms with {contacts_}")
        for fed in sorted(game.federations.values(), key=lambda f: f.name.casefold()):
            members = ", ".join(bot.country_label(game, c) for c in sorted(fed.members)) or "no players"
            assoc = ", ".join(bot.country_label(game, c) for c in sorted(fed.associates))
            fname = clean_name(fed.name, f"Federation {fed.id}")
            lines.append(f"Federation **{fname}**: {members}" + (f"; associates {assoc}" if assoc else ""))
        await interaction.response.send_message(
            "\n".join(lines)[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @admin.command(name="assign", description="Link a user to an empire (also allows shared empires)")
    @app_commands.autocomplete(empire=empire_autocomplete)
    async def assign(interaction: discord.Interaction, user: discord.Member, empire: str):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game, cid, error = lookup(slot, empire)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        game.claims[user.id] = cid
        bot.mark_dirty(slot)
        await interaction.response.send_message(
            f"{user.mention} is now **{_country_choice(game, cid)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="unassign", description="Unlink a user from their empire")
    async def unassign(interaction: discord.Interaction, user: discord.Member):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        if game is None or game.claims.pop(user.id, None) is None:
            return await interaction.response.send_message("That user has no claim.", ephemeral=True)
        bot.mark_dirty(slot)
        await interaction.response.send_message("Unassigned.", ephemeral=True)

    @admin.command(name="cleanup", description="Delete all empire, federation and Galactic Community channels and clear every claim")
    @app_commands.describe(confirm="Leave off to see exactly what would be deleted; True to delete it")
    async def cleanup(interaction: discord.Interaction, confirm: bool = False):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        if not confirm:
            await interaction.response.defer(ephemeral=True, thinking=True)
            plan = await bot.cleanup_plan(slot)
            if plan is None:
                return await interaction.followup.send(
                    "The bot can't find its category, so clean-up wouldn't do anything. Check the bot's console.",
                    ephemeral=True)
            category, channels, claims = plan
            return await interaction.followup.send(cleanup_preview(category, channels, claims), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.cleanup(slot)
        if result is None:
            return await interaction.followup.send(
                "Clean-up cancelled: the bot can't find its category, so nothing was deleted. "
                "Check the bot's console.", ephemeral=True)
        deleted, claims = result
        await interaction.followup.send(
            f"Cleaned up: deleted {deleted} channel(s) and cleared {claims} claim(s). "
            "Players can `/claim` again to get their channels back.",
            ephemeral=True,
        )

    @admin.command(name="resync", description="Re-apply all embassy channels and permissions now")
    async def resync(interaction: discord.Interaction):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        bot._members = {k: v for k, v in bot._members.items() if k[0] != slot.guild_id}
        bot.wake(slot)
        await interaction.response.send_message("Resync queued.", ephemeral=True)

    # ---- between sessions: work while nobody is hosting -----------------

    claims = AdminGroup(bot, name="claims", description="Manage claims between sessions")

    @claims.command(name="clear", description="Clear every claim in the current game")
    @app_commands.describe(confirm="Leave off to see how many claims would be cleared; True to clear them")
    async def claims_clear(interaction: discord.Interaction, confirm: bool = False):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        count = len(game.claims) if game else 0
        if not confirm:
            return await interaction.response.send_message(
                f"This would clear **{count}** claim(s) in game `{game.key if game else '-'}`. Their embassy "
                "channels go once they're empty. Run `/proxychat claims clear confirm:True` to go ahead.",
                ephemeral=True)
        cleared = bot.clear_claims(slot)
        await interaction.response.send_message(
            f"Cleared {cleared} claim(s). Players can `/claim` again.", ephemeral=True)

    session = AdminGroup(bot, name="session", description="Start and end game sessions")

    @session.command(name="end", description="End the session: remove the game's voice channels until it's played again")
    @app_commands.describe(clear_claims="Also clear every claim (default: keep them for next time)",
                           confirm="Leave off to see what would happen; True to end the session")
    async def session_end(interaction: discord.Interaction, clear_claims: bool = False, confirm: bool = False):
        slot = bot.slot_for(interaction)
        if slot is None:
            return await no_slot(interaction)
        game = slot.store.current
        if not confirm:
            kept = "cleared" if clear_claims else "kept, so nobody needs to `/claim` again"
            return await interaction.response.send_message(
                f"Ending the session for game `{game.key if game else '-'}` removes its voice channels (each "
                f"one now, or once it's empty) until the game is played again. Claims are {kept}. "
                "The commands channel stays. Run `/proxychat session end confirm:True` to go ahead.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        to_remove, cleared = await bot.end_session(clear_claims, slot)
        extra = f" Cleared {cleared} claim(s)." if clear_claims else " Claims are kept."
        await interaction.followup.send(
            f"Session ended: {to_remove} channel(s) will be removed.{extra} They come back when the game "
            "sends its next event.", ephemeral=True)

    admin.add_command(claims)
    admin.add_command(session)
    bot.extra_admin_commands(admin)
    tree.add_command(admin)
