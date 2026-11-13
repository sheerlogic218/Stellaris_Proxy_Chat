"""Settings for the central server bot, from server.toml. The token comes from a secret file or the environment."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

TOKEN_ENV = "PROXYCHAT_DISCORD_TOKEN"
# Values in server.example.toml that the operator must fill in start with this.
# Until they do, the setting counts as unset (see docs/DEPLOYMENT_PLACEHOLDERS.md).
PLACEHOLDER = "CHANGE_ME"
DEFAULT_CONFIG = Path("/config/server.toml")
DEFAULT_DB = Path("/data/proxychat.db")
DEFAULT_TOKEN_FILE = Path("/run/secrets/discord_token")


def is_placeholder(value):
    return isinstance(value, str) and value.startswith(PLACEHOLDER)


@dataclass
class ServerConfig:
    db_path: Path
    token: str
    # Gets DMs about join attempts, refusals and failures. 0 = no alerts
    owner_user_id: int = 0
    poll_seconds: float = 0.5
    # Where relays reach the API, e.g. https://<name>.<tailnet>.ts.net. "" = not set yet
    public_url: str = ""
    # Settings still holding a placeholder, for a warning at startup
    unset: tuple[str, ...] = ()


@dataclass
class ApiConfig:
    db_path: Path
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    # /healthz, only ever on localhost inside the container
    health_port: int = 8081
    # Relays older than this are told to update and refused at pairing
    min_relay_version: str = "0.1.0"


def _windows_user_token():
    """Trials on Windows: the same user variable local mode reads."""
    if os.name != "nt":
        return ""
    from proxychat_bot.config import _token_from_env
    return (_token_from_env() or "").strip()


def read_token(token_file):
    """The environment variable, else the token file, else (Windows only) the user variable."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = Path(token_file)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return _windows_user_token()


def _read(path):
    path = Path(path)
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_server_config(path):
    s = _read(path).get("server", {})
    unset = tuple(k for k, v in s.items() if is_placeholder(v))
    owner = s.get("owner_user_id", 0)
    public_url = s.get("public_url", "")
    return ServerConfig(
        db_path=Path(s.get("db_path", DEFAULT_DB)),
        token=read_token(s.get("token_file", DEFAULT_TOKEN_FILE)),
        owner_user_id=0 if is_placeholder(owner) else int(owner),
        poll_seconds=float(s.get("poll_seconds", 0.5)),
        public_url="" if is_placeholder(public_url) else str(public_url).rstrip("/"),
        unset=unset,
    )


def load_api_config(path):
    raw = _read(path)
    a = raw.get("api", {})
    return ApiConfig(
        db_path=Path(raw.get("server", {}).get("db_path", DEFAULT_DB)),
        listen_host=str(a.get("listen_host", "0.0.0.0")),
        listen_port=int(a.get("listen_port", 8080)),
        health_port=int(a.get("health_port", 8081)),
        min_relay_version=str(a.get("min_relay_version", "0.1.0")),
    )
