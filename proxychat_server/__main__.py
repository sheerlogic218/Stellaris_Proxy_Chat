"""Run the central server bot.

  python -m proxychat_server [--config /config/server.toml]

Guilds are approved with `python -m proxychat_server.admin_cli guild approve <id>`.
"""

import argparse
import logging
import os
import sys

from . import db
from .config import DEFAULT_CONFIG, TOKEN_ENV, load_server_config


def main():
    parser = argparse.ArgumentParser(prog="proxychat_server")
    parser.add_argument("--config", default=os.environ.get("PROXYCHAT_SERVER_CONFIG", str(DEFAULT_CONFIG)))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.stdout.reconfigure(line_buffering=True)
    cfg = load_server_config(args.config)
    if not cfg.token:
        sys.exit(f"Can't start: no token. Put it in the token_file (default /run/secrets/discord_token) "
                 f"or set {TOKEN_ENV}.")

    from .serverbot import ServerBot

    conn = db.connect(cfg.db_path)
    ServerBot(cfg, conn).run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
