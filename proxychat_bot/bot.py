"""Discord bot: tails game.log and keeps one embassy voice channel per claimed empire.

It also keeps a channel per federation with a claimed member, open to its members
and associates, and acts on the in-game menus: "Join their Voice Channel" moves the
caller into that empire's embassy, "Join the <federation> Voice Channel" into a
federation's channel, an accepted private call gets a temporary channel for the two
empires that is deleted once it's empty, and the Voice Chat button's menu moves
players back to their own embassy or into the Galactic Community or a federation channel.

The bot can share a category with the server's own channels: it only ever
deletes, renames or reorders channels it created itself (Store.owned). Its
channels sit together at the bottom of the category: the Galactic Community
first, then federations and embassies, each alphabetically, then any live calls.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands

from proxychat_core.protocol import FederationJoin, GalcomJoin, Home, Join
from proxychat_core.source import LocalLogSource
from proxychat_core.state import (GALCOM_CHANNEL, CountriesChanged, FederationsChanged, GalcomChanged, GameSwitched,
                                  Hailed, PairsChanged, Processor, Requested)

from .embassy import GALCOM_NAME, desired_embassies, desired_federations, galcom_users, resolve_occupancy

log = logging.getLogger("proxychat")

# Short, so a channel hides quickly after someone joins it
DEBOUNCE_SECONDS = 0.3
RESYNC_SECONDS = 300.0
# How long an empty call channel is kept, so both players have time to get in
CALL_GRACE_SECONDS = 60.0

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


class ProxyChatBot(discord.Client):
    def __init__(self, cfg, store, source=None):
        # Default intents only (guilds, voice states): no privileged intents needed
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.store = store
        self.processor = Processor(store)
        # Where game events come from: game.log on this PC unless told otherwise
        self.source = source or LocalLogSource(cfg.log_path, cfg.poll_seconds)
        self.tree = app_commands.CommandTree(self)
        self.dirty = asyncio.Event()
        self._members = {}
        self._joined = {}   # user id -> when they joined their current embassy channel
        self._calls = {}    # call channel id -> (frozenset of the two country ids, created at)
        self._category_id = None
        self._refused = set()   # (channel id, action) already reported as refused by Discord
        self._arranged = None   # the order last applied, to skip the check when nothing changed
        # Reconcile and the in-game requests both move people and edit channels
        self._lock = asyncio.Lock()
        register_commands(self)

    async def setup_hook(self):
        guild = discord.Object(self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        asyncio.create_task(self.watch_events())
        asyncio.create_task(self.reconcile_loop())

    async def on_ready(self):
        log.info("Logged in as %s, watching %s", self.user, self.source.describe())
        self.dirty.set()

    # ---- game.log -------------------------------------------------------

    async def watch_events(self):
        async for batch in self.source.batches():
            try:
                await self.apply_batch(batch)
            except Exception:
                log.exception("Error applying game events")

    async def apply_batch(self, batch):
        changes = []
        for event in batch.events:
            changes += self.processor.feed(event.date, event.record)
        if changes:
            self.store.save()
            self.dirty.set()
            # Replayed records (the existing log on startup) aren't announced again
            if not batch.replay:
                await self.announce(changes)

    async def announce(self, changes):
        for change in changes:
            if isinstance(change, GameSwitched):
                log.info("Now tracking game %s", change.key)
            elif isinstance(change, PairsChanged):
                game = self.store.games[change.key]
                for pair in change.added:
                    a, b = (self.country_label(game, c) for c in sorted(pair))
                    log.info("Comms established: %s <-> %s", a, b)
                    await self.post(f"📡 **{a}** and **{b}** have established communications.")
                for pair in change.removed:
                    a, b = (self.country_label(game, c) for c in sorted(pair))
                    log.info("Comms lost: %s <-> %s", a, b)
            elif isinstance(change, CountriesChanged):
                log.info("Player empires updated for game %s", change.key)
            elif isinstance(change, GalcomChanged):
                game = self.store.games[change.key]
                members = ", ".join(self.country_label(game, c) for c in sorted(game.galcom)) or "none"
                log.info("Galactic Community player members: %s", members)
            elif isinstance(change, FederationsChanged):
                game = self.store.games[change.key]
                for fed in sorted(game.federations.values(), key=lambda f: f.id):
                    members = ", ".join(self.country_label(game, c) for c in sorted(fed.members)) or "none"
                    assoc = ", ".join(self.country_label(game, c) for c in sorted(fed.associates)) or "none"
                    log.info("Federation %r: players %s; associates %s", fed.name, members, assoc)
            elif isinstance(change, Hailed):
                h = change.hail
                log.info("Call accepted: %s -> %s", h.caller_name, h.target_name)
                await self.handle_call(h.key, h.caller, h.target)
            elif isinstance(change, Requested):
                r = change.record
                game = self.store.games[r.key]
                if isinstance(r, Join):
                    log.info("%s asked to join the channel of %s",
                             self.country_label(game, r.caller), self.country_label(game, r.target))
                    await self.handle_join(r.key, r.caller, r.target)
                elif isinstance(r, Home):
                    log.info("%s asked to return to their own channel", self.country_label(game, r.country))
                    await self.handle_home(r.key, r.country)
                elif isinstance(r, GalcomJoin):
                    log.info("%s asked to join the Galactic Community channel", self.country_label(game, r.country))
                    await self.handle_galcom(r.key, r.country)
                elif isinstance(r, FederationJoin):
                    log.info("%s asked to join the %s channel", self.country_label(game, r.country), r.name or "federation")
                    await self.handle_federation(r.key, r.country, r.fed)
                else:
                    log.info("%s ignored a call from %s",
                             self.country_label(game, r.target), self.country_label(game, r.caller))

    async def post(self, text):
        if not self.cfg.announce_channel_id:
            return
        channel = self.get_channel(self.cfg.announce_channel_id)
        if channel is not None:
            try:
                await channel.send(text)
            except discord.HTTPException:
                log.exception("Could not post announcement")

    @staticmethod
    def country_label(game, country_id):
        c = game.countries.get(country_id)
        return c.name if c else f"Empire {country_id}"

    # ---- channel reconciliation -----------------------------------------

    async def reconcile_loop(self):
        await self.wait_until_ready()
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=RESYNC_SECONDS)
                await asyncio.sleep(DEBOUNCE_SECONDS)
            except TimeoutError:
                pass
            self.dirty.clear()
            try:
                async with self._lock:
                    await self.reconcile()
            except Exception:
                log.exception("Reconcile failed")

    async def member(self, guild, user_id):
        if user_id not in self._members:
            m = guild.get_member(user_id)
            if m is None:
                try:
                    m = await guild.fetch_member(user_id)
                except discord.NotFound:
                    m = None
            self._members[user_id] = m
        return self._members[user_id]

    async def ensure_category(self, guild):
        """The category to put channels in, or None if the configured one is missing.

        category_id picks an existing category and is never created. Otherwise the
        category is found by name, and created (private) if there isn't one.
        """
        if self.cfg.category_id:
            cat = guild.get_channel(self.cfg.category_id)
            if getattr(cat, "type", None) != discord.ChannelType.category:
                log.error("Category %s not found, or the bot can't see it. Check category_id and "
                          "that the bot's role has View Channels on it.", self.cfg.category_id)
                return None
        else:
            cat = next((c for c in guild.categories if c.name == self.cfg.category), None)
            if cat is None:
                log.info("Creating category %r", self.cfg.category)
                cat = await guild.create_category(
                    self.cfg.category, overwrites={guild.default_role: EVERYONE, guild.me: SELF}
                )
        if cat.id != self._category_id:
            log.info("Using category %r (%s)", cat.name, cat.id)
            self._category_id = cat.id
            self._arranged = None
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

    def ours(self, channel):
        """A channel the bot created and that is still inside its category: the only kind it changes."""
        return (channel is not None and channel.id in self.store.owned
                and self._category_id is not None and channel.category_id == self._category_id)

    def managed_category_id(self):
        """The one category the bot may delete in: the configured id, else the one found by name."""
        return self.cfg.category_id or self._category_id

    async def delete_owned(self, guild, channel_id):
        """The ONLY place the bot deletes anything. Returns True if it deleted the channel.

        Re-reads the channel from Discord (not the local cache) and refuses unless it
        is a voice or text channel the bot created, in this server, inside the bot's
        category. Never a category, never anything the bot didn't make.
        """
        category_id = self.managed_category_id()
        if not category_id or channel_id not in self.store.owned:
            log.warning("Refusing to delete channel %s: not one of ours", channel_id)
            return False
        try:
            fresh = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            self.store.owned.discard(channel_id)   # already gone
            return False
        checks = {
            "in this server": getattr(getattr(fresh, "guild", None), "id", None) == self.cfg.guild_id,
            "a voice or text channel": fresh.type in DELETABLE_TYPES,
            "inside the bot's category": getattr(fresh, "category_id", None) == category_id,
        }
        failed = [what for what, ok in checks.items() if not ok]
        if failed:
            log.warning("Refusing to delete %r (%s): not %s. Forgetting it instead.",
                        getattr(fresh, "name", "?"), channel_id, ", ".join(failed))
            self.store.owned.discard(channel_id)
            return False
        try:
            await fresh.delete(reason="Proxy Chat: channel no longer needed")
        except discord.Forbidden as e:
            self.refused(fresh, "delete", e)
            return False
        self.store.owned.discard(channel_id)
        self._refused = {k for k in self._refused if k[0] != channel_id}
        return True

    async def create_channel(self, guild, category, name, overwrites):
        """Create a voice channel and remember it as ours.

        It's created synced to the category, so it inherits the category's entry for
        the bot's role (Manage Channels, Manage Permissions...). Discord only lets a
        bot without server-wide rights change a channel's permissions when it has such
        an entry in that channel, and it can't add one itself. The per-player
        permissions are applied straight after, keeping the bot role's entry.
        """
        channel = await guild.create_voice_channel(name, category=category)
        self.store.owned.add(channel.id)
        self.store.save()
        await self.apply_overwrites(channel, overwrites)
        return channel

    def refused(self, channel, action, error):
        """Log a permission refusal from Discord once per channel and action, then carry on."""
        key = (getattr(channel, "id", None), action)
        if key in self._refused:
            log.debug("Still refused: %s %r", action, getattr(channel, "name", "?"))
            return
        self._refused.add(key)
        log.error("Discord refused to %s %r (%s). Check the bot role's permissions on the category. "
                  "A channel created before the permission fix may need deleting by hand, or "
                  "Edit Channel > Permissions > Sync with category.", action, getattr(channel, "name", "?"), error)

    async def ensure_commands_channel(self, guild, category):
        """The bot's text channel for commands, created on first start and kept through clean-ups.

        It's created without its own permissions, so it follows the category's: whoever
        the owner lets see the category can see and use it.
        """
        if not self.cfg.commands_channel:
            return None
        channel = guild.get_channel(self.store.commands_channel)
        if channel is not None and self.ours(channel):
            return channel
        if channel is not None:
            # Moved out of the category: leave it alone and make a new one
            self.store.owned.discard(channel.id)
        log.info("Creating text channel %r", self.cfg.commands_channel)
        channel = await guild.create_text_channel(
            self.cfg.commands_channel, category=category,
            topic="Proxy Chat: /claim your empire, /empires, /contacts. Admins: /proxychat",
        )
        self.store.owned.add(channel.id)
        self.store.commands_channel = channel.id
        self.store.save()
        return channel

    async def cleanup_plan(self):
        """What /proxychat cleanup would do, without doing it.

        Returns (category, [(channel, kind)], claims), or None if the category can't be
        found. The list is exactly the channels cleanup would try to delete: ones the
        bot made that are inside its category, minus the commands channel.
        """
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            return None
        category = await self.ensure_category(guild)
        if category is None:
            return None
        kinds = {}
        for game in self.store.games.values():
            for key, cid in game.channels.items():
                kinds[cid] = GALCOM_NAME if key == GALCOM_CHANNEL else "Embassy"
            for cid in game.fed_channels.values():
                kinds[cid] = "Federation"
        for cid in self._calls:
            kinds[cid] = "Private call"
        channels = []
        for channel_id in sorted(self.store.owned):
            if channel_id == self.store.commands_channel:
                continue
            channel = guild.get_channel(channel_id)
            if self.ours(channel):
                channels.append((channel, kinds.get(channel_id, "Left over from an earlier game")))
        channels.sort(key=lambda e: (e[1], e[0].name.casefold()))
        claims = sum(len(g.claims) for g in self.store.games.values())
        return category, channels, claims

    async def cleanup(self):
        """Delete every empire, Galactic Community, federation and call channel, and clear all claims.

        The commands channel stays. Known empires and contacts are kept, so players
        can /claim again straight away. Returns (channels deleted, claims cleared).
        """
        async with self._lock:
            guild = self.get_guild(self.cfg.guild_id)
            if guild is None:
                return 0, 0
            if await self.ensure_category(guild) is None:
                log.error("Clean-up cancelled: the bot's category can't be found, so nothing was touched")
                return None
            claims = 0
            for game in self.store.games.values():
                claims += len(game.claims)
                game.claims.clear()
                game.channels.clear()
                game.fed_channels.clear()
            self._calls.clear()
            deleted = 0
            for channel_id in sorted(self.store.owned):
                if channel_id == self.store.commands_channel:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    self.store.owned.discard(channel_id)
                    continue
                log.info("Clean-up: deleting %r", channel.name)
                if await self.delete_owned(guild, channel_id):
                    deleted += 1
            self._arranged = None
            self.store.save()
            return deleted, claims

    async def arrange(self, guild, category, ordered):
        """Put our channels, in this order, after the server's own voice channels.

        One bulk request, and only when the order is actually wrong.
        """
        ids = [c.id for c in ordered]
        if ids == self._arranged:
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
        self._arranged = ids

    async def reconcile(self):
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            log.warning("Bot is not in guild %s", self.cfg.guild_id)
            return
        category = await self.ensure_category(guild)
        if category is None:
            return
        game = self.store.current
        desired = desired_embassies(game)
        embassies = []
        keep = set()
        changed = False

        commands = await self.ensure_commands_channel(guild, category)
        if commands is not None:
            keep.add(commands.id)

        for country, emb in desired.items():
            channel = guild.get_channel(game.channels.get(country, 0))
            exists = getattr(channel, "type", None) == discord.ChannelType.voice and self.ours(channel)

            present = self.occupants(guild, channel) if exists else []
            allowed, kick = resolve_occupancy(game, emb, [m.id for m in present])

            overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
            for user_id in allowed:
                m = await self.member(guild, user_id)
                if m is not None:
                    overwrites[m] = MEMBER

            if not exists:
                log.info("Creating embassy channel %r", emb.name)
                channel = await self.create_channel(guild, category, emb.name, overwrites)
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

        galcom = await self.reconcile_galcom(guild, category, game)
        if galcom is not None:
            keep.add(galcom.id)
        federations = await self.reconcile_federations(guild, category, game)
        keep.update(c.id for _, c in federations)

        # Only our own channels are ever deleted; the server's channels are left alone
        now = time.monotonic()
        calls = []
        for channel_id in sorted(self.store.owned):
            channel = guild.get_channel(channel_id)
            if channel is None:
                self.store.owned.discard(channel_id)   # deleted by hand
                changed = True
                continue
            if channel_id in keep:
                continue
            if not self.ours(channel):
                # Moved out of the category by someone: not ours to touch any more
                log.warning("Channel %r was moved out of the category; no longer managing it", channel.name)
                self.store.owned.discard(channel_id)
                self._calls.pop(channel_id, None)
                changed = True
                continue
            if channel_id in self._calls:
                _, created = self._calls[channel_id]
                if self.occupants(guild, channel) or now - created < CALL_GRACE_SECONDS:
                    calls.append(channel)
                    continue
                log.info("Deleting empty call channel %r", channel.name)
                del self._calls[channel_id]
            elif self.occupants(guild, channel):
                continue   # someone's still in it: wait until it's empty
            else:
                log.info("Deleting channel %r (not a claimed empire in this game)", channel.name)
            await self.delete_owned(guild, channel_id)
            changed = True
        for channel_id in [c for c in self._calls if guild.get_channel(c) is None]:
            del self._calls[channel_id]

        embassies.sort(key=lambda e: (e[0].casefold(), e[1].id))
        federations.sort(key=lambda e: (e[0].casefold(), e[1].id))
        calls.sort(key=lambda c: (c.name.casefold(), c.id))
        ordered = ([galcom] if galcom else []) + [c for _, c in federations] + [c for _, c in embassies] + calls
        await self.arrange(guild, category, [c for c in ordered if self.ours(c)])

        if game:
            stale = [c for c in game.channels if c not in desired and not (c == GALCOM_CHANNEL and galcom)]
            for c in stale:
                del game.channels[c]
            changed |= bool(stale)
            wanted = desired_federations(game)
            stale = [f for f in game.fed_channels if f not in wanted]
            for f in stale:
                del game.fed_channels[f]
            changed |= bool(stale)
        if changed:
            self.store.save()

    async def reconcile_galcom(self, guild, category, game):
        """The Galactic Community channel: exists while a member empire is claimed.

        Returns the channel, or None when there shouldn't be one.
        """
        users = galcom_users(game)
        if not users:
            return None
        return await self.reconcile_room(guild, category, game.channels, GALCOM_CHANNEL, GALCOM_NAME, users)

    async def reconcile_federations(self, guild, category, game):
        """One channel per federation with a claimed member, for its members and associates.

        Returns [(name, channel)].
        """
        rooms = []
        for fid, room in desired_federations(game).items():
            channel = await self.reconcile_room(guild, category, game.fed_channels, fid, room.name, room.allowed_users)
            rooms.append((room.name, channel))   # room.name: the cache may lag a rename
        return rooms

    async def reconcile_room(self, guild, category, channels, key, name, users):
        """A channel everyone in users may join, with no occupancy rule (Galactic Community, federations).

        channels[key] holds its id. Creates or renames it, and disconnects anyone in it
        who isn't in users any more. Returns the channel.
        """
        overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
        for user_id in users:
            m = await self.member(guild, user_id)
            if m is not None:
                overwrites[m] = MEMBER
        channel = guild.get_channel(channels.get(key, 0))
        if getattr(channel, "type", None) != discord.ChannelType.voice or not self.ours(channel):
            log.info("Creating %r channel", name)
            channel = await self.create_channel(guild, category, name, overwrites)
            channels[key] = channel.id
            self.store.save()
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
        # since joins and leaves can change these often. Entries for the bot's own
        # roles are left exactly as they are: they're what lets it manage the channel.
        me = channel.guild.me
        own_roles = {r.id for r in getattr(me, "roles", ())} - {channel.guild.default_role.id}
        current = {target.id: (target, ow) for target, ow in channel.overwrites.items() if target.id not in own_roles}
        wanted = {target.id: (target, ow) for target, ow in desired.items() if target.id not in own_roles}
        for tid, (target, ow) in wanted.items():
            if tid not in current or current[tid][1].pair() != ow.pair():
                await channel.set_permissions(target, overwrite=ow)
        for tid, (target, _) in current.items():
            if tid not in wanted:
                await channel.set_permissions(target, overwrite=None)

    def mark_dirty(self):
        self.store.save()
        self.dirty.set()

    def occupants(self, guild, channel):
        """Members in a voice channel, earliest joiner first (bot excluded)."""
        others = [m for m in channel.members if m.id != guild.me.id]
        return sorted(others, key=lambda m: (self._joined.get(m.id, 0.0), m.id))

    # ---- in-game requests -------------------------------------------------

    def _request_context(self, key):
        """Guild and game for a request, or (None, None) if it's for a game we aren't on."""
        guild = self.get_guild(self.cfg.guild_id)
        game = self.store.games.get(key)
        if guild is None or game is None or key != self.store.current_key:
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

    async def handle_join(self, key, caller, target):
        """In-game "Join their Voice Channel": move the caller's players into target's embassy."""
        async with self._lock:
            guild, game = self._request_context(key)
            if game is None:
                return
            users = sorted(game.claimers(caller))
            if not users:
                log.info("Join from %s ignored: nobody has claimed it with /claim", self.country_label(game, caller))
                return
            await self.reconcile()   # make sure the channel and its permissions are current
            emb = desired_embassies(game).get(target)
            channel = guild.get_channel(game.channels.get(target, 0))
            name = self.country_label(game, target)
            if emb is None or not self.ours(channel):
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
            self.dirty.set()

    async def handle_home(self, key, country):
        """Voice Chat menu "Return to my Voice Channel": move the empire's players home."""
        async with self._lock:
            guild, game = self._request_context(key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Return home from %s ignored: nobody has claimed it with /claim", self.country_label(game, country))
                return
            await self.reconcile()
            channel = guild.get_channel(game.channels.get(country, 0))
            if not self.ours(channel):
                return
            for u in users:
                await self.bring(guild, u, channel, "Back to your own voice channel.")
            self.dirty.set()

    async def handle_galcom(self, key, country):
        """Voice Chat menu "Join the Galactic Community Voice Channel"."""
        async with self._lock:
            guild, game = self._request_context(key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Galactic Community join from %s ignored: nobody has claimed it with /claim",
                         self.country_label(game, country))
                return
            await self.reconcile()
            channel = guild.get_channel(game.channels.get(GALCOM_CHANNEL, 0))
            if not self.ours(channel):
                return
            for u in users:
                await self.bring(guild, u, channel, f"You asked to join the {GALCOM_NAME} voice channel.")
            self.dirty.set()

    async def handle_federation(self, key, country, fed):
        """Subspace Channel or Voice Chat menu "Join the <federation> Voice Channel"."""
        async with self._lock:
            guild, game = self._request_context(key)
            if game is None:
                return
            users = sorted(game.claimers(country))
            if not users:
                log.info("Federation join from %s ignored: nobody has claimed it with /claim",
                         self.country_label(game, country))
                return
            await self.reconcile()
            room = desired_federations(game).get(fed)
            channel = guild.get_channel(game.fed_channels.get(fed, 0))
            federation = game.federations.get(fed)
            name = room.name if room else (federation.name if federation and federation.name else "That federation")
            if room is None or not self.ours(channel):
                for u in users:
                    await self.tell(guild, u, f"**{name}** has no voice channel: none of its member empires "
                                              "has a player who used `/claim`.")
                return
            for u in users:
                if u in room.allowed_users:
                    await self.bring(guild, u, channel, f"You asked to join the **{name}** voice channel.")
            self.dirty.set()

    async def handle_call(self, key, caller, target):
        """Accepted private call: a temporary channel for the two empires, with both moved in."""
        async with self._lock:
            guild, game = self._request_context(key)
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
                (guild.get_channel(cid) for cid, (p, _) in self._calls.items() if p == pair and self.ours(guild.get_channel(cid))),
                None,
            )
            if channel is None:
                category = await self.ensure_category(guild)
                if category is None:
                    return
                overwrites = {guild.default_role: EVERYONE, guild.me: SELF}
                for u in users:
                    m = await self.member(guild, u)
                    if m is not None:
                        overwrites[m] = MEMBER
                name = f"Call: {a} \u2194 {b}"[:100]
                log.info("Creating call channel %r", name)
                channel = await self.create_channel(guild, category, name, overwrites)
            self._calls[channel.id] = (pair, time.monotonic())
            self.dirty.set()   # reconcile puts it in place at the bottom
            for u in sorted(users):
                await self.bring(guild, u, channel, f"Private call between **{a}** and **{b}**.")

    # ---- voice occupancy ------------------------------------------------

    def in_category(self, channel):
        """Whether channel is one of ours (the category may hold the server's own channels too)."""
        return channel is not None and channel.id in self.store.owned

    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return  # mute/deafen etc.
        if self.in_category(after.channel):
            self._joined[member.id] = time.monotonic()
        else:
            self._joined.pop(member.id, None)
        if self.in_category(before.channel) or self.in_category(after.channel):
            self.dirty.set()


def _country_choice(game, country_id):
    c = game.countries[country_id]
    label = f"{c.name} ({c.ruler})" if c.ruler else c.name
    if not c.human:
        label += " [AI]"
    return label[:100]


def is_admin(cfg, user):
    """Manage Server, or one of the configured admin roles."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.manage_guild:
        return True
    return any(role.id in cfg.admin_role_ids for role in getattr(user, "roles", ()))


class AdminGroup(app_commands.Group):
    """/proxychat: checked here, since Discord's own default only knows permissions, not roles."""

    def __init__(self, bot, **kwargs):
        super().__init__(**kwargs)
        self.bot = bot

    async def interaction_check(self, interaction):
        if is_admin(self.bot.cfg, interaction.user):
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

    async def empire_autocomplete(interaction, current):
        game = bot.store.current
        if game is None:
            return []
        current = current.lower()
        choices = [
            app_commands.Choice(name=_country_choice(game, cid), value=str(cid))
            for cid in sorted(game.countries)
            if current in _country_choice(game, cid).lower()
        ]
        return choices[:25]

    def lookup(empire):
        """Empire by id (what autocomplete sends, or typed) or exact name, case-insensitive."""
        game = bot.store.current
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
        game = bot.store.current
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
        game, cid, error = lookup(empire)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        others = game.claimers(cid) - {interaction.user.id}
        if others:
            return await interaction.response.send_message(
                "That empire is already claimed. Ask an admin to use `/proxychat assign` if you share it.",
                ephemeral=True,
            )
        game.claims[interaction.user.id] = cid
        bot.mark_dirty()
        await interaction.response.send_message(
            f"You are now **{game.countries[cid].name}**. Your embassy channel will appear shortly.",
            ephemeral=True,
        )

    @tree.command(name="unclaim", description="Unlink yourself from your empire")
    async def unclaim(interaction: discord.Interaction):
        game = bot.store.current
        if game is None or game.claims.pop(interaction.user.id, None) is None:
            return await interaction.response.send_message("You haven't claimed an empire.", ephemeral=True)
        bot.mark_dirty()
        await interaction.response.send_message("Unclaimed.", ephemeral=True)

    @tree.command(name="contacts", description="Show the empires you have communications with")
    async def contacts(interaction: discord.Interaction):
        game = bot.store.current
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
    admin = AdminGroup(
        bot,
        name="proxychat",
        description="Proxy chat admin commands",
        default_permissions=None if bot.cfg.admin_role_ids else discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @admin.command(name="status", description="Show the tracked game, player empires, claims and contacts")
    async def status(interaction: discord.Interaction):
        game = bot.store.current
        if game is None:
            return await interaction.response.send_message("No game tracked yet.", ephemeral=True)
        lines = [f"Game `{game.key}`, last date {game.last_date or '?'}"]
        for cid in sorted(game.countries):
            users = ", ".join(f"<@{u}>" for u in sorted(game.claimers(cid))) or "unclaimed"
            contacts_ = ", ".join(bot.country_label(game, c) for c in sorted(game.contacts(cid))) or "none"
            lines.append(f"**{_country_choice(game, cid)}**: {users}, comms with {contacts_}")
        for fed in sorted(game.federations.values(), key=lambda f: f.name.casefold()):
            members = ", ".join(bot.country_label(game, c) for c in sorted(fed.members)) or "no players"
            assoc = ", ".join(bot.country_label(game, c) for c in sorted(fed.associates))
            lines.append(f"Federation **{fed.name or fed.id}**: {members}" + (f"; associates {assoc}" if assoc else ""))
        await interaction.response.send_message(
            "\n".join(lines)[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @admin.command(name="assign", description="Link a user to an empire (also allows shared empires)")
    @app_commands.autocomplete(empire=empire_autocomplete)
    async def assign(interaction: discord.Interaction, user: discord.Member, empire: str):
        game, cid, error = lookup(empire)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        game.claims[user.id] = cid
        bot.mark_dirty()
        await interaction.response.send_message(
            f"{user.mention} is now **{game.countries[cid].name}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="unassign", description="Unlink a user from their empire")
    async def unassign(interaction: discord.Interaction, user: discord.Member):
        game = bot.store.current
        if game is None or game.claims.pop(user.id, None) is None:
            return await interaction.response.send_message("That user has no claim.", ephemeral=True)
        bot.mark_dirty()
        await interaction.response.send_message("Unassigned.", ephemeral=True)

    @admin.command(name="cleanup", description="Delete all empire, federation and Galactic Community channels and clear every claim")
    @app_commands.describe(confirm="Leave off to see exactly what would be deleted; True to delete it")
    async def cleanup(interaction: discord.Interaction, confirm: bool = False):
        if not confirm:
            await interaction.response.defer(ephemeral=True, thinking=True)
            plan = await bot.cleanup_plan()
            if plan is None:
                return await interaction.followup.send(
                    "The bot can't find its category, so clean-up wouldn't do anything. Check the bot's console.",
                    ephemeral=True)
            category, channels, claims = plan
            return await interaction.followup.send(cleanup_preview(category, channels, claims), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.cleanup()
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
        bot._members.clear()
        bot.dirty.set()
        await interaction.response.send_message("Resync queued.", ephemeral=True)

    tree.add_command(admin)
