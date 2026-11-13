"""Persistent game state and the processor that applies parsed log records to it.

Everything is keyed by the mod's game key, so loading a different campaign
switches to that campaign's countries, contacts and claims automatically.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .protocol import (Comms, CountryId, FederationAssociate, FederationJoin, FederationMember, GalcomJoin,
                       GalcomMember, Hail, HailDeclined, Home, Join, Pair, SweepBegin, SweepEnd)

# Key in Game.channels for the Galactic Community channel (country ids start at 1)
GALCOM_CHANNEL = 0


@dataclass
class Country:
    id: int
    name: str
    ruler: str
    human: bool
    last_date: str | None = None


@dataclass
class Federation:
    id: int                 # the mod's proxychat_fid, stable through renames
    name: str
    members: set[int] = field(default_factory=set)      # player empires in it
    associates: set[int] = field(default_factory=set)   # player empires with association status

    def to_json(self):
        return {"id": self.id, "name": self.name, "members": sorted(self.members),
                "associates": sorted(self.associates)}

    @classmethod
    def from_json(cls, d):
        return cls(d["id"], d.get("name", ""), set(d.get("members", [])), set(d.get("associates", [])))


@dataclass
class Game:
    key: str
    countries: dict[int, Country] = field(default_factory=dict)
    pairs: set[frozenset[int]] = field(default_factory=set)
    claims: dict[int, int] = field(default_factory=dict)      # discord user id -> country id
    channels: dict[int, int] = field(default_factory=dict)    # country id -> discord channel id
    galcom: set[int] = field(default_factory=set)              # player empires in the Galactic Community
    federations: dict[int, Federation] = field(default_factory=dict)   # federation id -> Federation
    fed_channels: dict[int, int] = field(default_factory=dict)        # federation id -> discord channel id
    last_date: str | None = None

    def contacts(self, country):
        return {other for p in self.pairs if country in p for other in p if other != country}

    def claimers(self, country):
        return {user for user, c in self.claims.items() if c == country}

    def to_json(self):
        return {
            "key": self.key,
            "countries": [asdict(c) for c in self.countries.values()],
            "pairs": sorted(sorted(p) for p in self.pairs),
            "claims": {str(u): c for u, c in self.claims.items()},
            "channels": {str(c): ch for c, ch in self.channels.items()},
            "galcom": sorted(self.galcom),
            "federations": [f.to_json() for f in self.federations.values()],
            "fed_channels": {str(f): ch for f, ch in self.fed_channels.items()},
            "last_date": self.last_date,
        }

    @classmethod
    def from_json(cls, d):
        return cls(
            key=d["key"],
            countries={c["id"]: Country(**c) for c in d.get("countries", [])},
            pairs={frozenset(p) for p in d.get("pairs", [])},
            claims={int(u): c for u, c in d.get("claims", {}).items()},
            channels={int(c): ch for c, ch in d.get("channels", {}).items()},
            galcom=set(d.get("galcom", [])),
            federations={f["id"]: Federation.from_json(f) for f in d.get("federations", [])},
            fed_channels={int(f): ch for f, ch in d.get("fed_channels", {}).items()},
            last_date=d.get("last_date"),
        )


class FileBackend:
    """Keeps a Store as a JSON file (local mode). Writes go through a temp file."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class Store:
    """All known games plus which one is currently being played.

    Where it's kept is up to the backend: anything with read() -> dict | None and
    write(dict). Without one (and without a path) the store lives in memory only.
    """

    def __init__(self, path=None, backend=None):
        self.backend = backend or (FileBackend(path) if path else None)
        self.games: dict[str, Game] = {}
        self.current_key: str | None = None
        # Every Discord channel the bot created and hasn't deleted yet. The bot only
        # ever deletes, renames or reorders these, so it can share a category with
        # the server's own channels.
        self.owned: set[int] = set()
        # The bot's text channel for commands: survives clean-ups
        self.commands_channel: int = 0

    @property
    def current(self):
        return self.games.get(self.current_key) if self.current_key else None

    def game(self, key):
        if key not in self.games:
            self.games[key] = Game(key)
        return self.games[key]

    @property
    def path(self):
        """The JSON file, for a file-backed store (else None)."""
        return getattr(self.backend, "path", None)

    @classmethod
    def load(cls, path):
        return cls.from_backend(FileBackend(path))

    @classmethod
    def from_backend(cls, backend):
        store = cls(backend=backend)
        d = backend.read()
        if d:
            store.load_json(d)
        return store

    def load_json(self, d):
        self.games = {g["key"]: Game.from_json(g) for g in d.get("games", [])}
        self.current_key = d.get("current_key")
        self.commands_channel = d.get("commands_channel", 0)
        if "owned" in d:
            self.owned = set(d["owned"])
        else:
            # Older state files: the embassy channels were the bot's
            self.owned = {ch for g in self.games.values() for ch in g.channels.values()}

    def to_json(self):
        return {
            "current_key": self.current_key,
            "owned": sorted(self.owned),
            "commands_channel": self.commands_channel,
            "games": [g.to_json() for g in self.games.values()],
        }

    def save(self):
        if self.backend:
            self.backend.write(self.to_json())


@dataclass(frozen=True)
class GameSwitched:
    key: str


@dataclass(frozen=True)
class PairsChanged:
    key: str
    added: frozenset[frozenset[int]]
    removed: frozenset[frozenset[int]]


@dataclass(frozen=True)
class CountriesChanged:
    key: str


@dataclass(frozen=True)
class Hailed:
    hail: Hail


@dataclass(frozen=True)
class GalcomChanged:
    key: str


@dataclass(frozen=True)
class FederationsChanged:
    key: str


@dataclass(frozen=True)
class Requested:
    """A one-off player request from an in-game menu."""
    record: Join | HailDeclined | Home | GalcomJoin | FederationJoin


class Processor:
    """Applies records to a Store and reports what changed."""

    def __init__(self, store):
        self.store = store
        self._sweep_key = None
        self._sweep_pairs = None
        self._sweep_galcom = None
        self._sweep_feds = None

    def _switch(self, key, changes):
        if self.store.current_key != key:
            self.store.current_key = key
            self.store.game(key)
            changes.append(GameSwitched(key))

    def feed(self, date, record):
        changes = []

        if isinstance(record, SweepBegin):
            self._switch(record.key, changes)
            self._sweep_key, self._sweep_pairs, self._sweep_galcom = record.key, set(), set()
            self._sweep_feds = {}

        elif isinstance(record, CountryId):
            self._switch(record.key, changes)
            game = self.store.game(record.key)
            old = game.countries.get(record.country)
            new = Country(record.country, record.name, record.ruler, record.human, date)
            if old is None or (old.name, old.ruler, old.human) != (new.name, new.ruler, new.human):
                changes.append(CountriesChanged(record.key))
            game.countries[record.country] = new

        elif isinstance(record, Pair):
            if self._sweep_pairs is not None:
                self._sweep_pairs.add(frozenset((record.a, record.b)))

        elif isinstance(record, GalcomMember):
            if self._sweep_galcom is not None:
                self._sweep_galcom.add(record.country)

        elif isinstance(record, (FederationMember, FederationAssociate)):
            if self._sweep_feds is not None:
                fed = self._sweep_feds.setdefault(record.fed, Federation(record.fed, record.name))
                fed.name = record.name or fed.name
                if isinstance(record, FederationMember):
                    fed.members.add(record.country)
                else:
                    fed.associates.add(record.country)

        elif isinstance(record, SweepEnd):
            if self._sweep_key == record.key and self._sweep_pairs is not None:
                game = self.store.game(record.key)
                added = frozenset(self._sweep_pairs - game.pairs)
                removed = frozenset(game.pairs - self._sweep_pairs)
                game.pairs = set(self._sweep_pairs)
                if added or removed:
                    changes.append(PairsChanged(record.key, added, removed))
                if game.galcom != self._sweep_galcom:
                    game.galcom = set(self._sweep_galcom)
                    changes.append(GalcomChanged(record.key))
                if game.federations != self._sweep_feds:
                    game.federations = self._sweep_feds
                    changes.append(FederationsChanged(record.key))
            self._sweep_key, self._sweep_pairs, self._sweep_galcom = None, None, None
            self._sweep_feds = None

        elif isinstance(record, Comms):
            self._switch(record.key, changes)
            game = self.store.game(record.key)
            pair = frozenset((record.a, record.b))
            if pair not in game.pairs:
                game.pairs.add(pair)
                changes.append(PairsChanged(record.key, frozenset([pair]), frozenset()))
            if self._sweep_pairs is not None:
                self._sweep_pairs.add(pair)

        elif isinstance(record, Hail):
            self._switch(record.key, changes)
            game = self.store.game(record.key)
            # Calls carry names, so empires are known before the first monthly sweep
            known = [(record.caller, record.caller_name, True), (record.target, record.target_name, record.target_human)]
            for cid, name, human in known:
                if cid not in game.countries and name:
                    game.countries[cid] = Country(cid, name, "", human, date)
                    changes.append(CountriesChanged(record.key))
            changes.append(Hailed(record))

        elif isinstance(record, (Join, HailDeclined, Home)):
            self._switch(record.key, changes)
            changes.append(Requested(record))

        elif isinstance(record, GalcomJoin):
            self._switch(record.key, changes)
            game = self.store.game(record.key)
            # The menu only offers this to members, so it's fresher than the last sweep
            if record.country not in game.galcom:
                game.galcom.add(record.country)
                changes.append(GalcomChanged(record.key))
            changes.append(Requested(record))

        elif isinstance(record, FederationJoin):
            self._switch(record.key, changes)
            game = self.store.game(record.key)
            # The menus only offer this to members and associates, so it's fresher than the last sweep
            if _note_federation(game, record):
                changes.append(FederationsChanged(record.key))
            changes.append(Requested(record))

        if date:
            game = self.store.current
            if game:
                game.last_date = date
        return changes


def _note_federation(game, record):
    """Record what a FED_JOIN request proves about the caller. Returns True if anything changed."""
    before = {f: Federation(x.id, x.name, set(x.members), set(x.associates)) for f, x in game.federations.items()}
    fed = game.federations.setdefault(record.fed, Federation(record.fed, record.name))
    fed.name = record.name or fed.name
    if record.member:
        # An empire is in one federation at most
        for other in game.federations.values():
            other.members.discard(record.country)
        fed.members.add(record.country)
        fed.associates.discard(record.country)
    else:
        fed.associates.add(record.country)
    return game.federations != before
