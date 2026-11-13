"""The central server's SQLite database, shared by the bot, the API and the admin CLI.

Each process opens its own connection. WAL mode lets the API write events while
the bot reads. Schema changes are numbered migrations applied by PRAGMA user_version.

Nothing here is trusted blindly by the bot: queued events are validated again
before they're applied (see queue.py).
"""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

DEFAULT_MAX_GUILDS = 5
DEFAULT_MAX_SLOTS = 2

GUILD_STATUSES = ("approved", "frozen", "removed")

MIGRATIONS = [
    # 1: everything in design §7.1
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE guilds (
        guild_id       INTEGER PRIMARY KEY,
        status         TEXT NOT NULL CHECK (status IN ('approved', 'frozen', 'removed')),
        max_slots      INTEGER NOT NULL DEFAULT 2 CHECK (max_slots BETWEEN 1 AND 10),
        admin_role_ids TEXT NOT NULL DEFAULT '[]',
        note           TEXT NOT NULL DEFAULT '',
        created_at     INTEGER NOT NULL,
        removed_at     INTEGER
    );
    CREATE TABLE slots (
        slot_id             TEXT PRIMARY KEY,
        guild_id            INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        category_id         INTEGER NOT NULL,
        label               TEXT NOT NULL,
        commands_channel    TEXT NOT NULL DEFAULT 'bot-commands',
        announce_channel_id INTEGER NOT NULL DEFAULT 0,
        created_at          INTEGER NOT NULL,
        UNIQUE (guild_id, category_id),
        UNIQUE (guild_id, label)
    );
    CREATE TABLE slot_state (
        slot_id    TEXT PRIMARY KEY REFERENCES slots(slot_id) ON DELETE CASCADE,
        state      TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE relays (
        key_id     TEXT PRIMARY KEY,
        guild_id   INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        slot_id    TEXT NOT NULL REFERENCES slots(slot_id) ON DELETE CASCADE,
        public_key BLOB NOT NULL,
        label      TEXT NOT NULL,
        paired_by  INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen  INTEGER,
        revoked_at INTEGER
    );
    CREATE TABLE pairing_codes (
        code_hash  TEXT PRIMARY KEY,
        guild_id   INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
        slot_id    TEXT NOT NULL REFERENCES slots(slot_id) ON DELETE CASCADE,
        created_by INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used_at    INTEGER
    );
    CREATE TABLE leases (
        slot_id    TEXT PRIMARY KEY REFERENCES slots(slot_id) ON DELETE CASCADE,
        key_id     TEXT NOT NULL REFERENCES relays(key_id) ON DELETE CASCADE,
        expires_at INTEGER NOT NULL
    );
    CREATE TABLE launch_seq (
        slot_id    TEXT NOT NULL REFERENCES slots(slot_id) ON DELETE CASCADE,
        launch     TEXT NOT NULL,
        last_seq   INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (slot_id, launch)
    );
    CREATE TABLE event_queue (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id     TEXT NOT NULL,
        key_id      TEXT NOT NULL,
        payload     TEXT NOT NULL,
        received_at INTEGER NOT NULL
    );
    CREATE TABLE audit_log (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        at       INTEGER NOT NULL,
        actor    TEXT NOT NULL,
        guild_id INTEGER,
        action   TEXT NOT NULL,
        details  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX audit_at ON audit_log(at);
    CREATE INDEX queue_slot ON event_queue(slot_id, id);
    """,
]


def now():
    return int(time.time())


def connect(path):
    """Open (and migrate) the database. One connection per process or thread."""
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)   # autocommit; see transaction()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        # Statement by statement: executescript() would commit the transaction early
        with transaction(conn):
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={number}")


def _statements(sql):
    return [s.strip() for s in sql.split(";") if s.strip()]


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---- settings ---------------------------------------------------------------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def max_guilds(conn):
    return int(get_setting(conn, "max_guilds", DEFAULT_MAX_GUILDS))


def bump_config(conn):
    """Tell the running bot that guilds or slots changed, so it reloads them."""
    version = int(get_setting(conn, "config_version", 0)) + 1
    set_setting(conn, "config_version", version)
    return version


def config_version(conn):
    return int(get_setting(conn, "config_version", 0))


# ---- audit ------------------------------------------------------------------

def audit(conn, actor, action, guild_id=None, details=""):
    conn.execute("INSERT INTO audit_log (at, actor, guild_id, action, details) VALUES (?, ?, ?, ?, ?)",
                 (now(), str(actor), guild_id, action, details))


# ---- guilds -----------------------------------------------------------------

class CapReached(Exception):
    pass


def guild(conn, guild_id):
    return conn.execute("SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)).fetchone()


def active_guild_count(conn):
    return conn.execute("SELECT COUNT(*) FROM guilds WHERE status IN ('approved', 'frozen')").fetchone()[0]


def approve_guild(conn, guild_id, actor, max_slots=DEFAULT_MAX_SLOTS, note=""):
    """Approve a guild (or re-approve a frozen or removed one). Raises CapReached past max_guilds."""
    with transaction(conn):
        row = guild(conn, guild_id)
        already = row is not None and row["status"] in ("approved", "frozen")
        if not already and active_guild_count(conn) >= max_guilds(conn):
            raise CapReached(f"already at the limit of {max_guilds(conn)} guilds")
        if row is None:
            conn.execute("INSERT INTO guilds (guild_id, status, max_slots, note, created_at) "
                         "VALUES (?, 'approved', ?, ?, ?)", (guild_id, max_slots, note, now()))
        else:
            conn.execute("UPDATE guilds SET status = 'approved', max_slots = ?, note = ?, removed_at = NULL "
                         "WHERE guild_id = ?", (max_slots, note or row["note"], guild_id))
        audit(conn, actor, "guild approve", guild_id, f"max_slots={max_slots}")
        bump_config(conn)


def set_guild_status(conn, guild_id, status, actor):
    """Freeze, unfreeze (approved) or remove a guild. Returns False if it isn't known."""
    assert status in GUILD_STATUSES
    with transaction(conn):
        row = guild(conn, guild_id)
        if row is None:
            return False
        removed_at = now() if status == "removed" else None
        conn.execute("UPDATE guilds SET status = ?, removed_at = ? WHERE guild_id = ?", (status, removed_at, guild_id))
        if status == "removed":
            # A removed guild's relays stop working at once; its data goes after retention
            conn.execute("UPDATE relays SET revoked_at = ? WHERE guild_id = ? AND revoked_at IS NULL",
                         (now(), guild_id))
        audit(conn, actor, f"guild {status}", guild_id)
        bump_config(conn)
        return True


def is_approved(conn, guild_id):
    row = guild(conn, guild_id)
    return row is not None and row["status"] == "approved"


def admin_roles(row):
    return tuple(int(r) for r in json.loads(row["admin_role_ids"]))


def set_admin_roles(conn, guild_id, role_ids, actor):
    with transaction(conn):
        conn.execute("UPDATE guilds SET admin_role_ids = ? WHERE guild_id = ?",
                     (json.dumps(sorted(set(role_ids))), guild_id))
        audit(conn, actor, "admin roles", guild_id, ",".join(str(r) for r in sorted(set(role_ids))))
        bump_config(conn)


# ---- slots ------------------------------------------------------------------

class SlotError(Exception):
    pass


def new_slot_id():
    return "s_" + secrets.token_hex(6)


def add_slot(conn, guild_id, category_id, label, actor, state=None):
    """Create a slot in an approved guild. Raises SlotError on any rule broken."""
    with transaction(conn):
        row = guild(conn, guild_id)
        if row is None or row["status"] != "approved":
            raise SlotError("this server isn't approved")
        count = conn.execute("SELECT COUNT(*) FROM slots WHERE guild_id = ?", (guild_id,)).fetchone()[0]
        if count >= row["max_slots"]:
            raise SlotError(f"this server already has its {row['max_slots']} slot(s)")
        if conn.execute("SELECT 1 FROM slots WHERE guild_id = ? AND (category_id = ? OR label = ?)",
                        (guild_id, category_id, label)).fetchone():
            raise SlotError("that category or label is already used by a slot")
        slot_id = new_slot_id()
        conn.execute("INSERT INTO slots (slot_id, guild_id, category_id, label, created_at) VALUES (?, ?, ?, ?, ?)",
                     (slot_id, guild_id, category_id, label, now()))
        if state is not None:
            conn.execute("INSERT INTO slot_state (slot_id, state, updated_at) VALUES (?, ?, ?)",
                         (slot_id, json.dumps(state), now()))
        audit(conn, actor, "slot add", guild_id, f"{label} category={category_id} id={slot_id}")
        bump_config(conn)
        return slot_id


def remove_slot(conn, slot_id, actor):
    with transaction(conn):
        row = conn.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM slots WHERE slot_id = ?", (slot_id,))
        conn.execute("DELETE FROM event_queue WHERE slot_id = ?", (slot_id,))
        audit(conn, actor, "slot remove", row["guild_id"], f"{row['label']} id={slot_id}")
        bump_config(conn)
        return True


def slots_of(conn, guild_id=None):
    if guild_id is None:
        return conn.execute("SELECT * FROM slots ORDER BY guild_id, label").fetchall()
    return conn.execute("SELECT * FROM slots WHERE guild_id = ? ORDER BY label", (guild_id,)).fetchall()


def active_slots(conn):
    """Slots of approved guilds: the only ones the bot serves."""
    return conn.execute("SELECT s.* FROM slots s JOIN guilds g USING (guild_id) "
                        "WHERE g.status = 'approved' ORDER BY s.guild_id, s.label").fetchall()


class SqliteBackend:
    """Store backend keeping a slot's state as JSON in slot_state."""

    def __init__(self, conn, slot_id):
        self.conn = conn
        self.slot_id = slot_id

    def read(self):
        row = self.conn.execute("SELECT state FROM slot_state WHERE slot_id = ?", (self.slot_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def write(self, data):
        self.conn.execute("INSERT INTO slot_state (slot_id, state, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(slot_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                          (self.slot_id, json.dumps(data), now()))


# ---- relays -----------------------------------------------------------------

def relays(conn, guild_id=None):
    sql = ("SELECT r.*, s.label AS slot_label FROM relays r JOIN slots s USING (slot_id)"
           + (" WHERE r.guild_id = ?" if guild_id is not None else "") + " ORDER BY r.created_at")
    return conn.execute(sql, () if guild_id is None else (guild_id,)).fetchall()


def revoke_relay(conn, key_id, actor, guild_id=None):
    """Revoke one key (only within guild_id, when given). Returns False if there was nothing to revoke."""
    with transaction(conn):
        sql = "UPDATE relays SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL"
        args = [now(), key_id]
        if guild_id is not None:
            sql += " AND guild_id = ?"
            args.append(guild_id)
        changed = conn.execute(sql, args).rowcount
        if changed:
            conn.execute("DELETE FROM leases WHERE key_id = ?", (key_id,))
            audit(conn, actor, "relay revoke", guild_id, key_id)
        return bool(changed)


def revoke_all_relays(conn, actor):
    with transaction(conn):
        count = conn.execute("UPDATE relays SET revoked_at = ? WHERE revoked_at IS NULL", (now(),)).rowcount
        conn.execute("DELETE FROM leases")
        audit(conn, actor, "relay revoke-all", None, str(count))
        return count


def key_bound_to(conn, key_id, slot_id):
    """Whether a relay key is active and belongs to this slot (and its guild is approved)."""
    return conn.execute(
        "SELECT 1 FROM relays r JOIN guilds g USING (guild_id) "
        "WHERE r.key_id = ? AND r.slot_id = ? AND r.revoked_at IS NULL AND g.status = 'approved'",
        (key_id, slot_id)).fetchone() is not None


def lease_of(conn, slot_id):
    return conn.execute("SELECT l.*, r.label FROM leases l JOIN relays r USING (key_id) WHERE l.slot_id = ?",
                        (slot_id,)).fetchone()


# ---- retention --------------------------------------------------------------

DAY = 86400
RETENTION = {
    "removed_guild": 7 * DAY,
    "audit": 90 * DAY,
    "queue": 1 * DAY,
    "launch_seq": 30 * DAY,
    "pairing_code": 1 * DAY,
}


def purge(conn, at=None):
    """Delete data past its retention. Returns {what: rows deleted}."""
    at = now() if at is None else at
    counts = {}
    with transaction(conn):
        counts["guilds"] = conn.execute(
            "DELETE FROM guilds WHERE status = 'removed' AND removed_at < ?",
            (at - RETENTION["removed_guild"],)).rowcount
        counts["audit"] = conn.execute("DELETE FROM audit_log WHERE at < ?", (at - RETENTION["audit"],)).rowcount
        counts["queue"] = conn.execute("DELETE FROM event_queue WHERE received_at < ?",
                                       (at - RETENTION["queue"],)).rowcount
        counts["launch_seq"] = conn.execute("DELETE FROM launch_seq WHERE updated_at < ?",
                                            (at - RETENTION["launch_seq"],)).rowcount
        counts["pairing_codes"] = conn.execute(
            "DELETE FROM pairing_codes WHERE expires_at < ?", (at - RETENTION["pairing_code"],)).rowcount
        counts["leases"] = conn.execute("DELETE FROM leases WHERE expires_at < ?", (at,)).rowcount
        if any(counts.values()):
            audit(conn, "retention", "purge", None, ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return counts


def backup(conn, path):
    """A consistent copy of the live database (SQLite's online backup)."""
    target = sqlite3.connect(path)
    try:
        conn.backup(target)
    finally:
        target.close()
