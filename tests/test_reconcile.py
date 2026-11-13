"""Runs ProxyChatBot.reconcile against an in-memory fake guild (no Discord connection)."""

import asyncio
import dataclasses
import itertools
from pathlib import Path

import discord

from proxychat_bot.bot import ProxyChatBot
from proxychat_bot.config import Config
from proxychat_core.state import Country, Store

K = "123-456-789"
_ids = itertools.count(1000)


class FakeVoice:
    def __init__(self, channel):
        self.channel = channel


class FakeMember:
    def __init__(self, id):
        self.id = id
        self.voice_channel = None
        self.dms = []

    @property
    def voice(self):
        return FakeVoice(self.voice_channel) if self.voice_channel else None

    async def move_to(self, channel):
        if self.voice_channel:
            self.voice_channel.members.remove(self)
        self.voice_channel = channel
        if channel is not None:
            channel.members.append(self)

    async def send(self, text):
        self.dms.append(text)

    def __repr__(self):
        return f"<member {self.id}>"


class FakeChannel:
    def __init__(self, guild, name, type, category_id=None, overwrites=None):
        self.guild, self.id, self.name, self.type = guild, next(_ids), name, type
        self.position = self.id   # new channels go to the bottom, like Discord
        self.category_id = category_id
        self.overwrites = dict(overwrites or {})
        self.members = []
        self.edits = 0
        self.jump_url = f"https://discord.com/channels/5/{self.id}"

    async def edit(self, name=None):
        self.edits += 1
        self.name = name

    async def set_permissions(self, target, overwrite=None):
        self.edits += 1
        self.overwrites = {t: ow for t, ow in self.overwrites.items() if t.id != target.id}
        if overwrite is not None:
            self.overwrites[target] = overwrite

    def join(self, member):
        if member.voice_channel:
            member.voice_channel.members.remove(member)
        member.voice_channel = self
        self.members.append(member)

    async def delete(self, reason=None):
        del self.guild.channels[self.id]
        self.guild.deleted.append(self.name)

    @property
    def channels(self):
        return [c for c in self.guild.channels.values() if c.category_id == self.id]

    @property
    def voice_channels(self):
        return [c for c in self.guild.channels.values() if c.category_id == self.id and c.type == discord.ChannelType.voice]


class FakeHttp:
    def __init__(self, guild):
        self.guild = guild
        self.reorders = 0

    async def bulk_channel_update(self, guild_id, payload, reason=None):
        self.reorders += 1
        for entry in payload:
            self.guild.channels[entry["id"]].position = entry["position"]


class FakeGuild:
    def __init__(self, member_ids):
        self.id = 5
        self._state = type("State", (), {})()
        self._state.http = FakeHttp(self)
        self.default_role = discord.Object(1)
        self.deleted = []
        self.me = FakeMember(999)
        self.me.roles = [self.default_role]
        self.members = {i: FakeMember(i) for i in member_ids}
        self.channels = {}

    @property
    def categories(self):
        return [c for c in self.channels.values() if c.type == discord.ChannelType.category]

    def get_channel(self, id):
        return self.channels.get(id)

    def _add(self, ch):
        self.channels[ch.id] = ch
        return ch

    async def create_category(self, name, overwrites=None):
        return self._add(FakeChannel(self, name, discord.ChannelType.category, overwrites=overwrites))

    async def create_text_channel(self, name, category, topic=None):
        # No overwrites given: like Discord, it takes the category's
        ch = FakeChannel(self, name, discord.ChannelType.text, category_id=category.id, overwrites=category.overwrites)
        ch.topic = topic
        return self._add(ch)

    async def create_voice_channel(self, name, category, overwrites=None):
        # Like Discord: no overwrites given means synced with the category
        inherited = category.overwrites if overwrites is None else overwrites
        return self._add(FakeChannel(self, name, discord.ChannelType.voice, category_id=category.id, overwrites=inherited))

    async def fetch_channel(self, id):
        if id not in self.channels:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "unknown channel")
        return self.channels[id]

    def get_member(self, id):
        return self.members.get(id)

    async def fetch_member(self, id):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "unknown member")


def make_bot(guild, tmp_path):
    cfg = Config("token", 5, "Proxy Chat", 0, Path("game.log"), tmp_path / "state.json", 1.0)
    store = Store(cfg.state_path)
    store.current_key = K
    game = store.game(K)
    for cid, name in [(1, "Rocks"), (2, "Tzynn Empire"), (3, "Glebsig")]:
        game.countries[cid] = Country(cid, name, "ruler", True)
    bot = ProxyChatBot(cfg, store)
    bot.get_guild = lambda id: guild
    return bot, game


def set_slot(bot, **settings):
    """Change the (local) slot's settings, as editing config.toml and restarting would."""
    slot = bot.only_slot()
    slot.settings = dataclasses.replace(slot.settings, **settings)


def allowed(channel):
    return {t.id for t, ow in channel.overwrites.items() if ow.connect}


def embassy(guild, name):
    return next(c for c in guild.channels.values() if c.type == discord.ChannelType.voice and c.name == name)


def test_reconcile_creates_gates_and_cleans_up(tmp_path):
    guild = FakeGuild([101, 102, 103])
    bot, game = make_bot(guild, tmp_path)
    game.pairs = {frozenset((1, 2)), frozenset((2, 3))}
    game.claims = {101: 1, 102: 2, 103: 3}

    asyncio.run(bot.reconcile())
    rocks, tzynn, glebsig = (embassy(guild, n) for n in ("Rocks", "Tzynn Empire", "Glebsig"))
    assert allowed(rocks) == {999, 101, 102}
    assert allowed(tzynn) == {999, 101, 102, 103}
    assert allowed(glebsig) == {999, 102, 103}
    assert not rocks.overwrites[guild.default_role].view_channel

    # Nothing changed: a second pass makes no edits
    before = (rocks.edits, tzynn.edits)
    asyncio.run(bot.reconcile())
    assert (rocks.edits, tzynn.edits) == before

    # Glebsig's player is sitting in Tzynn's channel when the contact disappears
    member = guild.members[103]
    tzynn.join(member)
    game.pairs = {frozenset((1, 2))}
    asyncio.run(bot.reconcile())
    assert allowed(tzynn) == {999, 101, 102}
    assert member.voice_channel is None

    # Rename follows the empire name; unclaimed empire's channel is deleted
    game.countries[1].name = "Rocks Union"
    del game.claims[103]
    asyncio.run(bot.reconcile())
    names = {c.name for c in guild.channels.values() if c.type == discord.ChannelType.voice}
    assert names == {"Rocks Union", "Tzynn Empire"}
    assert 3 not in game.channels


def test_unknown_members_are_skipped(tmp_path):
    guild = FakeGuild([101])
    bot, game = make_bot(guild, tmp_path)
    game.pairs = {frozenset((1, 2))}
    game.claims = {101: 1, 555: 2}      # 555 left the server
    asyncio.run(bot.reconcile())
    assert allowed(embassy(guild, "Rocks")) == {999, 101}


def test_commands_registered(tmp_path):
    bot, _ = make_bot(FakeGuild([]), tmp_path)
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"claim", "unclaim", "contacts", "empires", "proxychat"}
    admin = bot.tree.get_command("proxychat")
    assert {c.name for c in admin.commands} == {"status", "assign", "unassign", "resync", "cleanup", "claims", "session"}
    assert {c.name for c in admin.get_command("claims").commands} == {"clear"}
    assert {c.name for c in admin.get_command("session").commands} == {"end"}


def abc_setup(tmp_path):
    """A (Rocks, user 101) knows B (Tzynn, 102); B knows C (Glebsig, 103); A and C haven't met."""
    guild = FakeGuild([101, 102, 103])
    bot, game = make_bot(guild, tmp_path)
    game.pairs = {frozenset((1, 2)), frozenset((2, 3))}
    game.claims = {101: 1, 102: 2, 103: 3}
    asyncio.run(bot.reconcile())
    return guild, bot, game, embassy(guild, "Tzynn Empire")


def visible(channel):
    return {t.id for t, ow in channel.overwrites.items() if ow.view_channel}


def test_occupant_hides_channel_from_strangers(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    a = guild.members[101]

    b_channel.join(a)
    bot._joined[101] = 1.0
    asyncio.run(bot.reconcile())
    assert visible(b_channel) == {999, 101, 102}     # hidden from C
    assert a.voice_channel is b_channel               # A stays

    b_channel.join(guild.members[102])               # B joins their own channel: fine
    bot._joined[102] = 2.0
    asyncio.run(bot.reconcile())
    assert visible(b_channel) == {999, 101, 102}
    assert len(b_channel.members) == 2

    a.voice_channel.members.remove(a)                # A leaves
    a.voice_channel = None
    bot._joined.pop(101)
    asyncio.run(bot.reconcile())
    assert visible(b_channel) == {999, 101, 102, 103}  # C can see it again


def test_simultaneous_join_keeps_first_joiner(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    a, c = guild.members[101], guild.members[103]
    # Both got in before the bot reacted; C was a moment later
    b_channel.join(c)
    b_channel.join(a)
    bot._joined.update({101: 5.0, 103: 5.1})
    asyncio.run(bot.reconcile())
    assert a.voice_channel is b_channel
    assert c.voice_channel is None
    assert visible(b_channel) == {999, 101, 102}


def test_comms_appearing_lets_both_stay(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    game.pairs.add(frozenset((1, 3)))               # A meets C
    asyncio.run(bot.reconcile())
    b_channel.join(guild.members[101])
    b_channel.join(guild.members[103])
    bot._joined.update({101: 1.0, 103: 2.0})
    asyncio.run(bot.reconcile())
    assert len(b_channel.members) == 2
    assert visible(b_channel) == {999, 101, 102, 103}


# ---- in-game Join / Call requests ------------------------------------------

def voice_channels(guild):
    return {c.name: c for c in guild.channels.values() if c.type == discord.ChannelType.voice}


def in_lobby(guild, *user_ids):
    """Put users in an ordinary voice channel outside the bot's category."""
    lobby = guild._add(FakeChannel(guild, "Lobby", discord.ChannelType.voice))
    for u in user_ids:
        lobby.join(guild.members[u])
    return lobby


def test_join_moves_caller_into_target_embassy(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    in_lobby(guild, 101)
    asyncio.run(bot.handle_join(K, 1, 2))           # Rocks asks to join Tzynn
    assert guild.members[101].voice_channel is b_channel


def test_join_not_in_voice_gets_a_link(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    asyncio.run(bot.handle_join(K, 1, 2))
    assert guild.members[101].voice_channel is None
    assert b_channel.jump_url in guild.members[101].dms[0]


def test_join_respects_occupancy(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    b_channel.join(guild.members[103])               # Glebsig is in Tzynn's channel
    bot._joined[103] = 1.0
    in_lobby(guild, 101)
    asyncio.run(bot.handle_join(K, 1, 2))           # Rocks hasn't met Glebsig
    assert guild.members[101].voice_channel.name == "Lobby"
    assert "can't join" in guild.members[101].dms[0]


def test_join_ignored_for_other_game(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    in_lobby(guild, 101)
    asyncio.run(bot.handle_join("999-999-999", 1, 2))
    assert guild.members[101].voice_channel.name == "Lobby"


def test_call_creates_private_channel_and_moves_both(tmp_path):
    guild, bot, game, _ = abc_setup(tmp_path)
    in_lobby(guild, 101, 102, 103)
    asyncio.run(bot.handle_call(K, 1, 2))
    call = voice_channels(guild)["Call: Rocks ↔ Tzynn Empire"]
    assert {m.id for m in call.members} == {101, 102}
    assert allowed(call) == {999, 101, 102}          # hidden from Glebsig
    assert guild.members[103].voice_channel.name == "Lobby"

    # A second accepted call between the same pair reuses the channel
    asyncio.run(bot.handle_call(K, 2, 1))
    assert sum(n.startswith("Call:") for n in voice_channels(guild)) == 1

    # Reconcile keeps the call while it's occupied, and the Lobby isn't ours to delete
    asyncio.run(bot.reconcile())
    assert "Call: Rocks ↔ Tzynn Empire" in voice_channels(guild)


def test_empty_call_is_deleted_after_grace(tmp_path, monkeypatch):
    import proxychat_bot.bot as botmod
    guild, bot, game, _ = abc_setup(tmp_path)
    asyncio.run(bot.handle_call(K, 1, 2))           # nobody in voice: both get links
    assert all(guild.members[u].dms for u in (101, 102))
    name = "Call: Rocks ↔ Tzynn Empire"
    asyncio.run(bot.reconcile())
    assert name in voice_channels(guild)             # still within the grace period
    monkeypatch.setattr(botmod, "CALL_GRACE_SECONDS", 0.0)
    asyncio.run(bot.reconcile())
    assert name not in voice_channels(guild)
    assert bot.only_slot().calls == {}


def test_call_needs_both_empires_claimed(tmp_path):
    guild, bot, game, _ = abc_setup(tmp_path)
    del game.claims[102]
    asyncio.run(bot.handle_call(K, 1, 2))
    assert not any(n.startswith("Call:") for n in voice_channels(guild))
    assert "/claim" in guild.members[101].dms[0]


# ---- Voice Chat menu: home and Galactic Community -----------------------------

GALCOM = "Galactic Community"


def test_galcom_channel_for_claimed_members_only(tmp_path):
    guild, bot, game, _ = abc_setup(tmp_path)
    assert GALCOM not in voice_channels(guild)       # no members yet
    game.galcom = {1, 3}                              # Rocks and Glebsig, who haven't met
    asyncio.run(bot.reconcile())
    channel = voice_channels(guild)[GALCOM]
    assert allowed(channel) == {999, 101, 103}       # no occupancy rule here
    # Rocks leaves the Galactic Community while sitting in the channel
    channel.join(guild.members[101])
    game.galcom = {3}
    asyncio.run(bot.reconcile())
    assert guild.members[101].voice_channel is None
    assert allowed(channel) == {999, 103}
    # No claimed members left: the channel goes
    game.galcom = set()
    asyncio.run(bot.reconcile())
    assert GALCOM not in voice_channels(guild)
    assert 0 not in game.channels


def test_home_moves_players_back_to_their_embassy(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    b_channel.join(guild.members[101])               # Rocks is visiting Tzynn
    asyncio.run(bot.handle_home(K, 1))
    assert guild.members[101].voice_channel is voice_channels(guild)["Rocks"]


def test_home_when_not_in_voice_sends_link(tmp_path):
    guild, bot, game, _ = abc_setup(tmp_path)
    asyncio.run(bot.handle_home(K, 1))
    assert voice_channels(guild)["Rocks"].jump_url in guild.members[101].dms[0]


def test_galcom_join_moves_member_in(tmp_path):
    guild, bot, game, _ = abc_setup(tmp_path)
    game.galcom = {1}
    in_lobby(guild, 101)
    asyncio.run(bot.handle_galcom(K, 1))
    assert guild.members[101].voice_channel is voice_channels(guild)[GALCOM]


# ---- sharing an existing category (e.g. the server's "Stellaris" section) -----

def stellaris_server(tmp_path):
    """A server that already has a Stellaris category with its own channels in it."""
    guild = FakeGuild([101, 102, 103])
    cat = guild._add(FakeChannel(guild, "Stellaris", discord.ChannelType.category))
    lounge = guild._add(FakeChannel(guild, "Stellaris Lounge", discord.ChannelType.voice, category_id=cat.id))
    lfg = guild._add(FakeChannel(guild, "LFG", discord.ChannelType.voice, category_id=cat.id))
    text = guild._add(FakeChannel(guild, "stellaris-chat", discord.ChannelType.text, category_id=cat.id))
    bot, game = make_bot(guild, tmp_path)
    set_slot(bot, category_id=cat.id)
    game.pairs = {frozenset((1, 2)), frozenset((2, 3))}
    return guild, bot, game, cat, (lounge, lfg, text)


def order(cat):
    return [c.name for c in sorted(cat.voice_channels, key=lambda c: (c.position, c.id))]


def test_shared_category_keeps_server_channels(tmp_path):
    guild, bot, game, cat, server_channels = stellaris_server(tmp_path)
    game.claims = {103: 3, 101: 1, 102: 2}
    game.galcom = {1}
    asyncio.run(bot.reconcile())

    assert len(guild.categories) == 1                 # no extra category created
    assert all(c.id in guild.channels for c in server_channels)
    # Server's channels first, then ours: Galactic Community, embassies A-Z
    assert order(cat) == ["Stellaris Lounge", "LFG", GALCOM, "Glebsig", "Rocks", "Tzynn Empire"]

    # Nothing changed: no further reorder requests
    reorders = guild._state.http.reorders
    asyncio.run(bot.reconcile())
    assert guild._state.http.reorders == reorders

    # Unclaiming deletes only our channel; the server's survive
    del game.claims[103]
    game.galcom = set()
    asyncio.run(bot.reconcile())
    assert order(cat) == ["Stellaris Lounge", "LFG", "Rocks", "Tzynn Empire"]
    assert all(c.id in guild.channels for c in server_channels)


def test_new_embassy_slots_in_alphabetically(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    game.claims[103] = 3                              # Glebsig claims later
    asyncio.run(bot.reconcile())
    assert order(cat) == ["Stellaris Lounge", "LFG", "Glebsig", "Rocks", "Tzynn Empire"]


def test_server_channel_added_later_stays_above_ours(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    game.claims = {101: 1}
    asyncio.run(bot.reconcile())
    guild._add(FakeChannel(guild, "Events", discord.ChannelType.voice, category_id=cat.id))
    game.claims[102] = 2                              # any change triggers the order check
    asyncio.run(bot.reconcile())
    assert order(cat) == ["Stellaris Lounge", "LFG", "Events", "Rocks", "Tzynn Empire"]


def test_call_goes_below_embassies(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    in_lobby(guild, 101, 102)
    asyncio.run(bot.handle_call(K, 1, 2))
    asyncio.run(bot.reconcile())
    assert order(cat)[-1] == "Call: Rocks ↔ Tzynn Empire"


def test_missing_category_id_creates_nothing(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    set_slot(bot, category_id=424242)                      # typo in config
    game.claims = {101: 1}
    asyncio.run(bot.reconcile())
    assert len(guild.categories) == 1
    assert "Rocks" not in voice_channels(guild)


def test_old_state_files_adopt_embassy_channels(tmp_path):
    import json
    from proxychat_core.state import Store as S
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current_key": K, "games": [
        {"key": K, "channels": {"1": 111, "0": 222}},
        {"key": "000-000-001", "channels": {"4": 333}},
    ]}), encoding="utf-8")
    store = S.load(path)
    assert store.owned == {111, 222, 333}
    store.save()
    assert S.load(path).owned == {111, 222, 333}


def test_rename_reorders_and_server_channels_never_move(tmp_path):
    guild, bot, game, cat, (lounge, lfg, _) = stellaris_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    before = (lounge.position, lfg.position)
    asyncio.run(bot.reconcile())
    game.countries[2].name = "Aardvark Empire"       # Tzynn renames, now sorts first
    asyncio.run(bot.reconcile())
    assert order(cat) == ["Stellaris Lounge", "LFG", "Aardvark Empire", "Rocks"]
    assert (lounge.position, lfg.position) == before


# ---- the bot never touches anything outside its category ----------------------

def test_channel_moved_out_of_category_is_left_alone(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    rocks = voice_channels(guild)["Rocks"]
    rocks.category_id = None                          # an admin drags it elsewhere
    del game.claims[101]                              # and Rocks is unclaimed
    edits = rocks.edits
    asyncio.run(bot.reconcile())
    assert rocks.id in guild.channels                 # not deleted
    assert rocks.edits == edits                       # not renamed or re-permissioned
    assert rocks.id not in bot.store.owned            # and forgotten


def test_moved_out_embassy_is_replaced_not_edited(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    game.claims = {101: 1}
    asyncio.run(bot.reconcile())
    old = voice_channels(guild)["Rocks"]
    old.category_id = None
    edits = old.edits
    asyncio.run(bot.reconcile())
    new = [c for c in cat.voice_channels if c.name == "Rocks"]
    assert len(new) == 1 and new[0].id != old.id
    assert old.edits == edits and old.id in guild.channels


def test_move_refused_falls_back_to_link(tmp_path):
    guild, bot, game, b_channel = abc_setup(tmp_path)
    in_lobby(guild, 101)
    member = guild.members[101]

    async def refuse(channel):
        raise discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "missing permissions")
    member.move_to = refuse
    asyncio.run(bot.handle_join(K, 1, 2))
    assert member.voice_channel.name == "Lobby"
    assert b_channel.jump_url in member.dms[0]


def test_permission_check_warns_about_server_wide_rights(tmp_path, caplog):
    import logging
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    guild.me.guild_permissions = discord.Permissions(manage_channels=True, view_channel=True)
    cat.permissions_for = lambda m: discord.Permissions(view_channel=True, manage_channels=True)
    with caplog.at_level(logging.INFO, logger="proxychat"):
        asyncio.run(bot.reconcile())
    text = caplog.text
    assert "manage_channels server-wide" in text
    assert "Missing on category" in text and "move_members" in text

    caplog.clear()
    bot.only_slot().category_id = None
    guild.me.guild_permissions = discord.Permissions.none()
    cat.permissions_for = lambda m: discord.Permissions(**{p: True for p in __import__("proxychat_bot.bot", fromlist=["x"]).CATEGORY_PERMS})
    with caplog.at_level(logging.INFO, logger="proxychat"):
        asyncio.run(bot.reconcile())
    assert "Locked to its category" in caplog.text
    assert "Missing" not in caplog.text


# ---- bot-commands channel and clean-up ------------------------------------------

def text_channels(guild):
    return {c.name: c for c in guild.channels.values() if c.type == discord.ChannelType.text}


def test_commands_channel_created_once_and_follows_category(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    cat.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    asyncio.run(bot.reconcile())
    cmds = text_channels(guild)["bot-commands"]
    assert cmds.category_id == cat.id
    assert cmds.overwrites == cat.overwrites          # synced with the category
    assert bot.store.commands_channel == cmds.id
    asyncio.run(bot.reconcile())
    assert sum(c.name == "bot-commands" for c in guild.channels.values()) == 1


def test_commands_channel_recreated_if_deleted(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    asyncio.run(bot.reconcile())
    asyncio.run(text_channels(guild)["bot-commands"].delete())
    asyncio.run(bot.reconcile())
    assert "bot-commands" in text_channels(guild)


def test_commands_channel_can_be_turned_off(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    set_slot(bot, commands_channel="")
    asyncio.run(bot.reconcile())
    assert "bot-commands" not in text_channels(guild)


def test_cleanup_removes_empire_channels_and_keeps_the_rest(tmp_path):
    guild, bot, game, cat, server_channels = stellaris_server(tmp_path)
    game.claims = {101: 1, 102: 2, 103: 3}
    game.galcom = {1}
    asyncio.run(bot.reconcile())
    in_lobby(guild, 101, 102)
    asyncio.run(bot.handle_call(K, 1, 2))
    rocks = voice_channels(guild)["Rocks"]
    rocks.join(guild.members[103])                   # someone is sitting in one

    deleted, claims = asyncio.run(bot.cleanup())
    assert (deleted, claims) == (5, 3)                # 3 embassies, galcom, call
    assert set(voice_channels(guild)) == {"Stellaris Lounge", "LFG", "Lobby"}
    assert "bot-commands" in text_channels(guild)
    assert all(c.id in guild.channels for c in server_channels)
    assert game.claims == {} and game.channels == {}
    assert bot.store.owned == {bot.store.commands_channel}
    assert game.countries and game.pairs              # empires and contacts kept

    # A further reconcile doesn't bring them back
    asyncio.run(bot.reconcile())
    assert set(voice_channels(guild)) == {"Stellaris Lounge", "LFG", "Lobby"}
    # ...until someone claims again
    game.claims[101] = 1
    asyncio.run(bot.reconcile())
    assert "Rocks" in voice_channels(guild)


# ---- /proxychat admin roles -------------------------------------------------------

VOLUNTEERS = 1470409430003880152


class FakeUser:
    def __init__(self, role_ids=(), manage_guild=False):
        self.roles = [discord.Object(r) for r in role_ids]
        self.guild_permissions = discord.Permissions(manage_guild=manage_guild)


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)


def admin_group(bot):
    return bot.tree.get_command("proxychat")


def check(bot, user):
    interaction = type("I", (), {"user": user, "response": FakeResponse()})()
    return asyncio.run(admin_group(bot).interaction_check(interaction)), interaction.response.sent


def test_admin_role_can_use_proxychat(tmp_path):
    guild = FakeGuild([])
    cfg = Config("token", 5, "Proxy Chat", 0, Path("game.log"), tmp_path / "s.json", 1.0, admin_role_ids=(VOLUNTEERS,))
    bot = ProxyChatBot(cfg, Store())
    assert admin_group(bot).default_permissions is None     # Discord shows it; the bot checks
    assert check(bot, FakeUser([VOLUNTEERS])) == (True, [])
    assert check(bot, FakeUser(manage_guild=True)) == (True, [])
    ok, sent = check(bot, FakeUser([42]))
    assert not ok and "Only server managers" in sent[0]


def test_without_admin_roles_only_manage_server(tmp_path):
    bot, _ = make_bot(FakeGuild([]), tmp_path)
    assert admin_group(bot).default_permissions.manage_guild
    assert check(bot, FakeUser([VOLUNTEERS]))[0] is False
    assert check(bot, FakeUser(manage_guild=True))[0] is True


# ---- cleanup can only ever delete the bot's own channels in its own category ------

def full_server(tmp_path):
    """Our category with our channels, plus plenty of things that must never be deleted."""
    guild, bot, game, cat, server_channels = stellaris_server(tmp_path)
    other_cat = guild._add(FakeChannel(guild, "General", discord.ChannelType.category))
    general_vc = guild._add(FakeChannel(guild, "General VC", discord.ChannelType.voice, category_id=other_cat.id))
    loose = guild._add(FakeChannel(guild, "Loose VC", discord.ChannelType.voice))
    game.claims = {101: 1, 102: 2, 103: 3}
    game.galcom = {1}
    asyncio.run(bot.reconcile())
    protected = [cat, other_cat, general_vc, loose, *server_channels]
    return guild, bot, game, cat, protected


def test_cleanup_never_touches_anything_else(tmp_path):
    guild, bot, game, cat, protected = full_server(tmp_path)
    ours = {"Rocks", "Tzynn Empire", "Glebsig", GALCOM}
    deleted, _ = asyncio.run(bot.cleanup())
    assert deleted == 4
    assert set(guild.deleted) == ours
    assert all(c.id in guild.channels for c in protected)


def test_cleanup_ignores_bogus_ids_in_state(tmp_path):
    """Even if state.json listed server channels as ours, they're outside our category or not ours to delete."""
    guild, bot, game, cat, protected = full_server(tmp_path)
    for c in protected:
        bot.store.owned.add(c.id)        # corrupt / hand-edited state file
    asyncio.run(bot.cleanup())
    survivors = [c for c in protected if c.id in guild.channels]
    # The only "owned" ids inside our category that can go are voice/text channels there;
    # categories and channels elsewhere always survive
    gone = {c.name for c in protected if c.id not in guild.channels}
    assert gone <= {"Stellaris Lounge", "LFG", "stellaris-chat"}   # only inside our category
    # Categories, channels in other categories and uncategorized channels always survive
    assert {"Stellaris", "General", "General VC", "Loose VC"} <= {c.name for c in survivors}


def test_cleanup_respects_live_state_over_cache(tmp_path):
    guild, bot, game, cat, protected = full_server(tmp_path)
    rocks = voice_channels(guild)["Rocks"]
    # Someone just dragged Rocks out; the cache hasn't caught up but Discord knows
    moved = FakeChannel(guild, "Rocks", discord.ChannelType.voice, category_id=None)
    moved.id = rocks.id
    real_fetch = guild.fetch_channel

    async def fetch(id):
        return moved if id == rocks.id else await real_fetch(id)
    guild.fetch_channel = fetch
    asyncio.run(bot.cleanup())
    assert rocks.id in guild.channels and "Rocks" not in guild.deleted


def test_cleanup_refuses_other_server_and_categories(tmp_path):
    guild, bot, game, cat, protected = full_server(tmp_path)
    tzynn = voice_channels(guild)["Tzynn Empire"]
    real_fetch = guild.fetch_channel
    elsewhere = type("G", (), {"id": 77})()

    async def fetch(id):
        ch = await real_fetch(id)
        if id == tzynn.id:
            fake = FakeChannel(guild, ch.name, ch.type, category_id=ch.category_id)
            fake.id, fake.guild = ch.id, elsewhere     # claims to be from another server
            return fake
        return ch
    guild.fetch_channel = fetch
    bot.store.owned.add(cat.id)                         # a category id somehow listed as ours
    asyncio.run(bot.cleanup())
    assert tzynn.id in guild.channels
    assert cat.id in guild.channels


def test_cleanup_cancelled_when_category_missing(tmp_path):
    guild, bot, game, cat, protected = full_server(tmp_path)
    claims = dict(game.claims)
    set_slot(bot, category_id=999999)                        # category gone / wrong id
    assert asyncio.run(bot.cleanup()) is None
    assert guild.deleted == [] and game.claims == claims


def test_commands_channel_survives_cleanup(tmp_path):
    guild, bot, game, cat, protected = full_server(tmp_path)
    asyncio.run(bot.cleanup())
    assert "bot-commands" in text_channels(guild)


# ---- locked-down bot: rights come only from its role's entry on the category ---------

BOT_ROLE = discord.Object(4242)
TESTERS = discord.Object(5151)
CATEGORY_BOT = discord.PermissionOverwrite(view_channel=True, manage_channels=True, manage_roles=True,
                                           connect=True, speak=True, stream=True, move_members=True)


def locked_server(tmp_path):
    guild, bot, game, cat, extra = stellaris_server(tmp_path)
    guild.me.roles = [guild.default_role, BOT_ROLE]
    cat.overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        BOT_ROLE: CATEGORY_BOT,
        TESTERS: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    return guild, bot, game, cat


def test_new_channels_keep_the_bot_role_entry_and_drop_testers(tmp_path):
    guild, bot, game, cat = locked_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    rocks = voice_channels(guild)["Rocks"]
    targets = {t.id: ow for t, ow in rocks.overwrites.items()}
    assert targets[BOT_ROLE.id] == CATEGORY_BOT          # inherited and kept: lets it manage the channel
    assert TESTERS.id not in targets                      # testers don't see every embassy
    assert allowed(rocks) == {BOT_ROLE.id, 999, 101, 102}
    # Further passes never touch the bot role's entry
    in_lobby(guild, 103)
    game.pairs.add(frozenset((1, 3)))
    game.claims[103] = 3
    asyncio.run(bot.reconcile())
    assert {t.id: ow for t, ow in rocks.overwrites.items()}[BOT_ROLE.id] == CATEGORY_BOT


def test_call_channel_keeps_bot_role_entry(tmp_path):
    guild, bot, game, cat = locked_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    asyncio.run(bot.handle_call(K, 1, 2))
    call = voice_channels(guild)["Call: Rocks ↔ Tzynn Empire"]
    assert {t.id for t in call.overwrites} >= {BOT_ROLE.id}
    assert TESTERS.id not in {t.id for t in call.overwrites}


def forbidden():
    return discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "Missing Permissions")


def test_refused_delete_doesnt_stop_updates(tmp_path, caplog):
    import logging
    guild, bot, game, cat = locked_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    rocks = voice_channels(guild)["Rocks"]

    async def refuse(reason=None):
        raise forbidden()
    rocks.delete = refuse
    del game.claims[101]
    game.claims[103] = 3                                  # another change in the same pass
    with caplog.at_level(logging.ERROR, logger="proxychat"):
        asyncio.run(bot.reconcile())                      # no exception
        asyncio.run(bot.reconcile())
    assert "Glebsig" in voice_channels(guild)             # the rest still happened
    assert caplog.text.count("Discord refused to delete") == 1   # reported once, not spammed
    assert rocks.id in bot.store.owned                    # still ours, retried later


def test_refused_update_doesnt_stop_other_channels(tmp_path):
    guild, bot, game, cat = locked_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    rocks = voice_channels(guild)["Rocks"]

    async def refuse(target, overwrite=None):
        raise forbidden()
    rocks.set_permissions = refuse
    game.pairs = set()                                    # both channels need new permissions
    asyncio.run(bot.reconcile())
    assert allowed(voice_channels(guild)["Tzynn Empire"]) == {BOT_ROLE.id, 999, 102}


def test_occupied_forgotten_channel_waits_until_empty(tmp_path):
    guild, bot, game, cat = locked_server(tmp_path)
    game.claims = {101: 1, 102: 2}
    asyncio.run(bot.reconcile())
    in_lobby(guild, 101, 102)
    asyncio.run(bot.handle_call(K, 1, 2))
    call = voice_channels(guild)["Call: Rocks ↔ Tzynn Empire"]
    bot.only_slot().calls.clear()                                    # as after a restart
    asyncio.run(bot.reconcile())
    assert call.id in guild.channels                      # people in it: kept
    for m in list(call.members):
        call.members.remove(m)
        m.voice_channel = None
    asyncio.run(bot.reconcile())
    assert call.id not in guild.channels                  # empty: gone


# ---- federations ------------------------------------------------------------------

def fed_setup(tmp_path):
    """Rocks (101) and Tzynn (102) form the Union; Glebsig (103) has association status."""
    from proxychat_core.state import Federation
    guild, bot, game, _ = abc_setup(tmp_path)
    game.federations = {4: Federation(4, "Union", {1, 2}, {3})}
    asyncio.run(bot.reconcile())
    return guild, bot, game


def test_federation_channel_for_members_and_associates(tmp_path):
    guild, bot, game = fed_setup(tmp_path)
    channel = voice_channels(guild)["Union"]
    assert allowed(channel) == {999, 101, 102, 103}  # no occupancy rule: Rocks and Glebsig haven't met
    assert game.fed_channels == {4: channel.id}
    # Renamed in game: the channel follows
    game.federations[4].name = "Grand Union"
    asyncio.run(bot.reconcile())
    assert voice_channels(guild)["Grand Union"] is channel
    # Glebsig loses association status while sitting in it
    channel.join(guild.members[103])
    game.federations[4].associates = set()
    asyncio.run(bot.reconcile())
    assert guild.members[103].voice_channel is None
    assert allowed(channel) == {999, 101, 102}
    # Federation dissolved: the channel goes
    game.federations = {}
    asyncio.run(bot.reconcile())
    assert "Grand Union" not in voice_channels(guild)
    assert game.fed_channels == {}


def test_federation_channels_sit_between_galcom_and_embassies(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    from proxychat_core.state import Federation
    game.claims = {101: 1, 102: 2, 103: 3}
    game.galcom = {1}
    game.federations = {4: Federation(4, "Zeta Pact", {3}), 5: Federation(5, "Alpha League", {1})}
    asyncio.run(bot.reconcile())
    assert order(cat) == ["Stellaris Lounge", "LFG", GALCOM, "Alpha League", "Zeta Pact",
                          "Glebsig", "Rocks", "Tzynn Empire"]


def test_federation_join_moves_associate_in(tmp_path):
    guild, bot, game = fed_setup(tmp_path)
    in_lobby(guild, 103)
    asyncio.run(bot.handle_federation(K, 3, 4))
    assert guild.members[103].voice_channel is voice_channels(guild)["Union"]


def test_federation_join_without_channel_explains(tmp_path):
    from proxychat_core.state import Federation
    guild, bot, game, _ = abc_setup(tmp_path)
    game.claims = {103: 3}
    game.federations = {4: Federation(4, "Union", {1, 2}, {3})}   # no member has claimed
    asyncio.run(bot.handle_federation(K, 3, 4))
    assert "Union" not in voice_channels(guild)
    assert "no voice channel" in guild.members[103].dms[0]


def test_cleanup_removes_federation_channels(tmp_path):
    guild, bot, game = fed_setup(tmp_path)
    deleted, _ = asyncio.run(bot.cleanup())
    assert "Union" not in voice_channels(guild)
    assert game.fed_channels == {}


# ---- /proxychat cleanup preview -----------------------------------------------------

def test_cleanup_preview_lists_exactly_what_gets_deleted(tmp_path):
    from proxychat_bot.bot import cleanup_preview
    from proxychat_core.state import Federation
    guild, bot, game, cat, protected = full_server(tmp_path)
    game.federations = {4: Federation(4, "Union", {1, 2})}
    asyncio.run(bot.reconcile())
    in_lobby(guild, 101, 102)
    asyncio.run(bot.handle_call(K, 1, 2))

    category, channels, claims = asyncio.run(bot.cleanup_plan())
    listed = {c.name: kind for c, kind in channels}
    assert listed == {
        "Rocks": "Embassy", "Tzynn Empire": "Embassy", "Glebsig": "Embassy",
        GALCOM: GALCOM, "Union": "Federation", "Call: Rocks ↔ Tzynn Empire": "Private call",
    }
    assert claims == 3
    text = cleanup_preview(category, channels, claims)
    assert "would delete **6** channel(s)" in text and "**3** claim(s)" in text
    assert "confirm:True" in text
    for c in protected:
        assert c.name not in listed                     # server channels never listed
    assert "bot-commands" not in listed

    # Previewing changes nothing
    assert guild.deleted == [] and len(game.claims) == 3

    deleted, _ = asyncio.run(bot.cleanup())
    assert sorted(guild.deleted) == sorted(listed)      # the preview was exact


def test_cleanup_preview_empty_and_truncated(tmp_path):
    from proxychat_bot.bot import cleanup_preview
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    category, channels, claims = asyncio.run(bot.cleanup_plan())
    assert "Nothing to clean up" in cleanup_preview(category, channels, claims)

    many = [(FakeChannel(guild, f"Empire number {i:03}", discord.ChannelType.voice), "Embassy") for i in range(200)]
    text = cleanup_preview(cat, many, 200)
    assert len(text) <= 2000 and "more" in text and "would delete **200**" in text


def test_cleanup_preview_when_category_missing(tmp_path):
    guild, bot, game, cat, _ = stellaris_server(tmp_path)
    set_slot(bot, category_id=999999)
    assert asyncio.run(bot.cleanup_plan()) is None
