"""Container health check for the bot: healthy while its heartbeat in the database is recent."""

import os
import sqlite3
import sys
import time

DB = os.environ.get("PROXYCHAT_DB", "/data/proxychat.db")

try:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    row = conn.execute("SELECT value FROM settings WHERE key = 'bot_heartbeat'").fetchone()
except sqlite3.Error:
    sys.exit(1)
sys.exit(0 if row and time.time() - int(row[0]) < 120 else 1)
