"""Several slots (categories, each hosting one game) in one bot: isolation, routing, guards, sessions."""

import asyncio
from pathlib import Path

import discord
import pytest

from proxychat_bot.bot import ProxyChatBot
from proxychat_bot.config import Config
from proxychat_bot.slots import GuildSettings, Slot, SlotSettings, check_distinct
from proxychat_core.names import clean_name
from proxychat_core.protocol import CountryId, Join, SweepBegin
from proxychat_core.source import Batch, SlotEvent
from proxychat_core.state import Country, Store
from test_reconcile import FakeChannel, FakeGuild

K = "123-456-789"
K2 = "999-888-777"


def voice_in(category):
    return {c.name for c in category.voice_channels}


def new_store(key=K):
    store = Store()
    store.current_key = key
    game = store.game(key)
    for cid, name in [(1, "Rocks"), (2, "Tzynn Empire"), (3, "Glebsig")]:
        game.countries[cid] = Country(cid, name, "ruler", True)
    game.pairs = {frozenset((1, 2))}
    return store


def make_bot(guilds, slot_specs, strict=False):
    """guilds: {guild id: FakeGuild}. slot_specs: [(slot id, guild id, category)]."""
    cfg = Config("token", 0, "Proxy Chat", 0, Path("game.log"), Path("unused.json"), 1.0)
    slots = [Slot(SlotSettings(sid, gid, category_id=cat.id), new_store()) for sid, gid, cat in slot_specs]
    bot = ProxyChatBot(cfg, slots=slots, guilds=[GuildSettings(g) for g in guilds], strict_permissions=strict)
    bot.get_guild = lambda gid: guilds.get(gid)
    return bot


def category(guild, name):
    return guild._add(FakeChannel(guild, name, discord.ChannelType.category))


def two_slot_guild():
    guild = FakeGuild([101, 102, 103, 201, 202])
    a, b = category(guild, "Proxy Chat 1"), category(guild, "Proxy Chat 2")
    bot = make_bot({5: guild}, [("s1", 5, a), ("s2", 5, b)])
    return guild, bot, a, b


def run(coro):
    return asyncio.run(coro)


# ---- isolation --------------------------------------------------------------

def test_each_slot_only_builds_in_its_own_category():
    guild, bot, a, b = two_slot_guild()
    s1, s2 = bot.slots["s1"], bot.slots["s2"]
    s1.store.current.claims = {101: 1, 102: 2}
    s2.store.current.claims = {201: 3}

    run(bot.reconcile(s1))
    run(bot.reconcile(s2))
    assert voice_in(a) == {"Rocks", "Tzynn Empire"}
    assert voice_in(b) == {"Glebsig"}
    assert s1.store.owned.isdisjoint(s2.store.owned)


def test_one_slot_never_deletes_anothers_channels():
    guild, bot, a, b = two_slot_guild()
    s1, s2 = bot.slots["s1"], bot.slots["s2"]
    s1.store.current.claims = {101: 1}
    run(bot.reconcile(s1))
    rocks = next(iter(a.voice_channels))

    # Even if slot 2's state somehow lists slot 1's channel, it won't touch it
    s2.store.owned.add(rocks.id)
    run(bot.reconcile(s2))
    assert rocks.id in guild.channels
    assert not run(bot.delete_owned(guild, rocks.id, s2))
    assert rocks.id in guild.channels and rocks.id in s1.store.owned


def test_slots_in_different_guilds_stay_apart():
    g1, g2 = FakeGuild([101]), FakeGuild([201])
    g2.id = 6
    c1, c2 = category(g1, "Proxy Chat"), category(g2, "Proxy Chat")
    bot = make_bot({5: g1, 6: g2}, [("s1", 5, c1), ("s2", 6, c2)])
    bot.slots["s1"].store.current.claims = {101: 1}
    bot.slots["s2"].store.current.claims = {201: 2}
    run(bot.reconcile(bot.slots["s1"]))
    run(bot.reconcile(bot.slots["s2"]))
    assert voice_in(c1) == {"Rocks"} and voice_in(c2) == {"Tzynn Empire"}

    # A channel in another guild is never deleted, even with a matching category id
    other = next(iter(c2.voice_channels))
    bot.slots["s1"].store.owned.add(other.id)
    assert not run(bot.delete_owned(g1, other.id, bot.slots["s1"]))


def test_two_slots_cant_share_a_category():
    guild = FakeGuild([])
    cat = category(guild, "Proxy Chat")
    with pytest.raises(ValueError):
        make_bot({5: guild}, [("s1", 5, cat), ("s2", 5, cat)])
    same_name = [Slot(SlotSettings("a", 5, category="PC"), Store()), Slot(SlotSettings("b", 5, category="PC"), Store())]
    with pytest.raises(ValueError):
        check_distinct(same_name)
    # Same name in different guilds is fine
    check_distinct([Slot(SlotSettings("a", 5, category="PC"), Store()), Slot(SlotSettings("b", 6, category="PC"), Store())])


# ---- routing ------------------------------------------------------------------

def test_events_go_to_their_slot_only():
    guild, bot, a, b = two_slot_guild()
    s1, s2 = bot.slots["s1"], bot.slots["s2"]
    batch = Batch([SlotEvent("s2", "2200.1.1", SweepBegin(K2)),
                   SlotEvent("s2", "2200.1.1", CountryId(K2, 7, True, "Newcomers", "r")),
                   SlotEvent("nope", "2200.1.1", SweepBegin(K2))])
    run(bot.apply_batch(batch))
    assert s2.store.current_key == K2 and 7 in s2.store.current.countries
    assert s1.store.current_key == K and K2 not in s1.store.games
    assert s2.dirty and not s1.dirty


class Interaction:
    def __init__(self, guild_id, channel=None):
        self.guild_id = guild_id
        self.channel = channel


def test_commands_pick_the_slot_of_their_category():
    guild, bot, a, b = two_slot_guild()
    in_b = FakeChannel(guild, "bot-commands", discord.ChannelType.text, category_id=b.id)
    elsewhere = FakeChannel(guild, "general", discord.ChannelType.text, category_id=None)
    assert bot.slot_for(Interaction(5, in_b)) is bot.slots["s2"]
    assert bot.slot_for(Interaction(5, elsewhere)) is None       # two slots: ambiguous
    assert bot.slot_for(Interaction(77, in_b)) is None           # not a guild the bot serves


def test_a_guilds_only_slot_answers_from_anywhere_in_it():
    g1 = FakeGuild([])
    bot = make_bot({5: g1}, [("s1", 5, category(g1, "Proxy Chat"))])
    general = FakeChannel(g1, "general", discord.ChannelType.text)
    assert bot.slot_for(Interaction(5, general)) is bot.slots["s1"]


def test_voice_updates_wake_only_the_owning_slot():
    guild, bot, a, b = two_slot_guild()
    s1, s2 = bot.slots["s1"], bot.slots["s2"]
    s1.store.current.claims = {101: 1}
    run(bot.reconcile(s1))
    rocks = next(iter(a.voice_channels))
    s1.dirty = s2.dirty = False
    before = type("V", (), {"channel": None})()
    after = type("V", (), {"channel": rocks})()
    run(bot.on_voice_state_update(guild.members[101], before, after))
    assert s1.dirty and not s2.dirty


# ---- permission guard ----------------------------------------------------------

def test_strict_mode_refuses_a_guild_with_dangerous_permissions():
    guild = FakeGuild([101, 102])
    cat = category(guild, "Proxy Chat")
    bot = make_bot({5: guild}, [("s1", 5, cat)], strict=True)
    slot = bot.slots["s1"]
    slot.store.current.claims = {101: 1}
    guild.me.guild_permissions = discord.Permissions(administrator=True)

    run(bot.reconcile(slot))
    assert voice_in(cat) == set() and slot.refused == "administrator"
    run(bot.handle_join(K, 1, 2, slot))
    assert guild.members[101].dms == []                # in-game requests ignored too

    guild.me.guild_permissions = discord.Permissions(connect=True, speak=True)
    run(bot.reconcile(slot))
    assert voice_in(cat) == {"Rocks"} and slot.refused is None


def test_strict_refusal_is_announced_once_in_the_commands_channel():
    guild = FakeGuild([101])
    cat = category(guild, "Proxy Chat")
    bot = make_bot({5: guild}, [("s1", 5, cat)], strict=True)
    slot = bot.slots["s1"]
    run(bot.reconcile(slot))                           # creates #bot-commands while allowed
    commands = guild.get_channel(slot.store.commands_channel)
    sent = []

    async def send(text):
        sent.append(text)

    commands.send = send
    guild.me.guild_permissions = discord.Permissions(manage_roles=True, kick_members=True)
    run(bot.reconcile(slot))
    run(bot.reconcile(slot))
    assert len(sent) == 1 and "manage_roles, kick_members" in sent[0]


def test_local_mode_only_warns_about_dangerous_permissions():
    guild = FakeGuild([101])
    cat = category(guild, "Proxy Chat")
    bot = make_bot({5: guild}, [("s1", 5, cat)], strict=False)
    bot.slots["s1"].store.current.claims = {101: 1}
    guild.me.guild_permissions = discord.Permissions(administrator=True)
    run(bot.reconcile(bot.slots["s1"]))
    assert voice_in(cat) == {"Rocks"}


# ---- between sessions ----------------------------------------------------------

def one_slot():
    guild = FakeGuild([101, 102])
    cat = category(guild, "Proxy Chat")
    bot = make_bot({5: guild}, [("s1", 5, cat)])
    slot = bot.slots["s1"]
    slot.store.current.claims = {101: 1, 102: 2}
    run(bot.reconcile(slot))
    return guild, bot, cat, slot


def test_session_end_removes_channels_and_keeps_claims():
    guild, bot, cat, slot = one_slot()
    assert voice_in(cat) == {"Rocks", "Tzynn Empire"}
    tzynn = next(c for c in cat.voice_channels if c.name == "Tzynn Empire")
    tzynn.join(guild.members[102])                    # still chatting after the game

    to_remove, cleared = run(bot.end_session(slot=slot))
    run(bot.reconcile(slot))
    assert (to_remove, cleared) == (2, 0)
    assert voice_in(cat) == {"Tzynn Empire"}          # waits until it's empty
    assert slot.store.current.claims == {101: 1, 102: 2} and slot.store.paused

    tzynn.members.clear()
    guild.members[102].voice_channel = None
    run(bot.reconcile(slot))
    assert voice_in(cat) == set()
    assert slot.store.current.channels == {}
    # The commands channel stays
    assert guild.get_channel(slot.store.commands_channel) is not None


def test_paused_slot_ignores_requests_and_new_claims_wait():
    guild, bot, cat, slot = one_slot()
    run(bot.end_session(slot=slot))
    run(bot.reconcile(slot))
    run(bot.handle_join(K, 1, 2, slot))
    assert guild.members[101].dms == []
    slot.store.current.claims[103] = 3                 # someone claims ahead of next session
    run(bot.reconcile(slot))
    assert voice_in(cat) == set()


def test_replay_doesnt_resume_but_a_live_event_does():
    guild, bot, cat, slot = one_slot()
    run(bot.end_session(slot=slot))
    run(bot.reconcile(slot))

    run(bot.apply_batch(Batch([SlotEvent("s1", "2200.2.1", SweepBegin(K))], replay=True)))
    assert slot.store.paused
    run(bot.apply_batch(Batch([SlotEvent("s1", "2200.2.1", Join(K, 1, 2))])))
    assert not slot.store.paused
    run(bot.reconcile(slot))
    assert voice_in(cat) == {"Rocks", "Tzynn Empire"}  # claims kept, so channels come straight back


def test_session_end_can_clear_claims():
    guild, bot, cat, slot = one_slot()
    assert run(bot.end_session(clear_claims=True, slot=slot)) == (2, 2)
    assert slot.store.current.claims == {}


def test_paused_survives_a_restart(tmp_path):
    store = Store(tmp_path / "state.json")
    store.paused = True
    store.save()
    assert Store.load(tmp_path / "state.json").paused


def test_clear_claims_empties_the_current_game():
    guild, bot, cat, slot = one_slot()
    assert bot.clear_claims(slot) == 2
    run(bot.reconcile(slot))
    assert slot.store.current.claims == {} and voice_in(cat) == set()


# ---- names and mentions --------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Rocks", "Rocks"),
    ("  Tzynn   Empire ", "Tzynn Empire"),
    ("@everyone", "everyone"),
    ("<@123> **bold** `code` ||spoiler||", "123 bold code spoiler"),
    ("Zero​width‮flip", "Zerowidthflip"),
    ("Line\nbreak\ttab", "Line break tab"),
    ("Kri'thak-Kan Commerce League", "Kri'thak-Kan Commerce League"),
    ("Ünïcödé Ēmpire", "Ünïcödé Ēmpire"),
    ("@@@", ""),
])
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


def test_clean_name_falls_back_and_caps_length():
    assert clean_name("@#*", "Empire 4") == "Empire 4"
    assert clean_name(None, "Empire 4") == "Empire 4"
    assert len(clean_name("x" * 500)) == 100
    assert clean_name("ab cd", limit=3) == "ab"


def test_hostile_empire_names_make_tame_channels():
    guild = FakeGuild([101])
    cat = category(guild, "Proxy Chat")
    bot = make_bot({5: guild}, [("s1", 5, cat)])
    slot = bot.slots["s1"]
    slot.store.current.countries[1].name = "@everyone <@&42> **PWNED**‮"
    slot.store.current.claims = {101: 1}
    run(bot.reconcile(slot))
    assert voice_in(cat) == {"everyone &42 PWNED"}   # no @, <, > or markup left


def test_no_message_can_ping_anyone():
    guild = FakeGuild([])
    bot = make_bot({5: guild}, [("s1", 5, category(guild, "Proxy Chat"))])
    mentions = bot.allowed_mentions
    assert not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)
