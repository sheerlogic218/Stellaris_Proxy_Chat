"""Move a Discord user into their own empire's embassy channel.

  .venv\\Scripts\\python tools\\move_home.py USER_ID [--config config.stellaris-server.toml]
  .venv\\Scripts\\python tools\\move_home.py USER_ID --watch      # keep them there until Ctrl+C

Uses the bot's token over the REST API only, so it works while the bot is running.
The user has to be in a voice channel already (Discord can't pull someone into voice).
With --watch it checks every second and only moves them when they're somewhere else.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxychat_bot.config import find_config, load_config  # noqa: E402
from proxychat_core.state import Store  # noqa: E402

API = "https://discord.com/api/v10"


class Refused(Exception):
    pass


def call(cfg, method, path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bot {cfg.token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (proxychat, 1.0)",
            "X-Audit-Log-Reason": "Proxy Chat: moved to own embassy",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code == 429:
            wait = float(json.loads(text).get("retry_after", 1.0))
            time.sleep(wait)
            return call(cfg, method, path, body)
        raise Refused(f"{e.code}: {text}") from None


def embassy(cfg, user_id):
    store = Store.load(cfg.state_path)
    game = store.current
    if game is None:
        sys.exit("No game is being tracked in " + str(cfg.state_path))
    country = game.claims.get(user_id)
    if country is None:
        sys.exit(f"User {user_id} hasn't claimed an empire in game {game.key}.")
    name = game.countries[country].name if country in game.countries else f"Empire {country}"
    channel_id = game.channels.get(country)
    if not channel_id or channel_id not in store.owned:
        sys.exit(f"{name} has no embassy channel yet.")
    return name, channel_id


def current_voice_channel(cfg, user_id):
    """The voice channel id the user is in, or None if they're not in voice."""
    try:
        state = call(cfg, "GET", f"/guilds/{cfg.guild_id}/voice-states/{user_id}")
    except Refused as e:
        if str(e).startswith("404"):
            return None
        raise
    cid = state.get("channel_id") if state else None
    return int(cid) if cid else None


def move(cfg, user_id, channel_id):
    call(cfg, "PATCH", f"/guilds/{cfg.guild_id}/members/{user_id}", {"channel_id": str(channel_id)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", type=int)
    parser.add_argument("--config", default="config.stellaris-server.toml")
    parser.add_argument("--watch", action="store_true", help="keep moving them back every second until Ctrl+C")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    cfg = load_config(find_config(args.config))
    sys.stdout.reconfigure(line_buffering=True)

    if not args.watch:
        name, channel_id = embassy(cfg, args.user_id)
        try:
            move(cfg, args.user_id, channel_id)
        except Refused as e:
            sys.exit(f"Discord refused: {e}")
        print(f"Moved {args.user_id} to {name}'s channel.")
        return

    print(f"Watching {args.user_id} every {args.interval}s (Ctrl+C to stop)")
    last_error = None
    while True:
        try:
            name, channel_id = embassy(cfg, args.user_id)   # re-read: follows claims and new channels
            where = current_voice_channel(cfg, args.user_id)
            if where is not None and where != channel_id:
                move(cfg, args.user_id, channel_id)
                print(time.strftime("%H:%M:%S"), f"moved back to {name}'s channel (was in {where})")
            last_error = None
        except Refused as e:
            if str(e) != last_error:
                print(time.strftime("%H:%M:%S"), f"Discord refused: {e}")
                last_error = str(e)
        except Exception as e:  # network blips: keep going
            if str(e) != last_error:
                print(time.strftime("%H:%M:%S"), f"error: {e}")
                last_error = str(e)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
