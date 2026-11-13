"""Settings for the central server bot, from server.toml. The token comes from a secret file or the environment."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

TOKEN_ENV = "PROXYCHAT_DISCORD_TOKEN"
DEFAULT_CONFIG = Path("/config/server.toml")
DEFAULT_DB = Path("/data/proxychat.db")
DEFAULT_TOKEN_FILE = Path("/run/secrets/discord_token")


@dataclass
class ServerConfig:
    db_path: Path
    token: str
    # Gets DMs about join attempts, refusals and failures. 0 = no alerts
    owner_user_id: int = 0
    poll_seconds: float = 0.5


def read_token(token_file):
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = Path(token_file)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def load_server_config(path):
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    s = raw.get("server", {})
    return ServerConfig(
        db_path=Path(s.get("db_path", DEFAULT_DB)),
        token=read_token(s.get("token_file", DEFAULT_TOKEN_FILE)),
        owner_user_id=int(s.get("owner_user_id", 0)),
        poll_seconds=float(s.get("poll_seconds", 0.5)),
    )
