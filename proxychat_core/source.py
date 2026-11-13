"""Where the bot's game events come from.

An event source yields batches of parsed records, each tagged with the slot (one
Discord category hosting one game) it belongs to. Local mode reads game.log on this
PC; the central server will read events that relays sent in instead.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .logwatch import LogTailer
from .protocol import Record, parse_line

log = logging.getLogger("proxychat")

# The one slot in local mode, where the bot serves a single category
LOCAL_SLOT = "local"


@dataclass(frozen=True)
class SlotEvent:
    slot_id: str
    date: str | None
    record: Record


@dataclass(frozen=True)
class Batch:
    events: list[SlotEvent] = field(default_factory=list)
    # True for records replayed from before the source started (e.g. the existing
    # game.log on startup): they update state but aren't announced again
    replay: bool = False


class LocalLogSource:
    """Tails game.log on this PC. The first batch replays the current launch's log."""

    def __init__(self, log_path, poll_seconds=1.0, slot_id=LOCAL_SLOT):
        self.log_path = Path(log_path)
        self.poll_seconds = poll_seconds
        self.slot_id = slot_id
        self.tailer = LogTailer(log_path)

    def describe(self):
        return str(self.log_path)

    def poll(self, first=False):
        """One synchronous read: a Batch, or None if nothing new was parsed."""
        events = []
        for line in self.tailer.poll():
            parsed = parse_line(line)
            if parsed:
                events.append(SlotEvent(self.slot_id, *parsed))
        return Batch(events, replay=first) if events else None

    async def batches(self):
        first = True
        while True:
            try:
                batch = await asyncio.to_thread(self.poll, first)
                first = False
                if batch:
                    yield batch
            except Exception:
                log.exception("Error reading %s", self.log_path)
            await asyncio.sleep(self.poll_seconds)
