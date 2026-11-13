"""Entry point.

  python -m proxychat_bot                 run the Discord bot (reads config.toml)
  python -m proxychat_bot --dry-run       just watch game.log and print what the bot would see
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from proxychat_core.source import LocalLogSource
from proxychat_core.state import Processor, Store

from .config import TOKEN_ENV, find_config, load_config


def dry_run(cfg):
    store = Store()  # in memory: dry runs never touch the bot's state.json
    processor = Processor(store)
    source = LocalLogSource(cfg.log_path, cfg.poll_seconds)
    print(f"Watching {source.describe()} (Ctrl+C to stop)")
    while True:
        batch = source.poll()
        for event in batch.events if batch else []:
            date, record = event.date, event.record
            print(f"[{date or '?'}] {record}")
            for change in processor.feed(date, record):
                print(f"    -> {change}")
                game = store.current
                if game:
                    empires = {c.id: c.name for c in game.countries.values()}
                    print(f"       empires={empires} pairs={sorted(sorted(p) for p in game.pairs)}")
        time.sleep(cfg.poll_seconds)


def main():
    parser = argparse.ArgumentParser(prog="proxychat_bot")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", help="override game.log path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.stdout.reconfigure(line_buffering=True)
    config_path = find_config(args.config)
    cfg = load_config(config_path)
    if args.log:
        cfg.log_path = Path(args.log)

    if args.dry_run:
        try:
            dry_run(cfg)
        except KeyboardInterrupt:
            pass
        return

    problems = []
    if not config_path.exists():
        problems.append(f"config file not found: {config_path}")
    if not cfg.token:
        problems.append(f"no token: set {TOKEN_ENV} or discord.token")
    if not cfg.guild_id:
        problems.append(f"discord.guild_id is not set in {config_path}")
    if problems:
        sys.exit("Can't start:\n  " + "\n  ".join(problems))

    from .bot import ProxyChatBot

    store = Store.load(cfg.state_path)
    ProxyChatBot(cfg, store).run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
