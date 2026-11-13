"""Proxy Chat relay for game hosts.

  python -m proxychat_relay pair <server_url> <code> [--label NAME] [--log PATH]
  python -m proxychat_relay run
  python -m proxychat_relay status
  python -m proxychat_relay forget

An admin gets the server address and a one-time code with /proxychat host in
Discord. Pair once, then `run` whenever you host a game (start_relay.bat does it).
"""

import argparse
import sys
from pathlib import Path

from proxychat_core import signing
from proxychat_core.names import clean_name

from . import __version__
from . import config as relay_config
from .client import Client, RelayError
from .relay import Relay

DEFAULT_LABEL = "Game host"


def _paths(args):
    folder = Path(args.config_dir) if args.config_dir else relay_config.default_dir()
    return folder / "relay.toml"


def _store(store):
    if store is not None:
        return store
    from .keys import KeyringStore
    return KeyringStore()


def cmd_pair(args, store, out):
    label = clean_name(args.label, "", 32)
    if not label:
        out("Give the relay a name with --label (letters and numbers).")
        return 2
    path = _paths(args)
    previous = relay_config.load(path)
    try:
        client = Client(args.server_url, allow_insecure_local=args.insecure_local)
        key = signing.new_private_key()
        reply = client.pair(args.code, signing.public_bytes(key), label)
    except RelayError as e:
        out(f"Pairing failed: {e}")
        return 1
    key_id = reply.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        out("Pairing failed: the server sent something unexpected.")
        return 1
    store.put(key_id, key)
    cfg = relay_config.RelayConfig(
        server_url=client.base, key_id=key_id, label=label, slot=str(reply.get("slot", "")),
        log_path=Path(args.log) if args.log else relay_config.DEFAULT_LOG,
    )
    relay_config.save(cfg, path)
    if previous and previous.key_id != key_id:
        store.delete(previous.key_id)
        out("Replaced this PC's earlier pairing. Ask an admin to revoke the old one with /proxychat relay revoke.")
    out(f"Paired as {label!r} for {cfg.slot or 'the slot'}. Start the relay whenever you host: "
        "python -m proxychat_relay run")
    if not cfg.log_path.exists():
        out(f"Note: {cfg.log_path} doesn't exist yet. It appears once Stellaris has run; "
            "if your game logs elsewhere, pair again with --log.")
    return 0


def _load(args, store, out):
    cfg = relay_config.load(_paths(args))
    if cfg is None:
        out("This PC isn't paired yet. Ask an admin for a code with /proxychat host, then run "
            "python -m proxychat_relay pair <server> <code> --label <your name>")
        return None, None
    key = store.get(cfg.key_id)
    if key is None:
        out("This PC's relay key is missing from Windows Credential Manager. Pair again with a new code.")
        return cfg, None
    return cfg, key


def cmd_run(args, store, out):
    cfg, key = _load(args, store, out)
    if key is None:
        return 1
    try:
        client = Client(cfg.server_url, cfg.key_id, key, allow_insecure_local=args.insecure_local)
    except RelayError as e:
        out(str(e))
        return 1
    return Relay(cfg, client, say=out).run()


def cmd_status(args, store, out):
    cfg = relay_config.load(_paths(args))
    out(f"Proxy Chat relay {__version__}")
    if cfg is None:
        out("Not paired.")
        return 0
    out(f"Server:  {cfg.server_url}")
    out(f"Slot:    {cfg.slot or '?'}")
    out(f"Name:    {cfg.label}")
    out(f"Log:     {cfg.log_path}{'' if cfg.log_path.exists() else ' (not there yet)'}")
    out(f"Key:     {'stored in Credential Manager' if store.get(cfg.key_id) else 'MISSING: pair again'}")
    return 0


def cmd_forget(args, store, out):
    path = _paths(args)
    cfg = relay_config.load(path)
    if cfg is None:
        out("Nothing to forget: not paired.")
        return 0
    store.delete(cfg.key_id)
    path.unlink()
    out("Removed this PC's pairing and key. Ask an admin to revoke it in Discord too: /proxychat relay revoke")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="proxychat_relay", description="Proxy Chat relay for game hosts")
    p.add_argument("--config-dir", help=argparse.SUPPRESS)
    p.add_argument("--insecure-local", action="store_true", help=argparse.SUPPRESS)   # http://localhost, for tests
    sub = p.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair", help="Connect this PC to a slot, with a code from /proxychat host")
    pair.add_argument("server_url")
    pair.add_argument("code")
    pair.add_argument("--label", default=DEFAULT_LABEL, help="Your name, as admins will see it")
    pair.add_argument("--log", help="game.log, if Stellaris logs somewhere unusual")
    pair.set_defaults(func=cmd_pair)
    sub.add_parser("run", help="Send game events while you host").set_defaults(func=cmd_run)
    sub.add_parser("status", help="Show this PC's pairing").set_defaults(func=cmd_status)
    sub.add_parser("forget", help="Remove this PC's pairing and key").set_defaults(func=cmd_forget)
    return p


def main(argv=None, store=None, out=print):
    args = build_parser().parse_args(argv)
    return args.func(args, _store(store), out)


if __name__ == "__main__":
    sys.exit(main())
