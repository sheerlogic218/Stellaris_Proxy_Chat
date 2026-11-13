"""Event sources and store backends: the seams the central server plugs into."""

import asyncio
import json
from pathlib import Path

from proxychat_bot.bot import ProxyChatBot
from proxychat_bot.config import Config
from proxychat_core.protocol import Comms, CountryId, SweepBegin
from proxychat_core.source import LOCAL_SLOT, Batch, LocalLogSource, SlotEvent
from proxychat_core.state import FileBackend, Store

PREFIX = "[10:50:45][effect_impl.cpp:22217]: [2203.12.4] Log effect, file: events/proxychat_events.txt line: 16. "
K = "123-456-789"


def line(text):
    return PREFIX + "PROXYCHAT|" + text + "\n"


# ---- LocalLogSource -------------------------------------------------------

def test_first_poll_is_a_replay_then_new_lines_are_live(tmp_path):
    log = tmp_path / "game.log"
    log.write_text("noise\n" + line(f"SWEEP_BEGIN|{K}") + line(f"COMMS|{K}|1|2"), encoding="utf-8")
    source = LocalLogSource(log)

    first = source.poll(first=True)
    assert first.replay
    assert first.events == [SlotEvent(LOCAL_SLOT, "2203.12.4", SweepBegin(K)),
                            SlotEvent(LOCAL_SLOT, "2203.12.4", Comms(K, 1, 2))]

    assert source.poll() is None   # nothing new
    with log.open("a", encoding="utf-8") as f:
        f.write(line(f"COMMS|{K}|2|3"))
    live = source.poll()
    assert not live.replay and [e.record for e in live.events] == [Comms(K, 2, 3)]


def test_events_are_tagged_with_the_sources_slot(tmp_path):
    log = tmp_path / "game.log"
    log.write_text(line(f"COMMS|{K}|1|2"), encoding="utf-8")
    assert LocalLogSource(log, slot_id="slot-7").poll().events[0].slot_id == "slot-7"


def test_async_batches_skip_empty_polls(tmp_path):
    log = tmp_path / "game.log"
    log.write_text("", encoding="utf-8")
    source = LocalLogSource(log, poll_seconds=0.01)

    async def first_batch():
        gen = source.batches()
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        with log.open("a", encoding="utf-8") as f:
            f.write(line(f"COMMS|{K}|1|2"))
        batch = await asyncio.wait_for(task, 2)
        await gen.aclose()
        return batch

    batch = asyncio.run(first_batch())
    # The empty first poll used up the replay, so this one is live
    assert not batch.replay and batch.events[0].record == Comms(K, 1, 2)


# ---- Store backends -------------------------------------------------------

class MemoryBackend:
    def __init__(self, data=None):
        self.data = data

    def read(self):
        return self.data

    def write(self, data):
        self.data = json.loads(json.dumps(data))   # what a database would round-trip


def test_store_round_trips_through_any_backend():
    backend = MemoryBackend()
    store = Store(backend=backend)
    store.current_key = K
    store.game(K).claims[42] = 3
    store.owned.add(99)
    store.save()

    loaded = Store.from_backend(backend)
    assert loaded.current_key == K and loaded.current.claims == {42: 3} and loaded.owned == {99}
    assert loaded.path is None


def test_file_backend_keeps_the_existing_state_json_format(tmp_path):
    path = tmp_path / "state.json"
    store = Store(path)
    store.current_key = K
    store.game(K)
    store.commands_channel = 5
    store.save()

    assert isinstance(store.backend, FileBackend) and store.path == path
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"current_key", "owned", "commands_channel", "paused", "games"}
    assert Store.load(path).commands_channel == 5


def test_store_without_backend_stays_in_memory(tmp_path):
    store = Store()
    store.save()   # no-op, no error
    assert store.backend is None and store.path is None


def test_missing_file_loads_empty(tmp_path):
    assert Store.load(tmp_path / "nope.json").games == {}


# ---- bot uses the source --------------------------------------------------

def make_bot(tmp_path, source=None):
    cfg = Config("token", 5, "Proxy Chat", 0, Path("game.log"), tmp_path / "state.json", 1.0)
    bot = ProxyChatBot(cfg, Store(cfg.state_path), source)
    announced = []

    async def announce(changes, slot=None):
        announced.extend(changes)

    bot.announce = announce
    return bot, announced


def events(*records):
    return [SlotEvent(LOCAL_SLOT, "2200.1.1", r) for r in records]


def test_bot_defaults_to_tailing_the_configured_log(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert isinstance(bot.source, LocalLogSource) and bot.source.log_path == Path("game.log")


def test_bot_accepts_another_source(tmp_path):
    source = object()
    bot, _ = make_bot(tmp_path, source)
    assert bot.source is source


def test_replayed_batch_updates_state_without_announcing(tmp_path):
    bot, announced = make_bot(tmp_path)
    asyncio.run(bot.apply_batch(Batch(events(SweepBegin(K), CountryId(K, 1, True, "Rocks", "r")), replay=True)))
    assert bot.store.current_key == K and announced == []
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["current_key"] == K
    assert bot.dirty.is_set()


def test_live_batch_is_announced(tmp_path):
    bot, announced = make_bot(tmp_path)
    asyncio.run(bot.apply_batch(Batch(events(SweepBegin(K)))))
    assert announced


def test_empty_batch_changes_nothing(tmp_path):
    bot, announced = make_bot(tmp_path)
    asyncio.run(bot.apply_batch(Batch()))
    assert not (tmp_path / "state.json").exists() and announced == [] and not bot.dirty.is_set()
