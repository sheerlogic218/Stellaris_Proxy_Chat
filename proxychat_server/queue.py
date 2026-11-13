"""Event source for the server bot: records relays sent in, from the event_queue table.

The API writes one row per record. The bot doesn't trust those rows: each one is
validated again (records.from_dict), and its relay key must still be active and
bound to the row's slot in an approved guild. Anything else is dropped.

Rows are deleted once the batch they were in has been applied (or failed to
apply, so a bad batch can't block the queue). If the bot dies in between, the
batch is applied again on restart: delivery is at least once, which the game
state tolerates (sweeps, contacts and names are idempotent; a repeated join
request just moves someone where they already are).
"""

import asyncio
import json
import logging

from proxychat_core.records import RecordError, from_dict
from proxychat_core.source import Batch, SlotEvent

from . import db

log = logging.getLogger("proxychat")

MAX_PAYLOAD = 4096


class QueueSource:
    def __init__(self, conn, poll_seconds=0.5, batch_size=500):
        self.conn = conn
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size

    def describe(self):
        return "the relay event queue"

    def poll(self):
        """(Batch or None, row ids to delete, rows dropped as invalid)."""
        rows = self.conn.execute("SELECT id, slot_id, key_id, payload FROM event_queue ORDER BY id LIMIT ?",
                                 (self.batch_size,)).fetchall()
        events, ids, dropped = [], [], 0
        bound = {}
        for row in rows:
            ids.append(row["id"])
            which = (row["key_id"], row["slot_id"])
            if which not in bound:
                bound[which] = db.key_bound_to(self.conn, *which)
            if not bound[which]:
                dropped += 1
                continue
            try:
                if len(row["payload"]) > MAX_PAYLOAD:
                    raise RecordError("too long")
                date, record = from_dict(json.loads(row["payload"]))
            except (RecordError, ValueError, TypeError):
                dropped += 1
                continue
            events.append(SlotEvent(row["slot_id"], date, record))
        if dropped:
            log.warning("Dropped %d queued event(s): invalid, or from a revoked or rebound relay", dropped)
            db.audit(self.conn, "bot", "queue drop", None, str(dropped))
        return (Batch(events) if events else None), ids, dropped

    def ack(self, ids):
        if ids:
            marks = ",".join("?" * len(ids))
            self.conn.execute(f"DELETE FROM event_queue WHERE id IN ({marks})", ids)

    async def batches(self):
        while True:
            ids = []
            try:
                batch, ids, _ = self.poll()
                if batch:
                    yield batch   # resumes once the bot has applied it
                self.ack(ids)
            except Exception:
                log.exception("Error reading the event queue")
                if ids:
                    try:
                        self.ack(ids)   # don't let one bad batch block the queue
                    except Exception:
                        log.exception("Could not clear the failed batch")
            if not ids:
                await asyncio.sleep(self.poll_seconds)
