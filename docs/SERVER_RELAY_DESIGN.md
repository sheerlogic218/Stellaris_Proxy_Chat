# Proxy Chat: central bot + relay design

Status: **draft for review**. Nothing here is built yet. Branch: `server-relay`.

**Goal:** one Proxy Chat bot, hosted on a home server, serves several Discord servers
and several games at the same time. Whoever hosts a Stellaris game runs a small
**relay** that reads their `game.log` and sends the mod's events to the bot.

**Top priority:** the security of the home server. Every choice below favours a small
attack surface and containment over convenience. Where there's a trade-off, it's called
out.

---

## 1. Overview

```
 Game host's PC (Windows)                 Tailscale Funnel                 Home server
 ┌──────────────────────┐    HTTPS     ┌───────────────────┐   ┌──────────────────────────────────────┐
 │ Stellaris → game.log │  signed POST │ public *.ts.net   │   │  [ts sidecar] → [api]  ─┐            │
 │ relay: tail + parse  │ ───────────► │ TLS ends at the   │──►│   no token,    no net   │ SQLite     │
 │ sends records only   │ ◄─────────── │ sidecar node      │   │                         ▼            │
 │ no inbound port      │   ack/lease  └───────────────────┘   │                      [bot] ──► Discord│
 └──────────────────────┘                                      │                   has token, egress │
                                                               │                   to Discord only   │
                                                               └──────────────────────────────────────┘
```

The data only goes one way: game → relay → API → database → bot → Discord. The bot never
sends anything back to the game. The relay only gets acknowledgements and lease status
back. That's what keeps the internet-facing part so small.

---

## 2. Threat model

| Asset | Why it matters |
|---|---|
| **Home server and LAN** | Your machine, your other services, your network. **Highest priority.** |
| Discord bot token | Whoever has it controls the bot in every server it's in. |
| Tailnet | A compromised node shouldn't reach your other Tailscale devices. |
| Players' data | Discord user IDs, claims, empire names. |
| Other servers' channels | One server's relay must never affect another server. |

| Attacker | What they might try |
|---|---|
| Internet scanners | The `*.ts.net` name is public in Certificate Transparency logs, so expect probes and fuzzing within days. |
| Random Discord users | Inviting the public bot to many servers to use up your resources or Discord rate limits. |
| A malicious or careless relay holder | Fake events, floods, oversized or abusive empire names, replayed requests. |
| A stolen relay key | Same as above, but it can only reach one slot. |
| A compromised dependency | Supply-chain risk in discord.py, aiohttp and similar, on the server or the relay. |

**Design rules that follow from this:**
1. Deny by default: unknown guilds, unknown keys and unknown paths get nothing.
2. The internet-facing process never holds the Discord token and can't reach the internet or the LAN.
3. Every request is authenticated before its body is parsed, and size and rate limits apply first.
4. Every relay credential is limited to **one guild and one slot**, and can be revoked on its own.
5. The server stores no reusable secrets for relays. It keeps only public keys and hashes.
6. The blast radius in Discord is kept small: the bot only ever gets permissions on its category.

---

## 3. Home server hardening (the top priority)

### 3.1 Process split: three containers

| Container | Inbound | Outbound | Secrets | Job |
|---|---|---|---|---|
| `ts` (Tailscale sidecar) | Funnel :443 | Tailscale control and DERP only | Tailscale auth key, used once | Ends TLS and forwards to `api:8080` on an internal network |
| `api` | Only from `ts`, internal network | **None** (internal-only network) | None | Authenticates, validates and rate-limits, then writes events to SQLite |
| `bot` | **None** | Discord only (`discord.com`, `gateway.discord.gg`) | Discord token (Docker secret file) | Applies events, runs slash commands, manages channels |

`api` and `bot` share **only** a data volume holding the SQLite database. If `api` is
compromised, the attacker gets no token, no internet and no LAN. The most they can do is
tamper with the database, and the bot treats the database as untrusted input
(section 7.3).

### 3.2 Container hardening (all three)
- Run as a non-root user with a fixed UID inside each container. On the host, rootful Docker with `userns-remap` maps container root to an unprivileged host UID. Rootless Docker would skip the `DOCKER-USER` chain that the LAN block relies on.
- `read_only: true` root filesystem, with `tmpfs` for `/tmp`. Only the data volume is writable, and only `api` and `bot` mount it.
- `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` and the default seccomp profile. `ts` gets only `NET_ADMIN` if it runs in kernel mode; use userspace networking so it needs no capabilities.
- Never mount the Docker socket. Use no host network and no privileged mode.
- Set `mem_limit`, `cpus` and `pids_limit` on each container so a flood can't starve the host.
- Use small, pinned base images (`python:3.x-slim` by digest). Install dependencies from `requirements.lock` with `--require-hashes`.

### 3.3 Network containment
- **Docker networks:**
  - `edge` is internal, with only `ts` and `api` on it.
  - `discord_egress` is used by `bot` only.
  - `api` has **no** network with internet access.
- **Host firewall:** in the `DOCKER-USER` chain, drop all container traffic to the LAN and private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`). That way even `bot` can't reach your router, NAS or other services.
- **Optional:** restrict `bot`'s egress to Discord through an egress proxy. This is nice to have, because Discord's IP ranges change.

### 3.4 Tailscale setup
- The `ts` sidecar joins as its **own node**, tagged `tag:proxychat`. It does **not** use your home server's existing Tailscale node, so a compromise can't ride your main node's tailnet access.
- **Auth key:** create a single-use, pre-authorised, tagged key. Tagged nodes don't expire.
- **ACL policy:**
  - Grant `tag:proxychat` **no** destinations and add no rule that lets it reach anything.
  - Allow Funnel for that tag only:
    ```jsonc
    "tagOwners": { "tag:proxychat": ["autogroup:admin"] },
    "nodeAttrs": [ { "target": ["tag:proxychat"], "attr": ["funnel"] } ],
    ```
  - ⚠️ If your policy still has the default allow-all rule (`src: ["*"], dst: ["*:*"]`), replace it with explicit rules **first**. Otherwise the new node can reach your whole tailnet.
- **Funnel:** serve only HTTPS 443 → `http://api:8080`. Your home IP is never exposed and the router has no open ports.
- **Off switch:** `docker exec ts tailscale funnel off` (or stopping `ts`) cuts all internet access straight away, and the bot keeps running.

### 3.5 Host hygiene
- Turn on automatic security updates for the OS.
- Rebuild the images weekly or when Dependabot or pip-audit flags something.
- Manage the host over Tailscale SSH or your LAN only, never through Funnel.
- **Backups:** each night, run `sqlite3 .backup`, encrypt it with `age`, and copy it off the box. The backup includes players' Discord IDs, so it's encrypted at rest.
- **Discord token:** keep it in a Docker secret file (`/run/secrets/discord_token`, mode 0400), not in an environment variable, because environment variables leak through `docker inspect` and crash dumps.

---

## 4. Relay ↔ server protocol

### 4.1 Endpoints
Everything else returns a bare 404 with no body.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/pair` | Pairing code | Swap a one-time code for a registered relay public key |
| POST | `/v1/events` | Signed | A batch of parsed records, which also renews the lease |
| POST | `/v1/heartbeat` | Signed | Renew the lease when there are no events |
| POST | `/v1/release` | Signed | Give up the lease when the relay shuts down cleanly |

- **What's left out:** there are no GET endpoints, uploads, redirects, CORS or HTML. The server header is minimal and there are no stack traces.
- **Before any parsing:** the request body is capped at **16 KiB**, a request must arrive within **5 s**, and `Content-Type` must be `application/json`.

### 4.2 Pairing: one-time code → Ed25519 key
1. **Admin runs the command.** A guild admin runs `/proxychat host slot:<slot>`. The bot replies **ephemerally** with a pairing code:
   - 12 base32 characters, about 60 bits;
   - single use, expires after **10 minutes**;
   - bound to `(guild, slot)`.

   The server stores only a SHA-256 of the code.
2. **The relay makes a key.** It generates an **Ed25519 key pair** locally. The private key goes into **Windows Credential Manager** through `keyring` and never leaves the PC.
3. **The relay registers the key.** It sends `POST /v1/pair {code, public_key, label, relay_version}`. The server checks that the code is unused and not expired, marks it used, and stores the public key against `(guild, slot)`.
4. **Brute force is ruled out.** Pairing is limited to 10 attempts a minute in total, and an alert fires after 20 failures in an hour. At 60 bits with a 10-minute lifetime, guessing a code isn't feasible.

### 4.3 Signed requests
Signed requests carry these headers:
```
X-PC-Key:   <key id>
X-PC-Time:  <unix seconds>
X-PC-Nonce: <16 random bytes, base64url>
X-PC-Sig:   Ed25519 over  "POST\n<path>\n<time>\n<nonce>\n<sha256(body) hex>"
```
The server rejects a request when:
- the key is unknown or revoked;
- the clock skew is more than **60 s**;
- the nonce was already seen in the last 120 s;
- the signature is bad.

It checks all of this before parsing the JSON body.

**Why signatures and not a bearer token:**
- A leaked database exposes nothing usable, because it holds only public keys.
- A captured request can't be replayed.
- The relay's secret never crosses the wire, not even inside TLS.

### 4.4 Event payload
The relay parses `game.log` **locally**, reusing today's `protocol.py`, and sends only
the structured records. It never sends raw log lines, which can contain file paths and
your Windows username.
```json
{ "launch": "<random id per game launch>", "seq_from": 120,
  "records": [ {"t":"COMMS","key":"123-456-789","a":1,"b":2,"date":"2200.1.1"}, ... ] }
```
- **Batch size:** at most **200 records** per batch.
- **Validation:** the server checks each record against a strict schema per type:
  - the type must be on the allowlist from `protocol.py`;
  - IDs are ints from 0 to 1,000,000;
  - keys match `^\d+-\d+-\d+$`;
  - dates match `^\d+\.\d+\.\d+$`;
  - names are at most 100 characters (Discord's channel-name limit), NFC-normalised, with control characters, zero-width characters and newlines stripped.

  It rejects the whole batch if any record fails.
- **Duplicates:** `launch` plus `seq` let the server ignore records it has already seen after a relay restart, which replays the log from the top.

### 4.5 Replies
`{"ok":true,"acked":<seq>,"lease":"held"|"denied","min_relay_version":"x.y"}`.
The reply never says *why* authentication failed: it's just 401.

### 4.6 Rate limits
| Scope | Limit |
|---|---|
| Unauthenticated requests (everything) | 30/min globally, then 429 |
| Per relay key | 5 requests/s, 500 records/min |
| Per slot | 3 new call channels/min, 20 voice moves/min |
| Per guild | 4 slots, 10 relay keys, 100 bot-owned channels |
| Global | `max_guilds` approved guilds (5) |

---

## 5. Tenancy model: guilds, slots, sessions

- **Guild:** a Discord server that you have **approved**. Approval is deny by default (section 6.1).
- **Slot:** one category in a guild that the server owner created and gave the bot permissions on. A slot hosts **one game at a time**, so two games at once need two slots, for example "Proxy Chat 1" and "Proxy Chat 2".
- **Session:** a `(slot, campaign key)` pair. This is today's `Store`/`Game` design, just per slot instead of per process. `state.py` already keeps several campaigns under `current_key`, so the logic mostly carries over: **one `Store` per slot**.
- **Relay key:** bound to exactly one `(guild, slot)`. Several volunteers can each pair a key to the same slot, but only one can hold the **lease** at a time:
  - the lease is renewed by each request or heartbeat every 30 s;
  - it expires after 90 s without one;
  - a second relay gets `lease: denied` and shows "Slot is hosted by <label> right now";
  - an admin can force-release it with `/proxychat lease release`.
- **Claims are per slot.** A player can hold a claim in more than one slot, but each slot's channels and permissions are separate.

**Isolation guarantee:** every database query and every Discord action is scoped by
`(guild_id, slot_id)` taken from the **authenticated key**, never from the request body.
A relay can't name another guild or slot.

---

## 6. Discord side

### 6.1 Onboarding: deny by default
1. **Public Bot** is on, so others can invite it. But:
2. When the bot joins a guild that isn't on the approved list, it **leaves straight away**. It first tries to DM the person who invited it with "ask the operator to approve guild <id>".
3. You approve a guild **from the server CLI**, not from Discord, so a hijacked Discord account can't approve guilds:
   `docker exec bot proxychat-admin guild approve <guild_id> --max-slots 2`.

### 6.2 Permissions (unchanged in spirit from `SERVER_SETUP.md`)
- **At the server level:** the invite grants only View Channels, Connect, Speak and Video (`3147264`).
- **Per category:** each slot category gives the bot Manage Channels, Manage Permissions and Move Members.
- **Refusing dangerous setups:** on setup and every hour, the bot refuses to operate in a guild where its role has **Administrator**, **Manage Server**, **Manage Roles** or server-wide **Manage Channels**. It tells the admins why. That keeps the damage small if the token ever leaks.

### 6.3 Commands
- **Registered globally.** Slash commands are registered globally rather than with today's per-guild `copy_global_to`/`sync`.
- **`/claim`, `/contacts`, `/empires`, `/unclaim`:** they apply to the slot of the category the command is run in, or take a `slot:` option.
- **`/proxychat setup|slot add|slot remove|host|relays|relay revoke|lease release|cleanup`:**
  - they need Manage Server or a configured admin role;
  - they're always ephemeral;
  - every one is written to the audit log.
- **Between sessions:** every command works with no relay connected. New admin commands are `/proxychat claims clear [slot]` and `/proxychat session end [slot]`. The second archives the current campaign, deletes its channels, and optionally clears its claims.
- **Mentions:** `allowed_mentions=none` is set **globally** on the client. Today it's only set per message. That way no empire name can ping `@everyone`.

### 6.4 Names coming from games
Empire and federation names reach channel names and DMs. On top of the API's checks
(section 4.4), the bot:
- removes `@`, `#`, `<`, `>` and the Discord markdown control characters;
- truncates names to 100 characters, Discord's limit for channel names;
- falls back to `Empire <id>` if nothing is left.

---

## 7. Data

### 7.1 SQLite schema
| Table | Contents |
|---|---|
| `guilds` | guild_id, approved, max_slots, admin_role_ids, created/removed timestamps |
| `slots` | slot_id, guild_id, category_id, commands_channel_id, label |
| `relays` | key_id, guild_id, slot_id, ed25519_pubkey, label, paired_by (user id), created, last_seen, revoked |
| `pairing_codes` | sha256(code), guild_id, slot_id, expires, used |
| `leases` | slot_id, key_id, expires |
| `slot_state` | slot_id, JSON of the per-slot `Store` (today's `state.json` shape) |
| `event_queue` | id, slot_id, key_id, payload, received (written by `api`, consumed by `bot`) |
| `audit_log` | time, actor (user id or key id), guild, action, details |

### 7.2 Retention and privacy
- **What's stored:** only Discord user IDs, empire and federation names, and channel IDs. There's no message content, and the IP addresses of relay requests aren't logged.
- **Guild removal:** when the bot leaves or is removed from a guild, that guild's data is deleted after 7 days.
- **Expiry:**
  - finished sessions are deleted after 30 days without activity;
  - the audit log is kept for 90 days;
  - container logs are kept for 14 days.
- **Privacy note:** write a short `PRIVACY.md` saying what's stored and how to get it deleted.

### 7.3 The bot doesn't trust the queue
`bot` validates every queued payload again with the same schema code, and checks that
the `key_id` is still active and still bound to that slot. That way a compromised `api`
container can't make the bot act outside the slot the key belongs to.

---

## 8. The relay (game host's PC)

- **Interface:** a small CLI or tray app: `proxychat-relay pair <url> <code>`, then `proxychat-relay run`.
- **Files and network:**
  - it reads only `game.log`;
  - it opens no ports and accepts nothing inbound;
  - it talks only to the configured server URL, with TLS verification always on.
- **Contents:**
  - it reuses `logwatch.py` and `protocol.py` as they are today;
  - its private key is kept in Windows Credential Manager.
- **Updates:** it doesn't update itself. The server's `min_relay_version` makes the relay show "please update" and stop sending.
- **Distribution:**
  - a source zip with hash-pinned dependencies;
  - optionally an `.exe` built by GitHub Actions with build attestation and SHA256 sums;
  - no third-party download hosts.
- **Moving between PCs:** the relay never exports its key. A volunteer on another PC pairs again, and the old key is revoked with `/proxychat relay revoke`.

---

## 9. Operations and kill switches

| Situation | Action |
|---|---|
| A relay key leaked or a volunteer left | `/proxychat relay revoke <label>`, or the server CLI |
| A guild is misbehaving | `proxychat-admin guild freeze <id>` (or `remove`, which also makes the bot leave) |
| Suspected API compromise | `tailscale funnel off`, then stop `api` and inspect. The bot and Discord keep working, with no new events. |
| Suspected token leak | Reset the token in the Discord developer portal, update the secret file, restart `bot` |
| Maintenance | `proxychat-admin maintenance on`: the API answers 503 and relays back off and retry |

**Alerts** go to your own DMs from the bot:
- a new guild asking for approval;
- more than 20 authentication failures an hour;
- a relay paired or revoked;
- a guild setup refused because of dangerous permissions.

---

## 10. Repo layout after the change

```
proxychat_core/      protocol.py, state.py (Store/Processor), names.py (sanitising)
proxychat_relay/     logwatch.py, client.py (signing, batching, lease), cli.py
proxychat_server/
  api/               app.py (aiohttp), auth.py, schema.py, ratelimit.py
  bot/               bot.py, embassy.py (multi-guild, multi-slot), queue.py
  db.py, admin_cli.py
deploy/              docker-compose.yml, Dockerfile.api, Dockerfile.bot, ts-serve.json,
                     tailscale-acl-snippet.jsonc, firewall-docker-user.sh, backup.sh
mod/                 unchanged
tests/               core + api auth/schema fuzz tests + tenancy isolation tests
```

**Local mode:** keep the current single-host mode as `proxychat_bot --local`, with its
own token and log, for people who'd rather self-host than use your server. It's the same
core with a different event source.

---

## 11. Build order

1. **Restructure.** Pull `protocol.py` and `state.py` out into `proxychat_core`. The existing tests stay green.
2. **Multi-guild, multi-slot bot (local mode):**
   - SQLite storage, with `Store` per slot;
   - globally registered commands and global `allowed_mentions=none`;
   - name sanitising and the dangerous-permission check.

   Still no network involved.
3. **API service:**
   - auth (pairing and signatures), schema, rate limits, writes to the queue;
   - tests for replay, clock skew, oversized bodies, wrong slot, revoked keys, and fuzzed records.
4. **Relay client:** pairing, keyring storage, batching, lease handling, back-off.
5. **Deployment:**
   - compose file, sidecar, ACLs, `DOCKER-USER` firewall rules, secrets, backups;
   - check from outside: unknown paths, unsigned requests, and floods all fail cheaply, and the containers can't reach the LAN.
6. **Migrate the main server:**
   - import `state.stellaris-server.json` as slot 1, so the owned channels and claims carry over;
   - volunteers pair their relays;
   - run alongside the old setup for one game.
7. **Open up:** docs (owner, host and player guides; `SECURITY.md`; `PRIVACY.md`), the guild approval flow, and the release.

---

## 12. Decisions (2026-11-13)

1. **Home server:** Pop!_OS (based on Ubuntu). It uses rootful Docker Engine from Docker's
   apt repo, with `userns-remap` turned on, plus the `DOCKER-USER` firewall rules.
   ufw doesn't filter Docker traffic, which is why those rules are needed.
   See the implementation plan, section 3.
2. **Local mode stays.** The point of the central bot is that **commands keep working while
   the game host's PC is off**, for example to clean up claims between sessions. So the bot
   has to be fully usable with no relay connected.
3. **Guild cap: 5** approved guilds.
4. **Guild approval: server CLI only.** There's no Discord command for it at all.

Step-by-step work for each section is in
[SERVER_RELAY_IMPLEMENTATION.md](SERVER_RELAY_IMPLEMENTATION.md).
