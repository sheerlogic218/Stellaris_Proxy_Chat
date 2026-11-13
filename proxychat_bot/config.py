"""Settings from config.toml, with the bot token optionally from the environment."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOG = Path.home() / "Documents" / "Paradox Interactive" / "Stellaris" / "logs" / "game.log"
PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_ENV = "PROXYCHAT_DISCORD_TOKEN"


def find_config(name):
    """The given path if it exists, else the same name in the project folder."""
    path = Path(name)
    if path.exists() or path.is_absolute():
        return path
    return PROJECT_DIR / path


def _token_from_env():
    token = os.environ.get(TOKEN_ENV)
    if token or os.name != "nt":
        return token
    # A user variable set after this terminal (or VS Code) started isn't in our
    # environment yet, so read it from the registry directly
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, TOKEN_ENV)[0]
    except OSError:
        return None


@dataclass
class Config:
    token: str
    guild_id: int
    category: str
    announce_channel_id: int
    log_path: Path
    state_path: Path
    poll_seconds: float
    # An existing category to put the channels in, by id (overrides category by name)
    category_id: int = 0
    # Text channel the bot keeps in its category for commands. "" = none
    commands_channel: str = "bot-commands"
    # Roles that may use the /proxychat admin commands, besides Manage Server
    admin_role_ids: tuple[int, ...] = ()


def load_config(path):
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    discord_cfg = raw.get("discord", {})
    game_cfg = raw.get("game", {})
    base = path.parent

    def resolve(value, default):
        p = Path(value) if value else default
        return p if p.is_absolute() else base / p

    return Config(
        token=_token_from_env() or discord_cfg.get("token", ""),
        guild_id=int(discord_cfg.get("guild_id", 0)),
        category=discord_cfg.get("category", "Proxy Chat"),
        announce_channel_id=int(discord_cfg.get("announce_channel_id", 0)),
        log_path=resolve(game_cfg.get("log_path"), DEFAULT_LOG),
        state_path=resolve(game_cfg.get("state_path"), Path("state.json")),
        poll_seconds=float(game_cfg.get("poll_seconds", 1.0)),
        category_id=int(discord_cfg.get("category_id", 0)),
        commands_channel=discord_cfg.get("commands_channel", "bot-commands"),
        admin_role_ids=tuple(int(r) for r in discord_cfg.get("admin_role_ids", [])),
    )
