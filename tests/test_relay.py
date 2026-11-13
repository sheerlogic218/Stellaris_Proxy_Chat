"""The relay against the real API, on a background thread over localhost."""

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest
from aiohttp import web

from proxychat_core import signing
from proxychat_relay import __main__ as relay_cli
from proxychat_relay import config as relay_config
from proxychat_relay.client import Client, RelayError, Revoked, UpdateNeeded, check_url
from proxychat_relay.keys import MemoryKeyStore
from proxychat_relay.relay import LAUNCH_HEAD, MAX_PENDING, Relay
from proxychat_server import db
from proxychat_server.api import Api

K = "123-456-789"
G = 5
PREFIX = "[10:50:45][effect_impl.cpp:22217]: [2203.12.4] Log effect, file: events/proxychat_events.txt line: 16. "


class ThreadServer:
    """Runs an aiohttp app (made inside the thread) on 127.0.0.1 in a background thread."""

    def __init__(self, make_app):
        self.make_app = make_app
        self.ready = threading.Event()
        self.loop = None
        self.port = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def start():
            self.runner = web.AppRunner(self.make_app(), access_log=None)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]

        self.loop.run_until_complete(start())
        self.ready.set()
        self.loop.run_forever()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        async def stop():
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.db_path = tmp_path / "server.db"
        self.conn = db.connect(self.db_path)
        db.approve_guild(self.conn, G, "cli")
        self.slot = db.add_slot(self.conn, G, 100, "Game 1", "cli")
        self.api = None

        def make_app():
            # The API gets its own connection, made on its own thread
            self.api = Api(db.connect(self.db_path))
            return self.api.app()

        self.server = ThreadServer(make_app)
        self.store = MemoryKeyStore()
        self.log = tmp_path / "game.log"
        self.said = []

    def code(self):
        return db.new_pairing_code(self.conn, G, self.slot, 42)

    def cli(self, *args, config_dir=None):
        out = []
        code = relay_cli.main(["--config-dir", str(config_dir or self.tmp / "relay"), "--insecure-local", *args],
                              store=self.store, out=out.append)
        return code, "\n".join(out)

    def pair(self, label="Alice", config_dir=None):
        code, text = self.cli("pair", self.server.url, self.code(), "--label", label, "--log", str(self.log),
                              config_dir=config_dir)
        assert code == 0, text
        return relay_config.load((config_dir or self.tmp / "relay") / "relay.toml")

    def relay(self, cfg):
        client = Client(cfg.server_url, cfg.key_id, self.store.get(cfg.key_id), allow_insecure_local=True)
        return Relay(cfg, client, say=self.said.append)

    def write_log(self, *records, head="A"):
        lines = [f"[00:00:01][game.cpp:1]: Game starting {head * LAUNCH_HEAD}"]
        lines += [PREFIX + "PROXYCHAT|" + r for r in records]
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def append_log(self, *records):
        with self.log.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(PREFIX + "PROXYCHAT|" + r + "\n")

    def queued(self):
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM event_queue ORDER BY id")]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.server.close()


# ---- pairing and the CLI ---------------------------------------------------------

def test_pair_stores_the_key_outside_the_config(world):
    cfg = world.pair()
    assert cfg.key_id.startswith("k_") and cfg.slot == "Game 1" and cfg.label == "Alice"
    assert world.store.get(cfg.key_id) is not None
    text = (world.tmp / "relay" / "relay.toml").read_text(encoding="utf-8")
    private = signing.b64e(world.store.keys[cfg.key_id])
    assert private not in text and "key_id" in text
    row = db.active_relay(world.conn, cfg.key_id)
    assert row["public_key"] == signing.public_bytes(world.store.get(cfg.key_id))
    code, status = world.cli("status")
    assert code == 0 and "Game 1" in status and "stored in Credential Manager" in status


def test_pair_with_a_bad_code_leaves_nothing_behind(world):
    code, text = world.cli("pair", world.server.url, "AAAA-AAAA-AAAA", "--label", "Alice")
    assert code == 1 and "didn't accept" in text
    assert relay_config.load(world.tmp / "relay" / "relay.toml") is None and world.store.keys == {}


def test_pairing_again_replaces_the_old_key(world):
    first = world.pair()
    second = world.pair()
    assert first.key_id != second.key_id
    assert world.store.get(first.key_id) is None and world.store.get(second.key_id) is not None


def test_forget_removes_config_and_key(world):
    cfg = world.pair()
    assert world.cli("forget")[0] == 0
    assert world.store.keys == {} and relay_config.load(world.tmp / "relay" / "relay.toml") is None
    code, text = world.cli("run")
    assert code == 1 and "isn't paired" in text


def test_run_without_a_stored_key_explains(world):
    cfg = world.pair()
    world.store.delete(cfg.key_id)
    code, text = world.cli("run")
    assert code == 1 and "missing" in text


def test_label_is_required_to_be_usable(world):
    code, text = world.cli("pair", world.server.url, world.code(), "--label", "@@@")
    assert code == 2


# ---- URLs and transport ------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://example.com", "ftp://x", "https://", "example.com",
                                 "https://x.ts.net/?a=1", "https://user:pw@x.ts.net", "http://localhost:8080"])
def test_bad_server_urls_are_refused(url):
    with pytest.raises(RelayError):
        check_url(url)


def test_good_server_urls():
    assert check_url("https://proxychat.tail1234.ts.net/") == "https://proxychat.tail1234.ts.net"
    assert check_url("http://127.0.0.1:9", allow_insecure_local=True) == "http://127.0.0.1:9"


def test_redirects_are_never_followed(tmp_path):
    hits = []

    def make_app():
        async def redirect(request):
            raise web.HTTPTemporaryRedirect("/elsewhere")

        async def elsewhere(request):
            hits.append(dict(request.headers))
            return web.json_response({"key_id": "k_stolen"})

        app = web.Application()
        app.router.add_post("/v1/pair", redirect)
        app.router.add_post("/elsewhere", elsewhere)
        return app

    server = ThreadServer(make_app)
    try:
        client = Client(server.url, allow_insecure_local=True)
        with pytest.raises(RelayError, match="redirect"):
            client.pair("AAAA-AAAA-AAAA", b"\x01" * 32, "x")
        assert hits == []
    finally:
        server.close()


# ---- sending ---------------------------------------------------------------------

def test_records_reach_the_queue_and_a_restart_sends_no_duplicates(world):
    cfg = world.pair()
    world.write_log(f"SWEEP_BEGIN|{K}", f"ID|{K}|1|H|Rocks|r", f"COMMS|{K}|1|2", "noise that isn't ours")
    relay = world.relay(cfg)
    relay.step()
    assert [r["t"] for r in world.queued()] == ["SWEEP_BEGIN", "ID", "COMMS"]
    assert relay.lease == "held" and not relay.pending
    assert any("Hosting Game 1" in s for s in world.said)

    again = world.relay(cfg)           # relay restarted mid-game: replays the log
    again.step()
    assert len(world.queued()) == 3

    world.append_log(f"COMMS|{K}|2|3")
    again.step()
    assert [r["a"] for r in world.queued()[3:]] == [2]


def test_only_the_mods_records_are_sent(world):
    cfg = world.pair()
    world.write_log(f"COMMS|{K}|1|2")
    with world.log.open("a", encoding="utf-8") as f:
        f.write("[00:00:02][x.cpp:1]: C:\\Users\\SomeoneReal\\Documents secret path\n")
    world.relay(cfg).step()
    assert "SomeoneReal" not in json.dumps(world.queued())


def test_a_new_game_launch_is_followed(world):
    cfg = world.pair()
    world.write_log(f"COMMS|{K}|1|2", head="A")
    relay = world.relay(cfg)
    relay.step()
    world.write_log(f"COMMS|{K}|5|6", head="B")   # Stellaris restarted: a new log from the top
    relay.step()
    assert [(r["a"], r["b"]) for r in world.queued()] == [(1, 2), (5, 6)]
    assert any("restarted" in s for s in world.said)


def test_nothing_is_read_until_the_log_has_a_head(world):
    cfg = world.pair()
    world.log.write_text(PREFIX + f"PROXYCHAT|COMMS|{K}|1|2\n", encoding="utf-8")   # shorter than LAUNCH_HEAD
    relay = world.relay(cfg)
    assert relay.read_log() == 0 and not relay.pending


def test_a_second_host_waits_for_the_lease(world):
    alice = world.pair("Alice", config_dir=world.tmp / "a")
    bob = world.pair("Bob", config_dir=world.tmp / "b")
    world.write_log(f"COMMS|{K}|1|2")
    a, b = world.relay(alice), world.relay(bob)
    a.step()
    b.step()
    assert a.lease == "held" and b.lease == "denied"
    assert len(world.queued()) == 1 and len(b.pending) == 1      # Bob keeps his records
    assert any("Another host" in s for s in world.said)

    a.stop()                                                     # Alice stops: frees the slot
    b.next_try = 0
    b.step()
    assert b.lease == "held" and not b.pending


def test_a_revoked_relay_stops(world):
    cfg = world.pair()
    world.write_log(f"COMMS|{K}|1|2")
    relay = world.relay(cfg)
    relay.step()
    db.revoke_relay(world.conn, cfg.key_id, "cli")
    world.append_log(f"COMMS|{K}|2|3")
    with pytest.raises(Revoked):
        relay.step()
    assert relay.run(should_stop=lambda: False) == 1


def test_an_outdated_relay_is_told_to_update(world):
    cfg = world.pair()
    world.api.min_relay_version = "9.0.0"
    world.write_log(f"COMMS|{K}|1|2")
    with pytest.raises(UpdateNeeded):
        world.relay(cfg).step()


def test_an_unreachable_server_backs_off_and_keeps_records(world, tmp_path):
    cfg = world.pair()
    cfg.server_url = "http://127.0.0.1:9"     # nothing listens there
    world.write_log(f"COMMS|{K}|1|2")
    relay = world.relay(cfg)
    relay.step()
    assert len(relay.pending) == 1 and relay.next_try > 0 and relay.backoff == 2
    assert any("Can't reach" in s for s in world.said)
    relay.step()                                # still waiting: doesn't hammer the server
    assert relay.backoff == 2


def test_pending_records_are_capped(world):
    cfg = world.pair()
    world.write_log(*[f"COMMS|{K}|1|2"] * (MAX_PENDING + 10))
    relay = world.relay(cfg)
    relay.read_log()
    assert len(relay.pending) == MAX_PENDING and relay.dropped == 10


def test_big_replays_are_sent_in_batches(world):
    cfg = world.pair()
    world.write_log(*[f"COMMS|{K}|{i}|{i + 1}" for i in range(1, 451)])
    relay = world.relay(cfg)
    relay.step()
    relay.step()
    relay.step()
    assert len(world.queued()) == 450 and not relay.pending


def test_no_secret_reaches_the_logs_or_messages(world, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = world.pair()
    world.write_log(f"COMMS|{K}|1|2")
    relay = world.relay(cfg)
    relay.step()
    relay.stop()
    private = signing.b64e(world.store.keys[cfg.key_id])
    everything = caplog.text + "\n".join(world.said)
    assert private not in everything and world.store.keys[cfg.key_id].hex() not in everything


# ---- config file -------------------------------------------------------------------

def test_config_round_trip_with_awkward_characters(tmp_path):
    cfg = relay_config.RelayConfig("https://x.ts.net", "k_1", 'Al "the" \\host', "Game 1",
                                   Path("C:\\Users\\A B\\game.log"), 2.0)
    relay_config.save(cfg, tmp_path / "relay.toml")
    assert relay_config.load(tmp_path / "relay.toml") == cfg
