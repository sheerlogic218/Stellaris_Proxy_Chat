"""Security tests for the relay-facing API (design §2 and §4), against the real aiohttp app."""

import asyncio
import json
import random

import pytest
from aiohttp.test_utils import TestClient, TestServer

from proxychat_core import signing
from proxychat_core.protocol import Comms, CountryId, SweepBegin
from proxychat_core.records import to_dict
from proxychat_core.state import Store
from proxychat_server import db
from proxychat_server.api import Api, health_app
from proxychat_server.config import load_server_config
from proxychat_server.queue import QueueSource
from proxychat_server.serverbot import relay_pair_command

K = "123-456-789"
G = 5
LAUNCH = "launch-0001"


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


class Env:
    """A database with one approved guild and two slots, an API on a fake clock, and a client."""

    def __init__(self, tmp_path):
        self.conn = db.connect(tmp_path / "p.db")
        db.approve_guild(self.conn, G, "cli")
        self.slot = db.add_slot(self.conn, G, 100, "Game 1", "cli")
        self.slot2 = db.add_slot(self.conn, G, 101, "Game 2", "cli")
        self.clock = Clock(1_800_000_000)
        self.mono = Clock(1000)
        self.api = Api(self.conn, "0.1.0", clock=self.clock, monotonic=self.mono)
        self.client = None

    def code(self, slot=None, at=None):
        return db.new_pairing_code(self.conn, G, slot or self.slot, 42, at=int(self.clock() if at is None else at))

    async def raw(self, path, body=b"{}", headers=None, method="POST"):
        h = {"Content-Type": "application/json", **(headers or {})}
        return await self.client.request(method, path, data=body, headers=h)

    async def pair(self, code=None, version="0.1.0", label="Volunteer", slot=None):
        key = signing.new_private_key()
        body = json.dumps({"code": code or self.code(slot), "public_key": signing.b64e(signing.public_bytes(key)),
                           "label": label, "relay_version": version}).encode()
        r = await self.raw("/v1/pair", body)
        data = await r.json() if r.status == 200 else None
        return r, key, (data or {}).get("key_id")

    async def signed(self, key, key_id, path, payload, now=None, body=None, sign_path=None):
        body = json.dumps(payload).encode() if body is None else body
        headers = signing.sign_headers(key, key_id, sign_path or path, body, now=self.clock() if now is None else now)
        return await self.raw(path, body, headers), headers, body

    async def events(self, key, key_id, records, seq_from=0, launch=LAUNCH):
        payload = {"launch": launch, "seq_from": seq_from, "records": [to_dict("2200.1.1", r) for r in records]}
        r, _, _ = await self.signed(key, key_id, "/v1/events", payload)
        return r

    def queued(self, slot=None):
        sql = "SELECT payload, slot_id, key_id FROM event_queue" + (" WHERE slot_id = ?" if slot else "") + " ORDER BY id"
        return self.conn.execute(sql, (slot,) if slot else ()).fetchall()


def scenario(fn):
    """Run an async test body with a live in-process API."""
    def test(tmp_path):
        env = Env(tmp_path)

        async def main():
            async with TestClient(TestServer(env.api.app())) as client:
                env.client = client
                await fn(env)

        asyncio.run(main())
    test.__name__ = fn.__name__
    return test


# ---- probing --------------------------------------------------------------------

@scenario
async def test_unknown_paths_and_methods_get_an_empty_404(env):
    for method, path in [("POST", "/"), ("POST", "/admin"), ("GET", "/v1/events"), ("PUT", "/v1/pair"),
                         ("POST", "/v1/events/"), ("POST", "/V1/EVENTS"), ("GET", "/healthz")]:
        r = await env.raw(path, method=method)
        assert r.status == 404, (method, path)
        assert await r.read() == b""


@scenario
async def test_no_server_header(env):
    r = await env.raw("/nope")
    assert "Server" not in r.headers


@scenario
async def test_unsigned_requests_are_refused_before_the_body_is_parsed(env):
    r = await env.raw("/v1/events", b"{definitely not json")
    assert r.status == 401 and await r.read() == b""
    r = await env.raw("/v1/events", b"{}", {signing.HEADER_KEY: "k_doesnotexist"})
    assert r.status == 401
    assert db.get_setting(env.conn, "auth_failures").endswith(":2")


@scenario
async def test_unknown_keys_share_the_small_anonymous_budget(env):
    statuses = [(await env.raw("/v1/events", b"{}", {signing.HEADER_KEY: "k_x"})).status for _ in range(35)]
    assert statuses[:30] == [401] * 30 and set(statuses[30:]) == {429}


# ---- bodies ---------------------------------------------------------------------

@scenario
async def test_oversized_and_wrongly_typed_bodies(env):
    r, key, key_id = await env.pair()
    big = b'{"x": "' + b"a" * (17 * 1024) + b'"}'
    r, _, _ = await env.signed(key, key_id, "/v1/events", None, body=big)
    assert r.status == 413
    r = await env.client.post("/v1/pair", data=b"code=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 415


# ---- pairing --------------------------------------------------------------------

@scenario
async def test_pairing_works_once(env):
    code = env.code()
    r, key, key_id = await env.pair(code)
    assert r.status == 200 and key_id.startswith("k_")
    assert (await r.json())["slot"] == "Game 1"
    row = db.active_relay(env.conn, key_id)
    assert row["slot_id"] == env.slot and row["public_key"] == signing.public_bytes(key)
    r, _, _ = await env.pair(code)
    assert r.status == 401


@scenario
async def test_pairing_code_expires_and_bad_codes_fail(env):
    code = env.code(at=env.clock() - db.PAIRING_SECONDS - 1)
    assert (await env.pair(code))[0].status == 401
    assert (await env.pair("AAAA-AAAA-AAAA"))[0].status == 401        # well-formed, never issued
    assert (await env.pair("not a code"))[0].status == 401


@scenario
async def test_pairing_codes_accept_lowercase_and_spaces(env):
    code = env.code().lower().replace("-", " ")
    assert (await env.pair(code))[0].status == 200


@scenario
async def test_old_relays_are_told_to_update(env):
    env.api.min_relay_version = "0.2.0"
    r, _, _ = await env.pair(version="0.1.9")
    assert r.status == 426 and (await r.json())["min_relay_version"] == "0.2.0"


@scenario
async def test_malformed_pairing_requests(env):
    good = {"code": env.code(), "public_key": signing.b64e(b"\x01" * 32), "label": "x", "relay_version": "0.1.0"}
    for change in [{"public_key": signing.b64e(b"short")}, {"public_key": 5}, {"label": "@@@"}, {"label": 7},
                   {"relay_version": "latest"}, {"extra": 1}]:
        body = json.dumps({**good, **change}).encode()
        assert (await env.raw("/v1/pair", body)).status == 400, change
    assert (await env.raw("/v1/pair", b"[]")).status == 400


@scenario
async def test_pairing_brute_force_is_throttled(env):
    statuses = [(await env.pair("AAAA-AAAA-AAAA"))[0].status for _ in range(12)]
    assert statuses[:10] == [401] * 10 and statuses[10:] == [429, 429]


@scenario
async def test_pairing_fails_for_a_frozen_guild(env):
    code = env.code()
    db.set_guild_status(env.conn, G, "frozen", "cli")
    assert (await env.pair(code))[0].status == 401


# ---- signatures -------------------------------------------------------------------

@scenario
async def test_signed_events_are_queued_for_the_relays_own_slot(env):
    _, key, key_id = await env.pair()
    r = await env.events(key, key_id, [SweepBegin(K), Comms(K, 1, 2)])
    data = await r.json()
    assert r.status == 200 and data["lease"] == "held" and data["acked"] == 1
    rows = env.queued()
    assert [(row["slot_id"], row["key_id"]) for row in rows] == [(env.slot, key_id)] * 2


@scenario
async def test_a_replayed_request_is_refused(env):
    _, key, key_id = await env.pair()
    r, headers, body = await env.signed(key, key_id, "/v1/heartbeat", {})
    assert r.status == 200
    assert (await env.raw("/v1/heartbeat", body, headers)).status == 401


@scenario
async def test_clock_skew_is_limited(env):
    _, key, key_id = await env.pair()
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}, now=env.clock() - 61))[0].status == 401
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}, now=env.clock() + 61))[0].status == 401
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}, now=env.clock() - 59))[0].status == 200


@scenario
async def test_wrong_key_tampered_body_or_path_fail(env):
    _, key, key_id = await env.pair()
    other = signing.new_private_key()
    assert (await env.signed(other, key_id, "/v1/heartbeat", {}))[0].status == 401
    body = b"{}"
    headers = signing.sign_headers(key, key_id, "/v1/heartbeat", b'{"a": 1}', now=env.clock())
    assert (await env.raw("/v1/heartbeat", body, headers)).status == 401
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}, sign_path="/v1/release"))[0].status == 401


@scenario
async def test_revoked_keys_stop_working_at_once(env):
    _, key, key_id = await env.pair()
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}))[0].status == 200
    db.revoke_relay(env.conn, key_id, "cli")
    assert (await env.signed(key, key_id, "/v1/heartbeat", {}))[0].status == 401


# ---- what a relay can and can't do ----------------------------------------------------

@scenario
async def test_a_relay_cant_name_another_slot(env):
    _, key, key_id = await env.pair()
    payload = {"launch": LAUNCH, "seq_from": 0, "records": [to_dict(None, Comms(K, 1, 2))], "slot_id": env.slot2}
    assert (await env.signed(key, key_id, "/v1/events", payload))[0].status == 400
    assert env.queued() == []


@scenario
async def test_one_bad_record_rejects_the_whole_batch(env):
    _, key, key_id = await env.pair()
    payload = {"launch": LAUNCH, "seq_from": 0,
               "records": [to_dict(None, Comms(K, 1, 2)), {"t": "COMMS", "date": None, "key": K, "a": 1, "b": 1}]}
    assert (await env.signed(key, key_id, "/v1/events", payload))[0].status == 400
    assert env.queued() == []


@scenario
async def test_batch_shape_limits(env):
    _, key, key_id = await env.pair()
    rec = to_dict(None, Comms(K, 1, 2))
    for payload in [{"launch": LAUNCH, "seq_from": 0, "records": []},
                    {"launch": LAUNCH, "seq_from": 0, "records": [rec] * 201},
                    {"launch": "short", "seq_from": 0, "records": [rec]},
                    {"launch": LAUNCH, "seq_from": -1, "records": [rec]},
                    {"launch": LAUNCH, "seq_from": True, "records": [rec]},
                    {"launch": LAUNCH, "records": [rec]}]:
        env.mono.t += 10   # keep the per-relay budget topped up
        r, _, _ = await env.signed(key, key_id, "/v1/events", payload)
        assert r.status in (400, 413), payload["records"][:1]


@scenario
async def test_names_reach_the_queue_cleaned(env):
    _, key, key_id = await env.pair()
    await env.events(key, key_id, [CountryId(K, 1, True, "@everyone **x**‮", "<@1>")])
    payload = json.loads(env.queued()[0]["payload"])
    assert (payload["name"], payload["ruler"]) == ("everyone x", "1")


@scenario
async def test_duplicate_records_after_a_restart_are_skipped(env):
    _, key, key_id = await env.pair()
    recs = [Comms(K, 1, 2), Comms(K, 2, 3), Comms(K, 3, 4)]
    await env.events(key, key_id, recs, seq_from=0)
    r = await env.events(key, key_id, recs, seq_from=0)          # relay restarted and replayed
    assert (await r.json())["acked"] == 2 and len(env.queued()) == 3
    r = await env.events(key, key_id, recs, seq_from=2)          # overlaps by one
    assert (await r.json())["acked"] == 4 and len(env.queued()) == 5
    r = await env.events(key, key_id, recs[:1], seq_from=0, launch="launch-0002")   # a new game launch
    assert len(env.queued()) == 6


# ---- leases ---------------------------------------------------------------------------

@scenario
async def test_only_the_lease_holder_feeds_a_slot(env):
    _, key_a, a = await env.pair(label="Alice")
    _, key_b, b = await env.pair(label="Bob")
    assert (await (await env.events(key_a, a, [Comms(K, 1, 2)])).json())["lease"] == "held"
    r = await env.events(key_b, b, [Comms(K, 5, 6)])
    assert (await r.json())["lease"] == "denied"
    assert [row["key_id"] for row in env.queued()] == [a]

    env.clock.t += db.LEASE_SECONDS + 1                           # Alice's PC crashed
    env.mono.t += 10
    r = await env.events(key_b, b, [Comms(K, 5, 6)], launch="launch-bob1")
    assert (await r.json())["lease"] == "held"


@scenario
async def test_release_frees_the_slot(env):
    _, key_a, a = await env.pair(label="Alice")
    _, key_b, b = await env.pair(label="Bob")
    await env.signed(key_a, a, "/v1/heartbeat", {})
    assert (await (await env.signed(key_b, b, "/v1/release", {}))[0].json())["ok"]   # not Bob's: no-op
    assert (await (await env.signed(key_b, b, "/v1/heartbeat", {}))[0].json())["lease"] == "denied"
    await env.signed(key_a, a, "/v1/release", {})
    assert (await (await env.signed(key_b, b, "/v1/heartbeat", {}))[0].json())["lease"] == "held"


@scenario
async def test_relays_of_different_slots_dont_compete(env):
    _, key_a, a = await env.pair(label="Alice")
    _, key_c, c = await env.pair(label="Carol", slot=env.slot2)
    assert (await (await env.signed(key_a, a, "/v1/heartbeat", {}))[0].json())["lease"] == "held"
    assert (await (await env.signed(key_c, c, "/v1/heartbeat", {}))[0].json())["lease"] == "held"


# ---- budgets and maintenance --------------------------------------------------------------

@scenario
async def test_per_relay_request_budget(env):
    _, key, key_id = await env.pair()
    statuses = [(await env.signed(key, key_id, "/v1/heartbeat", {}))[0].status for _ in range(12)]
    assert statuses[:10] == [200] * 10 and set(statuses[10:]) == {429}


@scenario
async def test_per_relay_record_budget(env):
    _, key, key_id = await env.pair()
    statuses = []
    for i in range(3):
        env.mono.t += 1   # requests allowed; records barely refill
        r = await env.events(key, key_id, [Comms(K, 1, 2)] * 200, seq_from=i * 200)
        statuses.append(r.status)
    assert statuses == [200, 200, 429]


@scenario
async def test_maintenance_answers_503(env):
    db.set_setting(env.conn, "maintenance", "1")
    env.api._maintenance = (0.0, False)
    _, _, _ = await env.pair()
    r = await env.raw("/v1/pair", b"{}")
    assert r.status == 503 and r.headers["Retry-After"] == "60"


# ---- fuzzing ---------------------------------------------------------------------------

def junk(rng, depth=0):
    kind = rng.randrange(8 if depth < 3 else 5)
    return [lambda: None, lambda: rng.choice([True, False]), lambda: rng.randint(-2**40, 2**40),
            lambda: rng.random() * 1e6, lambda: "".join(chr(rng.randrange(0, 0x3000)) for _ in range(rng.randrange(20))),
            lambda: [junk(rng, depth + 1) for _ in range(rng.randrange(4))],
            lambda: {rng.choice(["t", "key", "a", "b", "date", "name", "x"]): junk(rng, depth + 1)
                     for _ in range(rng.randrange(5))},
            lambda: dict(to_dict("2200.1.1", Comms(K, 1, 2)), **{rng.choice(["a", "b", "key", "date", "t"]): junk(rng, depth + 1)}),
            ][kind]()


@scenario
async def test_fuzzed_bodies_never_cause_a_server_error(env):
    rng = random.Random(1234)
    _, key, key_id = await env.pair()
    for i in range(300):
        env.mono.t += 10
        records = [junk(rng) for _ in range(rng.randrange(1, 4))]
        payload = rng.choice([{"launch": LAUNCH, "seq_from": i, "records": records}, junk(rng), records])
        body = json.dumps(payload).encode()
        r, _, _ = await env.signed(key, key_id, rng.choice(["/v1/events", "/v1/heartbeat", "/v1/release"]),
                                   None, body=body)
        assert r.status in (200, 400, 413, 429), (r.status, payload)
        r = await env.raw("/v1/pair", body)
        assert r.status in (400, 401, 413, 426, 429), (r.status, payload)
    for row in env.queued():
        assert json.loads(row["payload"])["t"]   # only well-formed records got through


# ---- the whole path ---------------------------------------------------------------------

@scenario
async def test_relay_to_bot_state_end_to_end(env):
    _, key, key_id = await env.pair()
    await env.events(key, key_id, [SweepBegin(K), CountryId(K, 1, True, "Rocks", "r")])
    source = QueueSource(env.conn)
    batch, ids, dropped = source.poll()
    store = Store()
    from proxychat_core.state import Processor
    proc = Processor(store)
    for e in batch.events:
        assert e.slot_id == env.slot
        proc.feed(e.date, e.record)
    source.ack(ids)
    assert store.current.countries[1].name == "Rocks" and dropped == 0


def test_health_endpoint():
    async def main():
        async with TestClient(TestServer(health_app())) as client:
            r = await client.get("/healthz")
            return r.status, await r.text()
    assert asyncio.run(main()) == (200, "ok")


# ---- config and helpers ------------------------------------------------------------------

def test_placeholders_count_as_unset(tmp_path):
    (tmp_path / "s.toml").write_text('[server]\nowner_user_id = "CHANGE_ME_OWNER_USER_ID"\n'
                                     'public_url = "CHANGE_ME_PUBLIC_URL"\n', encoding="utf-8")
    cfg = load_server_config(tmp_path / "s.toml")
    assert cfg.owner_user_id == 0 and cfg.public_url == ""
    assert set(cfg.unset) == {"owner_user_id", "public_url"}


def test_the_example_config_parses(tmp_path):
    from pathlib import Path
    from proxychat_server.config import load_api_config
    example = Path(__file__).resolve().parent.parent / "server.example.toml"
    assert set(load_server_config(example).unset) == {"owner_user_id", "public_url"}
    assert load_api_config(example).listen_port == 8080


def test_pair_command_shows_url_and_code():
    text = relay_pair_command("https://x.example.ts.net", "ABCD-EFGH-IJKL")
    assert "https://x.example.ts.net" in text and "ABCD-EFGH-IJKL" in text


def test_signature_verify_never_raises_on_garbage():
    key = signing.new_private_key()
    pub = signing.public_bytes(key)
    for sig in ["", "!!!", "a" * 500, None, 5]:
        assert not signing.verify(pub, "POST", "/v1/events", "1", "n" * 16, b"", sig)
    assert not signing.verify(b"short", "POST", "/", "1", "n", b"", signing.b64e(b"x" * 64))
