"""The relay loop: read game.log, keep the mod's records, send them to the server.

Records are numbered within each game launch. A launch is identified by a hash of
the start of game.log, which Stellaris rewrites on every launch. So when the relay
restarts mid-game it reads the log from the top again with the same numbers, and
the server skips what it already has. Only parsed records are sent, never log lines.

Only one relay may feed a slot at a time (the lease). If another host holds it,
this relay keeps reading but holds its records and asks again every minute.
"""

import hashlib
import logging
import random
import time
from collections import deque

from proxychat_core.logwatch import LogTailer
from proxychat_core.protocol import parse_line
from proxychat_core.records import to_dict

from . import __version__
from .client import Busy, RelayError, Revoked, Unreachable, UpdateNeeded

log = logging.getLogger("proxychat.relay")

LAUNCH_HEAD = 256          # bytes of game.log that identify a launch
BATCH = 200                # records per request (the server's limit)
MAX_PENDING = 5000         # records held while the server can't be reached
HEARTBEAT_SECONDS = 30
DENIED_RETRY_SECONDS = 60
MAX_BACKOFF = 60


def version_tuple(text):
    try:
        return tuple(int(p) for p in str(text).split("."))
    except ValueError:
        return (0,)


class Relay:
    def __init__(self, cfg, client, clock=time.monotonic, say=print):
        self.cfg = cfg
        self.client = client
        self.clock = clock
        self.say = say
        self.tailer = LogTailer(cfg.log_path)
        self.launch = None
        self.seq = 0                 # number of the next record in this launch
        self.pending = deque()       # (launch, seq, record dict), oldest first
        self.lease = None            # "held", "denied" or None (not asked yet)
        self.next_try = 0.0
        self.backoff = 1.0
        self.last_contact = -HEARTBEAT_SECONDS
        self.dropped = 0
        self._last_problem = None

    # ---- reading ----------------------------------------------------------

    def _head(self):
        try:
            with open(self.cfg.log_path, "rb") as f:
                head = f.read(LAUNCH_HEAD)
        except OSError:
            return None
        return head if len(head) == LAUNCH_HEAD else None

    def read_log(self):
        """Read new lines and queue their records. Returns how many records were added."""
        head = self._head()
        if head is None:
            return 0   # no log yet, or the game has only just started writing it
        launch = "L" + hashlib.sha256(head).hexdigest()[:32]
        if launch != self.launch:
            if self.launch is not None:
                self.say("Stellaris was restarted: following the new game.log.")
                self.tailer = LogTailer(self.cfg.log_path)   # read the new launch from the top
            self.launch, self.seq = launch, 0
        added = 0
        for line in self.tailer.poll():
            parsed = parse_line(line)
            if parsed is None:
                continue
            self.pending.append((launch, self.seq, to_dict(*parsed)))
            self.seq += 1
            added += 1
        while len(self.pending) > MAX_PENDING:
            self.pending.popleft()
            self.dropped += 1
        if self.dropped and added:
            log.warning("Dropped %d old record(s) while the server was unreachable", self.dropped)
        return added

    # ---- sending ----------------------------------------------------------

    def _batch(self):
        """The oldest run of records from one launch, numbered consecutively."""
        launch, first, _ = self.pending[0]
        records = []
        for l, seq, record in self.pending:
            if l != launch or seq != first + len(records) or len(records) == BATCH:
                break
            records.append(record)
        return launch, first, records

    def send(self):
        """Send what's pending (or a heartbeat). Raises Revoked / UpdateNeeded, which end the relay."""
        now = self.clock()
        if now < self.next_try:
            return
        try:
            if self.pending:
                launch, first, records = self._batch()
                reply = self.client.events(launch, first, records)
                if reply.get("lease") == "held":
                    acked = reply.get("acked")
                    while self.pending and self.pending[0][0] == launch and isinstance(acked, int) \
                            and self.pending[0][1] <= acked:
                        self.pending.popleft()
            elif now - self.last_contact >= HEARTBEAT_SECONDS:
                reply = self.client.heartbeat()
            else:
                return
        except (Revoked, UpdateNeeded):
            raise
        except Busy as e:
            self._problem(str(e))
            self._wait(e.retry_after)
            return
        except (Unreachable, RelayError) as e:
            self._problem(str(e))
            self._wait()
            return
        self.last_contact = now
        self.backoff = 1.0
        self._problem(None)
        self._check_version(reply.get("min_relay_version"))
        self._note_lease(reply.get("lease"))

    def _wait(self, seconds=None):
        delay = seconds if seconds is not None else self.backoff * (1 + random.random() * 0.3)
        self.backoff = min(MAX_BACKOFF, self.backoff * 2)
        self.next_try = self.clock() + delay

    def _problem(self, text):
        if text != self._last_problem:
            if text:
                self.say(f"{text} Retrying...")
            elif self._last_problem:
                self.say("Connected to the server again.")
            self._last_problem = text

    def _check_version(self, minimum):
        if minimum and version_tuple(__version__) < version_tuple(minimum):
            raise UpdateNeeded(f"This relay ({__version__}) is too old: the server needs {minimum} or newer. "
                               "Please update it.")

    def _note_lease(self, lease):
        if lease is None or lease == self.lease:
            if lease == "denied":
                self.next_try = self.clock() + DENIED_RETRY_SECONDS
            return
        self.lease = lease
        if lease == "held":
            self.say(f"Hosting {self.cfg.slot or 'the slot'}: game events are going to Discord.")
        else:
            self.say("Another host's relay is feeding this slot right now. Waiting, and checking every minute. "
                     "(An admin can free it with /proxychat relay release.)")
            self.next_try = self.clock() + DENIED_RETRY_SECONDS

    # ---- running ----------------------------------------------------------

    def step(self):
        self.read_log()
        self.send()

    def run(self, should_stop=lambda: False):
        self.say(f"Relay {__version__} for {self.cfg.label!r}: watching {self.cfg.log_path}. Ctrl+C to stop.")
        try:
            while not should_stop():
                try:
                    self.step()
                except (Revoked, UpdateNeeded) as e:
                    self.say(str(e))
                    return 1
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    def stop(self):
        """Give the slot back so another host can take over straight away."""
        if self.lease != "held":
            return
        try:
            self.client.release()
            self.say("Stopped, and freed the slot for other hosts.")
        except RelayError:
            self.say("Stopped. (Couldn't tell the server; the slot frees itself within 90 seconds.)")
        self.lease = None
