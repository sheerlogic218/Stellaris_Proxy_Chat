"""The relay's settings: %APPDATA%\\ProxyChatRelay\\relay.toml. Nothing secret is in it.

The private key is kept separately, in Windows Credential Manager (keys.py).
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOG = Path.home() / "Documents" / "Paradox Interactive" / "Stellaris" / "logs" / "game.log"


def default_dir():
    base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / "ProxyChatRelay"


@dataclass
class RelayConfig:
    server_url: str
    key_id: str
    label: str
    slot: str = ""
    log_path: Path = DEFAULT_LOG
    poll_seconds: float = 1.0


def _quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(cfg, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Proxy Chat relay. Made by `python -m proxychat_relay pair`; nothing secret in here.",
        "[relay]",
        f"server_url = {_quote(cfg.server_url)}",
        f"key_id = {_quote(cfg.key_id)}",
        f"label = {_quote(cfg.label)}",
        f"slot = {_quote(cfg.slot)}",
        f"log_path = {_quote(cfg.log_path)}",
        f"poll_seconds = {float(cfg.poll_seconds)}",
    ]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path):
    """The config, or None if the relay hasn't been paired yet."""
    path = Path(path)
    if not path.exists():
        return None
    r = tomllib.loads(path.read_text(encoding="utf-8")).get("relay", {})
    return RelayConfig(
        server_url=r["server_url"],
        key_id=r["key_id"],
        label=r.get("label", ""),
        slot=r.get("slot", ""),
        log_path=Path(r.get("log_path") or DEFAULT_LOG),
        poll_seconds=float(r.get("poll_seconds", 1.0)),
    )
