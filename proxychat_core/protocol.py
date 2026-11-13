"""Parsing of the PROXYCHAT lines the mod writes into game.log.

A real line looks like:
  [10:50:45][effect_impl.cpp:22217]: [2200.1.1] Log effect, file: events/proxychat_events.txt line: 16. PROXYCHAT|COMMS|123-456-789|1|2

Records (after the "PROXYCHAT|" marker):
  SWEEP_BEGIN|<key>
  ID|<key>|<id>|<H or A>|<empire name>|<ruler name>
  PAIR|<id>|<id>
  SWEEP_END|<key>
  COMMS|<key>|<id>|<id>
  HAIL|<key>|<from id>|<to id>|<H or A>|<from name>|<to name>   (hail accepted)
  HAIL_DECLINED|<key>|<from id>|<to id>
  JOIN|<key>|<from id>|<to id>        (asked to join <to>'s embassy channel)
  GALCOM|<id>                         (inside a sweep: in the Galactic Community)
  HOME|<key>|<id>                     (Voice Chat menu: back to own embassy)
  GALCOM_JOIN|<key>|<id>              (Voice Chat menu: into the Galactic Community channel)
  FED|<fid>|<id>|<federation name>        (inside a sweep: a player empire in federation <fid>)
  FED_ASSOC|<fid>|<id>|<federation name>  (inside a sweep: a player empire with association status)
  FED_JOIN|<key>|<id>|<fid>|<M or A>|<federation name>
                                      (asked to join a federation channel, as a Member or Associate)
"""

import re
from dataclasses import dataclass

MARKER = "PROXYCHAT|"
_GAME_DATE = re.compile(r"\]: \[(\d+\.\d+\.\d+)\] Log effect")


@dataclass(frozen=True)
class SweepBegin:
    key: str


@dataclass(frozen=True)
class SweepEnd:
    key: str


@dataclass(frozen=True)
class CountryId:
    key: str
    country: int
    human: bool
    name: str
    ruler: str


@dataclass(frozen=True)
class Pair:
    a: int
    b: int


@dataclass(frozen=True)
class Comms:
    key: str
    a: int
    b: int


@dataclass(frozen=True)
class Hail:
    key: str
    caller: int
    target: int
    target_human: bool
    caller_name: str
    target_name: str


@dataclass(frozen=True)
class HailDeclined:
    key: str
    caller: int
    target: int


@dataclass(frozen=True)
class Join:
    key: str
    caller: int
    target: int


@dataclass(frozen=True)
class GalcomMember:
    """Inside a sweep: this player empire is in the Galactic Community."""
    country: int


@dataclass(frozen=True)
class Home:
    """Voice Chat menu: back to our own embassy channel."""
    key: str
    country: int


@dataclass(frozen=True)
class GalcomJoin:
    """Voice Chat menu: into the Galactic Community channel."""
    key: str
    country: int


@dataclass(frozen=True)
class FederationMember:
    """Inside a sweep: this player empire is a member of federation <fed>."""
    fed: int
    country: int
    name: str


@dataclass(frozen=True)
class FederationAssociate:
    """Inside a sweep: this player empire has association status with federation <fed>."""
    fed: int
    country: int
    name: str


@dataclass(frozen=True)
class FederationJoin:
    """Subspace Channel or Voice Chat menu: into a federation's channel."""
    key: str
    country: int
    fed: int
    member: bool   # False = joining as an associate
    name: str


Record = (SweepBegin | SweepEnd | CountryId | Pair | Comms | Hail | HailDeclined | Join
          | GalcomMember | Home | GalcomJoin | FederationMember | FederationAssociate | FederationJoin)


def _id(text):
    # Unset variables render as an empty string
    text = text.strip()
    return int(text) if text.isdigit() else None


def _key(text):
    text = text.strip()
    return text if re.fullmatch(r"\d+-\d+-\d+", text) else None


def parse_line(line):
    """Return (game_date or None, Record) for a PROXYCHAT line, else None."""
    idx = line.find(MARKER)
    if idx < 0:
        return None
    m = _GAME_DATE.search(line, 0, idx)
    date = m.group(1) if m else None
    f = line[idx + len(MARKER):].rstrip("\r\n").split("|")
    kind, args = f[0], f[1:]

    record = None
    if kind in ("SWEEP_BEGIN", "SWEEP_END") and len(args) >= 1:
        key = _key(args[0])
        if key:
            record = SweepBegin(key) if kind == "SWEEP_BEGIN" else SweepEnd(key)
    elif kind == "ID" and len(args) >= 5:
        key, country = _key(args[0]), _id(args[1])
        if key and country is not None:
            # Ruler is last; anything between is the empire name, in case it holds a "|"
            name = "|".join(args[3:-1]).strip()
            record = CountryId(key, country, args[2] == "H", name, args[-1].strip())
    elif kind == "PAIR" and len(args) >= 2:
        a, b = _id(args[0]), _id(args[1])
        if a is not None and b is not None and a != b:
            record = Pair(a, b)
    elif kind == "COMMS" and len(args) >= 3:
        key, a, b = _key(args[0]), _id(args[1]), _id(args[2])
        if key and a is not None and b is not None and a != b:
            record = Comms(key, a, b)
    elif kind == "HAIL" and len(args) >= 6:
        key, a, b = _key(args[0]), _id(args[1]), _id(args[2])
        if key and a is not None and b is not None:
            record = Hail(key, a, b, args[3] == "H", args[4].strip(), "|".join(args[5:]).strip())
    elif kind in ("HAIL_DECLINED", "JOIN") and len(args) >= 3:
        key, a, b = _key(args[0]), _id(args[1]), _id(args[2])
        if key and a is not None and b is not None and a != b:
            record = (HailDeclined if kind == "HAIL_DECLINED" else Join)(key, a, b)
    elif kind == "GALCOM" and len(args) >= 1:
        country = _id(args[0])
        if country is not None:
            record = GalcomMember(country)
    elif kind in ("HOME", "GALCOM_JOIN") and len(args) >= 2:
        key, country = _key(args[0]), _id(args[1])
        if key and country is not None:
            record = (Home if kind == "HOME" else GalcomJoin)(key, country)
    elif kind in ("FED", "FED_ASSOC") and len(args) >= 3:
        fed, country = _id(args[0]), _id(args[1])
        if fed and country is not None:   # fed 0 = the federation had no id yet
            name = "|".join(args[2:]).strip()
            record = (FederationMember if kind == "FED" else FederationAssociate)(fed, country, name)
    elif kind == "FED_JOIN" and len(args) >= 5:
        key, country, fed = _key(args[0]), _id(args[1]), _id(args[2])
        if key and country is not None and fed and args[3] in ("M", "A"):
            record = FederationJoin(key, country, fed, args[3] == "M", "|".join(args[4:]).strip())

    return (date, record) if record else None
