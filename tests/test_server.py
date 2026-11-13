"""The central server: database, queue, server bot and admin CLI (no Discord connection)."""

import asyncio
import json
import time
from pathlib import Path

import discord
import pytest

from proxychat_core.protocol import Comms, CountryId, SweepBegin
from proxychat_core.records import to_dict
from proxychat_core.state import Country, Store
from proxychat_server import admin_cli, db
from proxychat_server.config import ServerConfig, load_server_config
from proxychat_server.queue import QueueSource
from proxychat_server.serverbot import STALE_GAME_SECONDS, ServerBot
from test_reconcile import FakeChannel, FakeGuild

K = "123-456-789"
G = 5


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "proxychat.db")
    yield c
    c.close()


def add_relay(conn, slot_id, key_id="k1", guild_id=G):
    conn.execute("INSERT INTO relays (key_id, guild_id, slot_id, public_key, label, paired_by, created_at) "
                 "VALUES (?, ?, ?, x'00', 'Volunteer', 1, ?)", (key_id, guild_id, slot_id, db.now()))


def queue(conn, slot_id, record, key_id="k1", date="2200.1.1", raw=None):
    payload = raw if raw is not None else json.dumps(to_dict(date, record))
    conn.execute("INSERT INTO event_queue (slot_id, key_id, payload, received_at) VALUES (?, ?, ?, ?)",
                 (slot_id, key_id, payload, db.now()))


# ---- database ---------------------------------------------------------------

def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.connect(path).close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_guild_cap_is_five_by_default(conn):
    for gid in range(1, 6):
        db.approve_guild(conn, gid, "cli")
    with pytest.raises(db.CapReached):
        db.approve_guild(conn, 6, "cli")
    db.approve_guild(conn, 3, "cli", max_slots=4)      # re-approving an existing one is fine
    db.set_guild_status(conn, 2, "removed", "cli")
    db.approve_guild(conn, 6, "cli")                   # a place came free
    assert db.active_guild_count(conn) == 5


def test_frozen_guilds_count_towards_the_cap(conn):
    for gid in range(1, 6):
        db.approve_guild(conn, gid, "cli")
    db.set_guild_status(conn, 1, "frozen", "cli")
    with pytest.raises(db.CapReached):
        db.approve_guild(conn, 6, "cli")


def test_slot_rules(conn):
    with pytest.raises(db.SlotError):
        db.add_slot(conn, G, 100, "Game 1", "cli")     # guild not approved
    db.approve_guild(conn, G, "cli", max_slots=2)
    db.add_slot(conn, G, 100, "Game 1", "cli")
    with pytest.raises(db.SlotError):
        db.add_slot(conn, G, 100, "Game 2", "cli")     # same category
    with pytest.raises(db.SlotError):
        db.add_slot(conn, G, 101, "Game 1", "cli")     # same label
    db.add_slot(conn, G, 101, "Game 2", "cli")
    with pytest.raises(db.SlotError):
        db.add_slot(conn, G, 102, "Game 3", "cli")     # max_slots
    db.set_guild_status(conn, G, "frozen", "cli")
    assert db.active_slots(conn) == []                 # a frozen guild's slots aren't served


def test_removing_a_guild_revokes_its_relays_and_purge_deletes_it_later(conn):
    db.approve_guild(conn, G, "cli")
    slot = db.add_slot(conn, G, 100, "Game 1", "cli")
    add_relay(conn, slot)
    assert db.key_bound_to(conn, "k1", slot)
    db.set_guild_status(conn, G, "removed", "cli")
    assert not db.key_bound_to(conn, "k1", slot)

    db.purge(conn, at=db.now() + 6 * db.DAY)
    assert db.guild(conn, G) is not None
    db.purge(conn, at=db.now() + 8 * db.DAY)
    assert db.guild(conn, G) is None
    assert db.slots_of(conn) == [] and conn.execute("SELECT COUNT(*) FROM relays").fetchone()[0] == 0


def test_key_must_belong_to_the_slot(conn):
    db.approve_guild(conn, G, "cli")
    s1 = db.add_slot(conn, G, 100, "Game 1", "cli")
    s2 = db.add_slot(conn, G, 101, "Game 2", "cli")
    add_relay(conn, s1)
    assert db.key_bound_to(conn, "k1", s1) and not db.key_bound_to(conn, "k1", s2)
    db.revoke_relay(conn, "k1", "cli")
    assert not db.key_bound_to(conn, "k1", s1)


def test_slot_state_backend_round_trip(conn):
    db.approve_guild(conn, G, "cli")
    slot = db.add_slot(conn, G, 100, "Game 1", "cli")
    store = Store(backend=db.SqliteBackend(conn, slot))
    store.current_key = K
    store.game(K).claims[42] = 1
    store.save()
    loaded = Store.from_backend(db.SqliteBackend(conn, slot))
    assert loaded.current.claims == {42: 1}


def test_purge_keeps_recent_rows(conn):
    db.audit(conn, "cli", "something")
    assert db.purge(conn)["audit"] == 0
    assert db.purge(conn, at=db.now() + 91 * db.DAY)["audit"] >= 1


def test_backup_is_a_working_copy(conn, tmp_path):
    db.approve_guild(conn, G, "cli")
    db.backup(conn, tmp_path / "copy.db")
    assert db.guild(db.connect(tmp_path / "copy.db"), G)["status"] == "approved"


# ---- queue ------------------------------------------------------------------

def slot_with_relay(conn):
    db.approve_guild(conn, G, "cli")
    slot = db.add_slot(conn, G, 100, "Game 1", "cli")
    add_relay(conn, slot)
    return slot


def test_queue_yields_valid_events_and_deletes_after(conn):
    slot = slot_with_relay(conn)
    queue(conn, slot, SweepBegin(K))
    queue(conn, slot, Comms(K, 1, 2))
    source = QueueSource(conn)
    batch, ids, dropped = source.poll()
    assert [e.record for e in batch.events] == [SweepBegin(K), Comms(K, 1, 2)]
    assert {e.slot_id for e in batch.events} == {slot} and dropped == 0 and not batch.replay
    source.ack(ids)
    assert conn.execute("SELECT COUNT(*) FROM event_queue").fetchone()[0] == 0


def test_queue_drops_invalid_and_unauthorised_rows(conn):
    slot = slot_with_relay(conn)
    other = db.add_slot(conn, G, 101, "Game 2", "cli")
    queue(conn, slot, None, raw='{"t": "COMMS", "date": null, "key": "1-2-3", "a": 1, "b": 1}')
    queue(conn, slot, None, raw="not json")
    queue(conn, slot, None, raw=json.dumps(to_dict(None, Comms(K, 1, 2))) + " " * 5000)
    queue(conn, slot, Comms(K, 1, 2), key_id="unknown")
    queue(conn, other, Comms(K, 1, 2))           # k1 belongs to the first slot
    queue(conn, slot, Comms(K, 3, 4))            # the only good one
    batch, ids, dropped = QueueSource(conn).poll()
    assert [e.record for e in batch.events] == [Comms(K, 3, 4)]
    assert dropped == 5 and len(ids) == 6


def test_queue_stops_accepting_a_revoked_key(conn):
    slot = slot_with_relay(conn)
    queue(conn, slot, Comms(K, 1, 2))
    db.revoke_relay(conn, "k1", "cli")
    batch, _, dropped = QueueSource(conn).poll()
    assert batch is None and dropped == 1


def test_a_failing_batch_doesnt_block_the_queue(conn):
    slot = slot_with_relay(conn)
    queue(conn, slot, Comms(K, 1, 2))
    queue(conn, slot, Comms(K, 3, 4))
    source = QueueSource(conn, poll_seconds=0.01, batch_size=1)

    async def consume():
        gen = source.batches()
        first = await gen.__anext__()
        # The bot failing on the first batch: the source drops it and moves on to the next
        second = await asyncio.wait_for(gen.athrow(RuntimeError("bot blew up")), 2)
        await gen.aclose()
        return first, second

    first, second = run(consume())
    assert first.events[0].record == Comms(K, 1, 2) and second.events[0].record == Comms(K, 3, 4)
    remaining = [json.loads(r["payload"])["a"] for r in conn.execute("SELECT payload FROM event_queue")]
    assert remaining == [3]   # the failed batch is gone; the second is still waiting for its ack


# ---- server bot -------------------------------------------------------------

def server_bot(conn):
    bot = ServerBot(ServerConfig(Path("unused"), "token"), conn)
    guild = FakeGuild([101, 102])
    guilds = {G: guild}
    bot.get_guild = lambda gid: guilds.get(gid)
    return bot, guild


class LeavingGuild:
    def __init__(self, id):
        self.id, self.name, self.left = id, "Somewhere", False

    async def leave(self):
        self.left = True


def test_unapproved_guilds_are_left_and_approved_ones_kept(conn):
    db.approve_guild(conn, G, "cli")
    db.approve_guild(conn, 6, "cli")
    db.set_guild_status(conn, 6, "frozen", "cli")
    bot, _ = server_bot(conn)
    stranger, approved, frozen = LeavingGuild(99), LeavingGuild(G), LeavingGuild(6)
    for g in (stranger, approved, frozen):
        run(bot.on_guild_join(g))
    assert stranger.left and not approved.left and not frozen.left
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'left unapproved guild'").fetchone()[0] == 1


def test_being_removed_marks_the_guild_removed(conn):
    db.approve_guild(conn, G, "cli")
    bot, _ = server_bot(conn)
    run(bot.on_guild_remove(LeavingGuild(G)))
    assert db.guild(conn, G)["status"] == "removed"


def test_server_bot_loads_slots_and_reloads_changes(conn):
    db.approve_guild(conn, G, "cli")
    s1 = db.add_slot(conn, G, 100, "Game 1", "cli")
    bot, _ = server_bot(conn)
    assert set(bot.slots) == {s1} and bot.strict_permissions
    first = bot.slots[s1]

    s2 = db.add_slot(conn, G, 101, "Game 2", "cli")
    db.set_admin_roles(conn, G, [777], "cli")
    assert db.config_version(conn) != bot._config_version
    bot.reload()
    assert set(bot.slots) == {s1, s2} and bot.slots[s1] is first   # existing slots keep their state
    assert bot.guild_settings(G).admin_role_ids == (777,)

    db.set_guild_status(conn, G, "frozen", "cli")
    bot.reload()
    assert bot.slots == {} and bot.guild_configs == {}


def test_server_bot_applies_queued_events_to_the_right_slot(conn):
    slot = slot_with_relay(conn)
    bot, _ = server_bot(conn)
    queue(conn, slot, SweepBegin(K))
    queue(conn, slot, CountryId(K, 1, True, "Rocks", "r"))
    batch, ids, _ = bot.source.poll()
    run(bot.apply_batch(batch))
    bot.source.ack(ids)
    loaded = Store.from_backend(db.SqliteBackend(conn, slot))
    assert loaded.current_key == K and loaded.current.countries[1].name == "Rocks"
    assert loaded.current.seen_at is not None


def test_removing_a_slot_deletes_its_channels(conn):
    db.approve_guild(conn, G, "cli")
    bot, guild = server_bot(conn)
    cat = guild._add(FakeChannel(guild, "Proxy Chat", discord.ChannelType.category))
    slot_id = db.add_slot(conn, G, cat.id, "Game 1", "cli")
    bot.reload()
    slot = bot.slots[slot_id]
    slot.store.current_key = K
    slot.store.game(K).countries[1] = Country(1, "Rocks", "r", True)
    slot.store.current.claims = {101: 1}
    run(bot.reconcile(slot))
    assert {c.name for c in cat.channels} == {"bot-commands", "Rocks"}

    assert run(bot.remove_slot(slot, "discord:1")) == 2
    assert cat.channels == [] and cat.id in guild.channels       # the category itself stays
    assert db.slots_of(conn) == [] and bot.slots == {}


def test_retention_forgets_old_games_but_never_the_current_one(conn):
    db.approve_guild(conn, G, "cli")
    slot_id = db.add_slot(conn, G, 100, "Game 1", "cli")
    bot, _ = server_bot(conn)
    store = bot.slots[slot_id].store
    store.current_key = "1-1-1"
    store.game("1-1-1").seen_at = 0
    store.game("2-2-2").seen_at = time.time() - STALE_GAME_SECONDS - 10
    store.game("3-3-3").seen_at = time.time()
    store.game("4-4-4")                                  # never seen: clock starts now
    bot.run_retention()
    assert set(store.games) == {"1-1-1", "3-3-3", "4-4-4"}
    assert store.games["4-4-4"].seen_at is not None


def test_server_config_reads_the_token_file(tmp_path):
    (tmp_path / "token").write_text("abc\n", encoding="utf-8")
    (tmp_path / "server.toml").write_text(
        f'[server]\ndb_path = "x.db"\ntoken_file = "{(tmp_path / "token").as_posix()}"\nowner_user_id = 42\n',
        encoding="utf-8")
    cfg = load_server_config(tmp_path / "server.toml")
    assert (cfg.token, cfg.owner_user_id, cfg.db_path) == ("abc", 42, Path("x.db"))


# ---- admin CLI --------------------------------------------------------------

def cli(tmp_path, *args):
    lines = []
    code = admin_cli.main(["--db", str(tmp_path / "proxychat.db"), *args], out=lines.append)
    return code, "\n".join(lines)


def test_cli_approves_up_to_the_cap(tmp_path):
    for gid in range(1, 6):
        assert cli(tmp_path, "guild", "approve", str(gid))[0] == 0
    code, text = cli(tmp_path, "guild", "approve", "6")
    assert code == 1 and "limit of 5" in text
    code, text = cli(tmp_path, "guild", "list")
    assert "5 of 5 guild places used" in text
    assert cli(tmp_path, "set", "max-guilds", "6")[0] == 0
    assert cli(tmp_path, "guild", "approve", "6")[0] == 0


def test_cli_freeze_unfreeze_remove(tmp_path):
    cli(tmp_path, "guild", "approve", "7")
    assert cli(tmp_path, "guild", "unfreeze", "7")[0] == 1      # not frozen
    assert cli(tmp_path, "guild", "freeze", "7")[0] == 0
    assert cli(tmp_path, "guild", "unfreeze", "7")[0] == 0
    assert cli(tmp_path, "guild", "remove", "7")[0] == 0
    assert cli(tmp_path, "guild", "freeze", "8")[0] == 1        # unknown
    assert cli(tmp_path, "guild", "approve")[0] == 2            # no id


def test_cli_import_state_carries_channels_and_claims(tmp_path):
    local = Store(tmp_path / "state.stellaris-server.json")
    local.current_key = K
    local.game(K).claims = {101: 1, 102: 2}
    local.owned = {500, 501}
    local.commands_channel = 500
    local.save()
    assert cli(tmp_path, "import-state", "5", "100", str(local.path))[0] == 1   # guild not approved yet
    cli(tmp_path, "guild", "approve", "5")
    code, text = cli(tmp_path, "import-state", "5", "100", str(local.path), "--label", "Main")
    assert code == 0 and "2 claim(s)" in text and "2 channel(s)" in text

    conn = db.connect(tmp_path / "proxychat.db")
    slot_id = db.slots_of(conn, 5)[0]["slot_id"]
    imported = Store.from_backend(db.SqliteBackend(conn, slot_id))
    assert imported.current.claims == {101: 1, 102: 2} and imported.owned == {500, 501}
    assert imported.commands_channel == 500


def test_cli_import_rejects_garbage(tmp_path):
    cli(tmp_path, "guild", "approve", "5")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cli(tmp_path, "import-state", "5", "100", str(tmp_path / "bad.json"))[0] == 1


def test_cli_status_maintenance_audit_backup(tmp_path):
    cli(tmp_path, "guild", "approve", "5")
    assert "never seen" in cli(tmp_path, "status")[1]
    cli(tmp_path, "maintenance", "on")
    assert "Maintenance: on" in cli(tmp_path, "status")[1]
    audit = cli(tmp_path, "audit")[1]
    assert "guild approve" in audit and "maintenance on" in audit
    assert cli(tmp_path, "backup", str(tmp_path / "b.db"))[0] == 0
    assert cli(tmp_path, "backup", str(tmp_path / "b.db"))[0] == 1   # won't overwrite


def test_cli_relays(tmp_path):
    cli(tmp_path, "guild", "approve", "5")
    conn = db.connect(tmp_path / "proxychat.db")
    slot = db.add_slot(conn, 5, 100, "Game 1", "cli")
    add_relay(conn, slot, "k1", 5)
    add_relay(conn, slot, "k2", 5)
    assert "Volunteer" in cli(tmp_path, "relay", "list")[1]
    assert cli(tmp_path, "relay", "revoke", "k1")[0] == 0
    assert cli(tmp_path, "relay", "revoke", "k1")[0] == 1         # already revoked
    assert "1 key(s)" in cli(tmp_path, "relay", "revoke-all")[1]


def test_cli_export_state_round_trips_with_import(tmp_path):
    local = Store(tmp_path / "state.json")
    local.current_key = K
    local.game(K).claims = {101: 1}
    local.owned = {500}
    local.save()
    cli(tmp_path, "guild", "approve", "5")
    cli(tmp_path, "import-state", "5", "100", str(local.path))
    conn = db.connect(tmp_path / "proxychat.db")
    slot_id = db.slots_of(conn, 5)[0]["slot_id"]
    assert cli(tmp_path, "export-state", slot_id, str(local.path))[0] == 1          # won't overwrite
    assert cli(tmp_path, "export-state", "s_nope", str(tmp_path / "x.json"))[0] == 1
    assert cli(tmp_path, "export-state", slot_id, str(tmp_path / "back.json"))[0] == 0
    back = Store.load(tmp_path / "back.json")
    assert back.current.claims == {101: 1} and back.owned == {500}


def test_trial_pair_command_adds_the_local_flag():
    from proxychat_server.serverbot import relay_pair_command
    assert "--insecure-local" in relay_pair_command("http://127.0.0.1:18080", "AAAA-BBBB-CCCC")
    assert "--insecure-local" not in relay_pair_command("https://x.ts.net", "AAAA-BBBB-CCCC")


def test_token_file_wins_over_the_windows_variable(tmp_path, monkeypatch):
    import proxychat_server.config as server_config
    monkeypatch.setattr(server_config, "_windows_user_token", lambda: "from-windows")
    (tmp_path / "token").write_text("from-file", encoding="utf-8")
    assert server_config.read_token(tmp_path / "token") == "from-file"
    assert server_config.read_token(tmp_path / "missing") == "from-windows"


def test_trial_mode_never_leaves_a_server(conn):
    bot = ServerBot(ServerConfig(Path("unused"), "token"), conn, dev_guild=G)
    stranger = LeavingGuild(99)
    run(bot.on_guild_join(stranger))
    run(bot.on_guild_remove(LeavingGuild(98)))
    assert not stranger.left and db.guild(conn, 98) is None


# ---- operator alerts ----------------------------------------------------------

def alert_bot(conn):
    bot, _ = server_bot(conn)
    sent = []

    async def alert(text):
        sent.append(text)

    bot.alert = alert
    return bot, sent


def test_alerts_report_new_audit_events_once(conn):
    db.approve_guild(conn, G, "cli")                     # before the bot first looks: not replayed
    bot, sent = alert_bot(conn)
    run(bot.check_alerts())
    assert sent == []
    db.set_guild_status(conn, G, "frozen", "cli")
    db.audit(conn, "cli", "pairing code", G)            # not an alerting action
    admin_cli.cmd_alert(conn, type("A", (), {"text": ["backup", "failed"]})(), lambda *_: None)
    run(bot.check_alerts())
    assert len(sent) == 1 and "guild frozen" in sent[0] and "backup failed" in sent[0]
    assert "pairing code" not in sent[0]
    run(bot.check_alerts())
    assert len(sent) == 1


def test_alerts_on_bursts_of_failed_sign_ins_once_per_hour(conn):
    bot, sent = alert_bot(conn)
    run(bot.check_alerts())
    for _ in range(21):
        db.note_auth_failure(conn, at=7200)
    run(bot.check_alerts())
    run(bot.check_alerts())
    assert len(sent) == 1 and "21 failed" in sent[0]


def test_a_refused_slot_alerts_once(conn):
    db.approve_guild(conn, G, "cli")
    bot, sent = alert_bot(conn)
    guild = FakeGuild([101])
    cat = guild._add(FakeChannel(guild, "Proxy Chat", discord.ChannelType.category))
    db.add_slot(conn, G, cat.id, "Game 1", "cli")
    bot.reload()
    bot.get_guild = lambda gid: guild
    slot = next(iter(bot.slots.values()))
    guild.me.guild_permissions = discord.Permissions(administrator=True)
    run(bot.reconcile(slot))
    run(bot.reconcile(slot))
    assert len(sent) == 1 and "administrator" in sent[0]
