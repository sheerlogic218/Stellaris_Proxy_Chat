"""The bot in server mode: many guilds and slots from the database, fed by relays.

Differences from local mode:
- Guilds must be approved from the server's command line (admin_cli). The bot
  leaves any other guild straight away, and does nothing in a frozen one.
- Slots, admin roles and slot state live in SQLite. Changes made by the CLI or by
  /proxychat commands are picked up within seconds.
- Strict permissions: a guild where the bot's role has dangerous server-wide
  permissions gets no service until they're removed.
- Commands are registered globally. Admins manage slots and admin roles with
  /proxychat slot ... and /proxychat roles ...
- Background upkeep: heartbeat for `admin_cli status`, hourly retention.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands

from proxychat_bot.bot import CATEGORY_PERMS, AdminGroup, ProxyChatBot
from proxychat_bot.slots import GuildSettings, Slot, SlotSettings, check_distinct
from proxychat_core.names import clean_name
from proxychat_core.state import Store

from . import db
from .queue import QueueSource

log = logging.getLogger("proxychat")

RELOAD_SECONDS = 5.0
HEARTBEAT_SECONDS = 30.0
GUILD_CHECK_SECONDS = 600.0
RETENTION_SECONDS = 3600.0
# Old campaigns in a slot (not the current one) are forgotten after this long without events
STALE_GAME_SECONDS = 30 * 86400

def relay_pair_command(public_url, code):
    """What a host runs to pair (proxychat_relay)."""
    local = public_url.startswith(("http://127.0.0.1", "http://localhost"))
    flag = "--insecure-local " if local else ""   # trials on one PC only
    return f'python -m proxychat_relay {flag}pair {public_url} {code} --label "Your name"'


NO_SLOT_SERVER = ("Run this in a channel inside a Proxy Chat category, so the bot knows which game you mean. "
                  "Admins: set one up with `/proxychat slot add`.")


class ServerBot(ProxyChatBot):
    server_mode = True

    def __init__(self, scfg, conn, source=None, dev_guild=None):
        self.conn = conn
        self.scfg = scfg
        # Trials: register commands on this one guild instead of globally
        self.dev_guild = dev_guild
        slots, guilds = self._read_config()
        super().__init__(scfg, source=source or QueueSource(conn, scfg.poll_seconds),
                         slots=slots, guilds=guilds, strict_permissions=True)
        self._config_version = db.config_version(conn)
        for name in scfg.unset:
            log.warning("server.toml: %s is still a placeholder (see docs/DEPLOYMENT_PLACEHOLDERS.md)", name)

    # ---- config from the database ----------------------------------------

    def _read_config(self, existing=None):
        existing = existing or {}
        guilds = [GuildSettings(row["guild_id"], db.admin_roles(row))
                  for row in self.conn.execute("SELECT * FROM guilds WHERE status = 'approved'")]
        slots = []
        for row in db.active_slots(self.conn):
            settings = SlotSettings(
                slot_id=row["slot_id"], guild_id=row["guild_id"], category_id=row["category_id"],
                commands_channel=row["commands_channel"], announce_channel_id=row["announce_channel_id"],
                label=row["label"],
            )
            slot = existing.get(row["slot_id"])
            if slot is None:
                slot = Slot(settings, Store.from_backend(db.SqliteBackend(self.conn, row["slot_id"])))
            elif slot.settings != settings:
                slot.settings = settings
                slot.category_id = None
                slot.arranged = None
            slots.append(slot)
        check_distinct(slots)
        return slots, guilds

    def reload(self):
        """Pick up guild and slot changes made by the CLI or by commands."""
        slots, guilds = self._read_config(self.slots)
        gone = set(self.slots) - {s.id for s in slots}
        self.slots = {s.id: s for s in slots}
        self.guild_configs = {g.guild_id: g for g in guilds}
        self._config_version = db.config_version(self.conn)
        for slot in slots:
            self.wake(slot)
        log.info("Config reloaded: %d guild(s), %d slot(s)%s", len(guilds), len(slots),
                 f", {len(gone)} slot(s) gone" if gone else "")

    def no_slot_text(self):
        return NO_SLOT_SERVER

    # ---- startup and upkeep ----------------------------------------------

    async def setup_hook(self):
        if self.dev_guild:
            guild = discord.Object(self.dev_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.warning("Trial mode: commands registered on guild %s only", self.dev_guild)
        else:
            await self.tree.sync()   # global: every approved guild gets the commands
        asyncio.create_task(self.watch_events())
        asyncio.create_task(self.reconcile_loop())
        asyncio.create_task(self.upkeep_loop())

    async def on_ready(self):
        log.info("Logged in as %s, serving %d slot(s) from %s", self.user, len(self.slots), self.source.describe())
        await self.check_guilds()
        await super().on_ready()

    async def upkeep_loop(self):
        await self.wait_until_ready()
        last = {"heartbeat": 0.0, "guilds": time.monotonic(), "retention": 0.0}
        while True:
            now = time.monotonic()
            try:
                if db.config_version(self.conn) != self._config_version:
                    self.reload()
                if now - last["heartbeat"] >= HEARTBEAT_SECONDS:
                    db.set_setting(self.conn, "bot_heartbeat", db.now())
                    last["heartbeat"] = now
                if now - last["guilds"] >= GUILD_CHECK_SECONDS:
                    await self.check_guilds()
                    last["guilds"] = now
                if now - last["retention"] >= RETENTION_SECONDS:
                    self.run_retention()
                    last["retention"] = now
            except Exception:
                log.exception("Upkeep failed")
            await asyncio.sleep(RELOAD_SECONDS)

    def run_retention(self):
        counts = db.purge(self.conn)
        forgotten = 0
        cutoff = time.time() - STALE_GAME_SECONDS
        for slot in self.slots.values():
            store = slot.store
            changed = False
            for key, game in list(store.games.items()):
                if key == store.current_key:
                    continue
                if game.seen_at is None:
                    game.seen_at = time.time()   # start the clock for games from before this field
                    changed = True
                elif game.seen_at < cutoff:
                    del store.games[key]
                    forgotten += 1
                    changed = True
            if changed:
                store.save()
        if forgotten:
            db.audit(self.conn, "retention", "forget games", None, str(forgotten))
        log.info("Retention: %s, %d old game(s) forgotten", counts, forgotten)

    async def alert(self, text):
        """DM the operator (owner_user_id). Never fails."""
        if not self.scfg.owner_user_id:
            return
        try:
            user = self.get_user(self.scfg.owner_user_id) or await self.fetch_user(self.scfg.owner_user_id)
            await user.send(f"[Proxy Chat] {text}"[:2000])
        except discord.HTTPException:
            log.warning("Could not DM the operator: %s", text)

    # ---- approval gate -----------------------------------------------------

    def allowed_guild(self, guild_id):
        """Approved or frozen: the bot may stay (it only acts in approved ones)."""
        row = db.guild(self.conn, guild_id)
        return row is not None and row["status"] in ("approved", "frozen")

    async def refuse_guild(self, guild):
        if self.dev_guild:
            # A trial shares the real bot's identity: never walk out of a real server
            log.warning("Trial mode: guild %s (%s) isn't approved here; NOT leaving it",
                        guild.id, getattr(guild, "name", "?"))
            return
        log.warning("Leaving guild %s (%s): not approved", guild.id, getattr(guild, "name", "?"))
        db.audit(self.conn, "bot", "left unapproved guild", guild.id, clean_name(getattr(guild, "name", "")))
        await self.alert(f"Someone added the bot to **{clean_name(getattr(guild, 'name', ''), 'a server')}** "
                         f"(`{guild.id}`), which isn't approved, so it left. To allow it: "
                         f"`admin_cli guild approve {guild.id}`, then they invite it again.")
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception("Could not leave guild %s", guild.id)

    async def on_guild_join(self, guild):
        if not self.allowed_guild(guild.id):
            await self.refuse_guild(guild)
        else:
            db.audit(self.conn, "bot", "joined guild", guild.id)

    async def on_guild_remove(self, guild):
        if self.dev_guild:
            return
        # Kicked or the server was deleted: stop serving it; its data goes after retention
        if db.set_guild_status(self.conn, guild.id, "removed", "bot"):
            await self.alert(f"The bot was removed from `{guild.id}`. Its data is deleted in 7 days.")

    async def check_guilds(self):
        for guild in list(self.guilds):
            if not self.allowed_guild(guild.id):
                await self.refuse_guild(guild)

    # ---- slot and role management ------------------------------------------

    async def remove_slot(self, slot, actor):
        """Delete every channel the slot made (its commands channel too), then the slot."""
        async with slot.lock:
            guild = self.get_guild(slot.guild_id)
            deleted = 0
            if guild is not None and await self.ensure_category(guild, slot) is not None:
                for channel_id in sorted(slot.store.owned):
                    if await self.delete_owned(guild, channel_id, slot):
                        deleted += 1
            db.remove_slot(self.conn, slot.id, actor)
        self.reload()
        return deleted

    def guild_slots(self, guild_id):
        return sorted((s for s in self.slots.values() if s.guild_id == guild_id), key=lambda s: s.settings.label)

    def extra_admin_commands(self, admin):
        bot = self

        def actor(interaction):
            return f"discord:{interaction.user.id}"

        async def label_autocomplete(interaction, current):
            return [app_commands.Choice(name=s.settings.label, value=s.settings.label)
                    for s in bot.guild_slots(interaction.guild_id)
                    if current.lower() in s.settings.label.lower()][:25]

        slot_group = AdminGroup(bot, name="slot", description="Set up the categories games are played in")

        @slot_group.command(name="add", description="Make a category a Proxy Chat slot (one game at a time)")
        @app_commands.describe(category="A category the bot's role has Manage Channels, Manage Permissions "
                                        "and Move Members on", label="A short name, e.g. Game 1")
        async def slot_add(interaction: discord.Interaction, category: discord.CategoryChannel, label: str):
            label = clean_name(label, "", 32)
            if not label:
                return await interaction.response.send_message("Give the slot a name.", ephemeral=True)
            try:
                db.add_slot(bot.conn, interaction.guild_id, category.id, label, actor(interaction))
            except db.SlotError as e:
                return await interaction.response.send_message(f"Can't add that slot: {e}.", ephemeral=True)
            bot.reload()
            missing = []
            me = getattr(interaction.guild, "me", None)
            if me is not None and hasattr(category, "permissions_for"):
                have = category.permissions_for(me)
                missing = [p for p in CATEGORY_PERMS if not getattr(have, p)]
            note = (f" The bot is still missing **{', '.join(missing)}** on that category: add them for its "
                    "role in the category's permissions." if missing else "")
            await interaction.response.send_message(
                f"Slot **{label}** set up in **{clean_name(category.name)}**. Its #bot-commands channel "
                f"appears shortly.{note}", ephemeral=True)

        @slot_group.command(name="remove", description="Stop using a category: deletes every channel the bot made there")
        @app_commands.describe(label="Which slot", confirm="Leave off to see what would happen; True to remove it")
        @app_commands.autocomplete(label=label_autocomplete)
        async def slot_remove(interaction: discord.Interaction, label: str, confirm: bool = False):
            slot = next((s for s in bot.guild_slots(interaction.guild_id) if s.settings.label == label), None)
            if slot is None:
                return await interaction.response.send_message("No slot by that name here.", ephemeral=True)
            if not confirm:
                return await interaction.response.send_message(
                    f"Removing **{label}** deletes the {len(slot.store.owned)} channel(s) the bot made in its "
                    "category (its commands channel too) and forgets its games and claims. The category "
                    f"itself stays. Run `/proxychat slot remove label:{label} confirm:True` to go ahead.",
                    ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            deleted = await bot.remove_slot(slot, actor(interaction))
            await interaction.followup.send(f"Removed slot **{label}** and {deleted} channel(s).", ephemeral=True)

        @slot_group.command(name="list", description="Show this server's slots")
        async def slot_list(interaction: discord.Interaction):
            row = db.guild(bot.conn, interaction.guild_id)
            slots = bot.guild_slots(interaction.guild_id)
            lines = [f"Slots: {len(slots)} of {row['max_slots'] if row else 0}"]
            for s in slots:
                game = s.store.current
                lease = db.lease_of(bot.conn, s.id)
                hosted = f"hosted by {clean_name(lease['label'])}" if lease else "no relay connected"
                state = "session ended" if s.store.paused else (f"game `{game.key}`" if game else "no game yet")
                lines.append(f"• **{s.settings.label}**: <#{s.settings.category_id}>, {state}, {hosted}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        roles_group = AdminGroup(bot, name="roles", description="Who besides server managers may use /proxychat")

        async def managers_only(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if perms is not None and perms.manage_guild:
                return True
            await interaction.response.send_message("Only server managers can change the admin roles.", ephemeral=True)
            return False

        def current_roles(guild_id):
            row = db.guild(bot.conn, guild_id)
            return set(db.admin_roles(row)) if row else set()

        @roles_group.command(name="add", description="Let a role use /proxychat")
        async def roles_add(interaction: discord.Interaction, role: discord.Role):
            if not await managers_only(interaction):
                return
            db.set_admin_roles(bot.conn, interaction.guild_id, current_roles(interaction.guild_id) | {role.id},
                               actor(interaction))
            bot.reload()
            await interaction.response.send_message(f"{role.mention} can now use `/proxychat`.", ephemeral=True)

        @roles_group.command(name="remove", description="Stop a role using /proxychat")
        async def roles_remove(interaction: discord.Interaction, role: discord.Role):
            if not await managers_only(interaction):
                return
            db.set_admin_roles(bot.conn, interaction.guild_id, current_roles(interaction.guild_id) - {role.id},
                               actor(interaction))
            bot.reload()
            await interaction.response.send_message(f"{role.mention} can no longer use `/proxychat`.", ephemeral=True)

        @roles_group.command(name="list", description="Show the roles that may use /proxychat")
        async def roles_list(interaction: discord.Interaction):
            roles = sorted(current_roles(interaction.guild_id))
            text = ", ".join(f"<@&{r}>" for r in roles) or "none (only server managers)"
            await interaction.response.send_message(f"Admin roles: {text}", ephemeral=True)

        # ---- relays: pairing and control ----

        @admin.command(name="host", description="Get a one-time code to connect a game host's relay to this slot")
        async def host(interaction: discord.Interaction):
            slot = bot.slot_for(interaction)
            if slot is None:
                return await interaction.response.send_message(bot.no_slot_text(), ephemeral=True)
            if not bot.scfg.public_url:
                return await interaction.response.send_message(
                    "Relays can't connect yet: the operator hasn't set the server's address (`public_url`).",
                    ephemeral=True)
            code = db.new_pairing_code(bot.conn, slot.guild_id, slot.id, interaction.user.id)
            await interaction.response.send_message(
                f"Pairing code for **{slot.settings.label}**: `{code}`\n"
                f"It works once, for {db.PAIRING_SECONDS // 60} minutes. Only you can see this message.\n"
                f"On the PC that hosts the game, run:\n"
                f"```\n{relay_pair_command(bot.scfg.public_url, code)}\n```"
                "Revoke a relay any time with `/proxychat relay revoke`.", ephemeral=True)

        relay_group = AdminGroup(bot, name="relay", description="The game hosts' relays")

        async def relay_autocomplete(interaction, current):
            return [app_commands.Choice(name=f"{clean_name(r['label'])} ({r['slot_label']})"[:100], value=r["key_id"])
                    for r in db.relays(bot.conn, interaction.guild_id)
                    if r["revoked_at"] is None and current.lower() in r["label"].lower()][:25]

        @relay_group.command(name="list", description="Show this server's relays and who is hosting each slot")
        async def relay_list(interaction: discord.Interaction):
            rows = [r for r in db.relays(bot.conn, interaction.guild_id) if r["revoked_at"] is None]
            leases = {s.id: db.lease_of(bot.conn, s.id) for s in bot.guild_slots(interaction.guild_id)}
            lines = [f"Relays: {len(rows)}"]
            for r in rows:
                lease = leases.get(r["slot_id"])
                hosting = " — **hosting now**" if lease and lease["key_id"] == r["key_id"] else ""
                seen = f"<t:{r['last_seen']}:R>" if r["last_seen"] else "never"
                lines.append(f"• **{clean_name(r['label'])}** for {clean_name(r['slot_label'])}, "
                             f"paired by <@{r['paired_by']}>, last seen {seen}{hosting}")
            await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

        @relay_group.command(name="revoke", description="Stop a relay: it has to pair again to send anything")
        @app_commands.describe(relay="Which relay")
        @app_commands.autocomplete(relay=relay_autocomplete)
        async def relay_revoke(interaction: discord.Interaction, relay: str):
            if not db.revoke_relay(bot.conn, relay, actor(interaction), guild_id=interaction.guild_id):
                return await interaction.response.send_message("No active relay by that name here.", ephemeral=True)
            await interaction.response.send_message("Revoked. That relay can't send anything any more.", ephemeral=True)

        @relay_group.command(name="release", description="Free this slot for another relay (if its host crashed)")
        async def relay_release(interaction: discord.Interaction):
            slot = bot.slot_for(interaction)
            if slot is None:
                return await interaction.response.send_message(bot.no_slot_text(), ephemeral=True)
            with db.transaction(bot.conn):
                dropped = db.release_lease(bot.conn, slot.id, actor=actor(interaction))
            text = "Released: another relay can host now." if dropped else "Nobody is hosting this slot."
            await interaction.response.send_message(text, ephemeral=True)

        admin.add_command(slot_group)
        admin.add_command(roles_group)
        admin.add_command(relay_group)
