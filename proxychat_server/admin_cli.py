"""Operator commands for the central server. Run on the server itself:

  docker exec bot python -m proxychat_server.admin_cli <command> ...

This is the only way to approve a guild: there's deliberately no Discord command
for it, so a hijacked Discord account can't let a server in. Every command is
written to the audit log as actor "cli". The running bot picks changes up within
seconds.

  guild approve <guild_id> [--max-slots N] [--note TEXT]
  guild list
  guild freeze|unfreeze|remove <guild_id>
  slot list [--guild ID]
  relay list [--guild ID]
  relay revoke <key_id>
  relay revoke-all
  maintenance on|off
  audit [-n N]
  status
  backup <path>
  import-state <guild_id> <category_id> <state.json> [--label TEXT]
  export-state <slot_id> <state.json>
  set max-guilds <N>
  alert <text>              DM the operator through the bot (for scripts)
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from proxychat_core.state import Store

from . import db
from .config import DEFAULT_DB

ACTOR = "cli"


def when(ts):
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def table(rows, headers):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(lines)


def cmd_guild(conn, args, out):
    if args.action == "approve":
        try:
            db.approve_guild(conn, args.guild_id, ACTOR, args.max_slots, args.note or "")
        except db.CapReached as e:
            out(f"Refused: {e}. Remove a guild first, or raise the cap with `set max-guilds`.")
            return 1
        out(f"Approved {args.guild_id} (up to {args.max_slots} slot(s)). They can invite the bot now.")
    elif args.action == "list":
        rows = conn.execute("SELECT g.*, (SELECT COUNT(*) FROM slots s WHERE s.guild_id = g.guild_id) AS n "
                            "FROM guilds g ORDER BY created_at").fetchall()
        out(table([(r["guild_id"], r["status"], f"{r['n']}/{r['max_slots']}", when(r["created_at"]), r["note"])
                   for r in rows], ["guild", "status", "slots", "approved", "note"]))
        out(f"{db.active_guild_count(conn)} of {db.max_guilds(conn)} guild places used.")
    else:
        status = {"freeze": "frozen", "unfreeze": "approved", "remove": "removed"}[args.action]
        if args.action == "unfreeze":
            row = db.guild(conn, args.guild_id)
            if row is None or row["status"] != "frozen":
                out("That guild isn't frozen.")
                return 1
        if not db.set_guild_status(conn, args.guild_id, status, ACTOR):
            out("Unknown guild.")
            return 1
        extra = {"frozen": " The bot stays but does nothing there.",
                 "removed": " The bot leaves; its relays are revoked; its data is deleted after 7 days.",
                 "approved": ""}[status]
        out(f"{args.guild_id} is now {status}.{extra}")
    return 0


def cmd_slot(conn, args, out):
    rows = db.slots_of(conn, args.guild)
    out(table([(r["slot_id"], r["guild_id"], r["category_id"], r["label"], when(r["created_at"])) for r in rows],
              ["slot", "guild", "category", "label", "created"]))
    return 0


def cmd_relay(conn, args, out):
    if args.action == "list":
        rows = db.relays(conn, args.guild)
        out(table([(r["key_id"], r["guild_id"], r["slot_label"], r["label"], when(r["created_at"]),
                    when(r["last_seen"]), "revoked " + when(r["revoked_at"]) if r["revoked_at"] else "active")
                   for r in rows], ["key", "guild", "slot", "label", "paired", "last seen", "state"]))
    elif args.action == "revoke":
        if not args.key_id:
            out("Which key? See `relay list`.")
            return 1
        if not db.revoke_relay(conn, args.key_id, ACTOR):
            out("No active key by that id.")
            return 1
        out(f"Revoked {args.key_id}.")
    else:
        out(f"Revoked {db.revoke_all_relays(conn, ACTOR)} key(s). Every relay has to pair again.")
    return 0


def cmd_maintenance(conn, args, out):
    with db.transaction(conn):
        db.set_setting(conn, "maintenance", "1" if args.state == "on" else "0")
        db.audit(conn, ACTOR, f"maintenance {args.state}")
    out(f"Maintenance {args.state}." + (" The API answers 503; relays back off." if args.state == "on" else ""))
    return 0


def cmd_audit(conn, args, out):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    out(table([(when(r["at"]), r["actor"], r["guild_id"] or "-", r["action"], r["details"]) for r in reversed(rows)],
              ["when", "actor", "guild", "action", "details"]))
    return 0


def cmd_status(conn, args, out):
    beat = int(db.get_setting(conn, "bot_heartbeat", 0))
    age = db.now() - beat if beat else None
    bot = "never seen" if age is None else (f"alive ({age}s ago)" if age < 120 else f"SILENT for {age}s")
    out(f"Bot: {bot}")
    out(f"Guilds: {db.active_guild_count(conn)} of {db.max_guilds(conn)}")
    out(f"Maintenance: {'on' if db.get_setting(conn, 'maintenance', '0') == '1' else 'off'}")
    out(f"Queued events: {conn.execute('SELECT COUNT(*) FROM event_queue').fetchone()[0]}")
    for row in db.slots_of(conn):
        lease = db.lease_of(conn, row["slot_id"])
        hosted = f"hosted by {lease['label']} until {when(lease['expires_at'])}" if lease else "no relay"
        out(f"  {row['guild_id']} / {row['label']} ({row['slot_id']}): {hosted}")
    return 0


def cmd_backup(conn, args, out):
    path = Path(args.path)
    if path.exists():
        out(f"{path} already exists; not overwriting it.")
        return 1
    db.backup(conn, path)
    db.audit(conn, ACTOR, "backup", None, str(path))
    out(f"Backed up to {path}.")
    return 0


def cmd_import_state(conn, args, out):
    """Bring a local-mode state.json in as a slot, so channels and claims carry over."""
    path = Path(args.state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        store = Store()
        store.load_json(data)          # validates the shape by parsing it
        state = store.to_json()
    except (OSError, ValueError, KeyError, TypeError) as e:
        out(f"Can't read {path}: {e}")
        return 1
    try:
        slot_id = db.add_slot(conn, args.guild_id, args.category_id, args.label, ACTOR, state=state)
    except db.SlotError as e:
        out(f"Can't import: {e}.")
        return 1
    claims = sum(len(g.claims) for g in store.games.values())
    out(f"Imported as slot {slot_id} ({args.label}): {len(store.games)} game(s), {claims} claim(s), "
        f"{len(store.owned)} channel(s). Stop the local bot for good: the same token must never run twice.")
    return 0


def cmd_export_state(conn, args, out):
    """Write a slot's state as a local-mode state.json (the reverse of import-state)."""
    path = Path(args.path)
    if path.exists():
        out(f"{path} already exists; not overwriting it. Move it aside first.")
        return 1
    if conn.execute("SELECT 1 FROM slots WHERE slot_id = ?", (args.slot_id,)).fetchone() is None:
        out("No slot by that id. See `slot list`.")
        return 1
    data = db.SqliteBackend(conn, args.slot_id).read() or Store().to_json()
    store = Store(path)
    store.load_json(data)
    store.save()
    db.audit(conn, ACTOR, "export-state", None, f"{args.slot_id} -> {path}")
    out(f"Exported slot {args.slot_id} to {path}.")
    return 0


def cmd_alert(conn, args, out):
    """Send the operator a DM through the bot (used by server scripts, e.g. a failed backup)."""
    text = " ".join(args.text).strip()[:500]
    if not text:
        out("Say what happened.")
        return 1
    db.audit(conn, ACTOR, "alert", None, text)
    out("Queued: the bot sends it within a minute.")
    return 0


def cmd_set(conn, args, out):
    if args.value < 1:
        out("Must be at least 1.")
        return 1
    with db.transaction(conn):
        db.set_setting(conn, "max_guilds", args.value)
        db.audit(conn, ACTOR, "set max-guilds", None, str(args.value))
    out(f"max-guilds is now {args.value}.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="admin_cli", description="Proxy Chat server administration")
    p.add_argument("--db", default=os.environ.get("PROXYCHAT_DB", str(DEFAULT_DB)))
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("guild")
    g.add_argument("action", choices=["approve", "list", "freeze", "unfreeze", "remove"])
    g.add_argument("guild_id", type=int, nargs="?")
    g.add_argument("--max-slots", type=int, default=db.DEFAULT_MAX_SLOTS, choices=range(1, 11), metavar="1-10")
    g.add_argument("--note")
    g.set_defaults(func=cmd_guild)

    s = sub.add_parser("slot")
    s.add_argument("action", choices=["list"])
    s.add_argument("--guild", type=int)
    s.set_defaults(func=cmd_slot)

    r = sub.add_parser("relay")
    r.add_argument("action", choices=["list", "revoke", "revoke-all"])
    r.add_argument("key_id", nargs="?")
    r.add_argument("--guild", type=int)
    r.set_defaults(func=cmd_relay)

    m = sub.add_parser("maintenance")
    m.add_argument("state", choices=["on", "off"])
    m.set_defaults(func=cmd_maintenance)

    a = sub.add_parser("audit")
    a.add_argument("-n", type=int, default=30)
    a.set_defaults(func=cmd_audit)

    sub.add_parser("status").set_defaults(func=cmd_status)

    b = sub.add_parser("backup")
    b.add_argument("path")
    b.set_defaults(func=cmd_backup)

    i = sub.add_parser("import-state")
    i.add_argument("guild_id", type=int)
    i.add_argument("category_id", type=int)
    i.add_argument("state")
    i.add_argument("--label", default="Game 1")
    i.set_defaults(func=cmd_import_state)

    e = sub.add_parser("export-state")
    e.add_argument("slot_id")
    e.add_argument("path")
    e.set_defaults(func=cmd_export_state)

    al = sub.add_parser("alert")
    al.add_argument("text", nargs="+")
    al.set_defaults(func=cmd_alert)

    st = sub.add_parser("set")
    st.add_argument("name", choices=["max-guilds"])
    st.add_argument("value", type=int)
    st.set_defaults(func=cmd_set)
    return p


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    if args.command == "guild" and args.action != "list" and args.guild_id is None:
        out("Which guild? Give its id.")
        return 2
    conn = db.connect(args.db)
    try:
        return args.func(conn, args, out)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
