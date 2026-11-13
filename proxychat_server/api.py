"""The relay-facing HTTP API: the only part of the central server on the internet.

  python -m proxychat_server.api [--config /config/server.toml]

It holds no Discord token and needs no outbound network. It authenticates relays,
checks what they send, and writes events into the database queue for the bot.

Every request goes through the same checks, in this order, and the cheap ones come
first so junk costs almost nothing:

  1. POST to one of four paths, else 404 (empty body)
  2. maintenance mode -> 503
  3. unauthenticated traffic (pairing, unknown keys) shares a small global budget -> 429
  4. Content-Type application/json and a Content-Length up to 16 KiB -> 415 / 411 / 413
  5. body read within 5 s -> 408
  6. signature (key known and active, clock within 60 s, nonce unused, Ed25519) -> 401
  7. per-relay request and record budgets -> 429
  8. strict JSON schema -> 400, then the handler

Error responses never say why: 401 is just 401. Logs carry the key id, path,
status and timing, never IP addresses.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import time

from aiohttp import web

from proxychat_core import signing
from proxychat_core.names import clean_name
from proxychat_core.records import RecordError, from_dict, to_dict

from . import db
from .ratelimit import BucketMap, TokenBucket

log = logging.getLogger("proxychat.api")

MAX_BODY = 16 * 1024
BODY_TIMEOUT = 5.0
CLOCK_SKEW = 60
NONCE_TTL = 120
MAX_NONCES = 100_000
MAX_BATCH = 200
ROUTES = ("/v1/pair", "/v1/events", "/v1/heartbeat", "/v1/release")
_LAUNCH = re.compile(r"[A-Za-z0-9_-]{8,64}")
_VERSION = re.compile(r"\d{1,4}(\.\d{1,4}){1,3}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,40}")


def version_tuple(text):
    return tuple(int(p) for p in text.split("."))


class Api:
    def __init__(self, conn, min_relay_version="0.1.0", clock=time.time, monotonic=time.monotonic):
        self.conn = conn
        self.min_relay_version = min_relay_version
        self.clock = clock
        # Budgets (design §4.6)
        self.anonymous = TokenBucket(30 / 60, 30, monotonic)       # unauthenticated, all together
        self.pairing = TokenBucket(10 / 60, 10, monotonic)         # pairing attempts, all together
        self.per_key = BucketMap(5, 10, monotonic)                 # requests per relay
        self.records = BucketMap(500 / 60, 500, monotonic)         # records per relay
        self.nonces = {}
        self._maintenance = (0.0, False)

    def app(self):
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("*", "/{tail:.*}", self.dispatch)
        app.on_response_prepare.append(_strip_server_header)
        return app

    # ---- the pipeline ------------------------------------------------------

    async def dispatch(self, request):
        started = time.monotonic()
        key_id = "-"
        try:
            response, key_id = await self._dispatch(request)
        except Exception:
            log.exception("Unhandled error on %s", request.path)
            response = web.Response(status=500)
        if request.method == "POST" and request.path in ROUTES:
            log.info("%s %s key=%s %.0fms", request.path, response.status, key_id,
                     (time.monotonic() - started) * 1000)
        return response

    async def _dispatch(self, request):
        if request.method != "POST" or request.path not in ROUTES:
            return web.Response(status=404), "-"
        if self.maintenance():
            return web.Response(status=503, headers={"Retry-After": "60"}), "-"

        pairing = request.path == "/v1/pair"
        relay = None
        if pairing:
            if not self.anonymous.take():
                return web.Response(status=429), "-"
        else:
            relay = db.active_relay(self.conn, request.headers.get(signing.HEADER_KEY))
            if relay is None:
                # Unknown key: counts against the unauthenticated budget, no signature check
                if not self.anonymous.take():
                    return web.Response(status=429), "-"
                db.note_auth_failure(self.conn, int(self.clock()))
                return web.Response(status=401), "-"

        if request.content_type != "application/json":
            return web.Response(status=415), "-"
        length = request.content_length
        if length is None:
            return web.Response(status=411), "-"
        if length > MAX_BODY:
            return web.Response(status=413), "-"
        try:
            body = await asyncio.wait_for(request.read(), BODY_TIMEOUT)
        except TimeoutError:
            return web.Response(status=408), "-"
        except web.HTTPRequestEntityTooLarge:
            return web.Response(status=413), "-"

        if pairing:
            if not self.pairing.take():
                return web.Response(status=429), "-"
            return self.pair(body), "-"

        key_id = relay["key_id"]
        if not self.authentic(relay, request, body):
            db.note_auth_failure(self.conn, int(self.clock()))
            return web.Response(status=401), key_id
        if not self.per_key.take(key_id):
            return web.Response(status=429), key_id
        handler = {"/v1/events": self.events, "/v1/heartbeat": self.heartbeat,
                   "/v1/release": self.release}[request.path]
        return handler(relay, body), key_id

    def maintenance(self):
        at, on = self._maintenance
        if time.monotonic() - at > 5:
            on = db.get_setting(self.conn, "maintenance", "0") == "1"
            self._maintenance = (time.monotonic(), on)
        return on

    def authentic(self, relay, request, body):
        h = request.headers
        timestamp, nonce, sig = h.get(signing.HEADER_TIME), h.get(signing.HEADER_NONCE), h.get(signing.HEADER_SIG)
        if not (timestamp and nonce and sig) or not timestamp.isdigit() or len(timestamp) > 12:
            return False
        now = self.clock()
        if abs(now - int(timestamp)) > CLOCK_SKEW or not _NONCE.fullmatch(nonce):
            return False
        if not signing.verify(relay["public_key"], "POST", request.path, timestamp, nonce, body, sig):
            return False
        # Only a correctly signed request uses up its nonce, so nobody can burn other relays' nonces
        self._forget_old_nonces(now)
        if nonce in self.nonces or len(self.nonces) >= MAX_NONCES:
            return False
        self.nonces[nonce] = now + NONCE_TTL
        return True

    def _forget_old_nonces(self, now):
        if len(self.nonces) > MAX_NONCES // 2:
            self.nonces = {n: exp for n, exp in self.nonces.items() if exp > now}

    # ---- handlers ----------------------------------------------------------

    def pair(self, body):
        d = _json_object(body, {"code", "public_key", "label", "relay_version"})
        if d is None:
            return web.Response(status=400)
        version = d["relay_version"]
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            return web.Response(status=400)
        if version_tuple(version) < version_tuple(self.min_relay_version):
            return web.json_response({"error": "update", "min_relay_version": self.min_relay_version}, status=426)
        code = db.normalize_code(d["code"])
        label = clean_name(d["label"], "", 32) if isinstance(d["label"], str) and len(d["label"]) <= 100 else ""
        try:
            public_key = signing.b64d(d["public_key"])
            signing.public_from_bytes(public_key)
        except (ValueError, TypeError):
            return web.Response(status=400)
        if not label:
            return web.Response(status=400)
        redeemed = db.redeem_pairing_code(self.conn, code, public_key, label, int(self.clock())) if code else None
        if redeemed is None:
            db.note_auth_failure(self.conn, int(self.clock()))
            return web.Response(status=401)
        key_id, slot = redeemed
        log.info("Relay %r paired to slot %s as %s", label, slot["slot_id"], key_id)
        return web.json_response({"key_id": key_id, "slot": slot["label"],
                                  "min_relay_version": self.min_relay_version})

    def events(self, relay, body):
        d = _json_object(body, {"launch", "seq_from", "records"})
        if d is None:
            return web.Response(status=400)
        launch, seq_from, records = d["launch"], d["seq_from"], d["records"]
        if (not isinstance(launch, str) or not _LAUNCH.fullmatch(launch)
                or isinstance(seq_from, bool) or not isinstance(seq_from, int) or not 0 <= seq_from <= 10**9
                or not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH):
            return web.Response(status=400)
        try:
            # Re-serialised from the parsed record, so only checked, cleaned values reach the queue
            payloads = [json.dumps(to_dict(*from_dict(r))) for r in records]
        except RecordError:
            return web.Response(status=400)
        key_id, slot_id = relay["key_id"], relay["slot_id"]
        if not self.records.take(key_id, len(payloads)):
            return web.Response(status=429)

        at = int(self.clock())
        with db.transaction(self.conn):
            self.conn.execute("UPDATE relays SET last_seen = ? WHERE key_id = ?", (at, key_id))
            last = db.last_seq(self.conn, slot_id, launch)
            if not db.take_lease(self.conn, slot_id, key_id, at):
                # Another relay is hosting this slot: nothing from this one goes in
                return self._reply(last, "denied")
            for i, payload in enumerate(payloads):
                if seq_from + i > last:
                    self.conn.execute("INSERT INTO event_queue (slot_id, key_id, payload, received_at) "
                                      "VALUES (?, ?, ?, ?)", (slot_id, key_id, payload, at))
            acked = max(last, seq_from + len(payloads) - 1)
            db.set_last_seq(self.conn, slot_id, launch, acked, at)
        return self._reply(acked, "held")

    def heartbeat(self, relay, body):
        if _json_object(body, set()) is None:
            return web.Response(status=400)
        at = int(self.clock())
        with db.transaction(self.conn):
            self.conn.execute("UPDATE relays SET last_seen = ? WHERE key_id = ?", (at, relay["key_id"]))
            held = db.take_lease(self.conn, relay["slot_id"], relay["key_id"], at)
        return self._reply(None, "held" if held else "denied")

    def release(self, relay, body):
        if _json_object(body, set()) is None:
            return web.Response(status=400)
        with db.transaction(self.conn):
            db.release_lease(self.conn, relay["slot_id"], relay["key_id"])
        return web.json_response({"ok": True})

    def _reply(self, acked, lease):
        return web.json_response({"ok": True, "acked": acked, "lease": lease,
                                  "min_relay_version": self.min_relay_version})


def _json_object(body, keys):
    """The body as a JSON object with exactly these keys, else None."""
    try:
        d = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or set(d) != set(keys):
        return None
    return d


async def _strip_server_header(request, response):
    response.headers.pop("Server", None)


def health_app():
    async def healthz(request):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.on_response_prepare.append(_strip_server_header)
    return app


async def serve(cfg):
    conn = db.connect(cfg.db_path)
    api = Api(conn, cfg.min_relay_version)
    runners = []
    for app, host, port in ((api.app(), cfg.listen_host, cfg.listen_port),
                            (health_app(), "127.0.0.1", cfg.health_port)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    log.info("API listening on %s:%s (health on 127.0.0.1:%s)", cfg.listen_host, cfg.listen_port, cfg.health_port)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main():
    from .config import DEFAULT_CONFIG, load_api_config

    parser = argparse.ArgumentParser(prog="proxychat_server.api")
    parser.add_argument("--config", default=os.environ.get("PROXYCHAT_SERVER_CONFIG", str(DEFAULT_CONFIG)))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve(load_api_config(args.config)))
    except KeyboardInterrupt:
        log.info("API stopped")


if __name__ == "__main__":
    main()
