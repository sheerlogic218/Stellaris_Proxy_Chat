"""Game records as plain dicts, for sending between the relay and the server.

The relay parses game.log on the host's PC and sends records as JSON objects; it
never sends raw log lines. The server validates every object strictly with
from_dict before it's queued, and the bot validates again before applying it.

    {"t": "COMMS", "date": "2200.1.1", "key": "123-456-789", "a": 1, "b": 2}

Anything unexpected (unknown type, missing or extra fields, wrong types, values
out of range) is rejected with RecordError. Names are cleaned (names.clean_name).
"""

import re

from .names import MAX_NAME, clean_name
from .protocol import (Comms, CountryId, FederationAssociate, FederationJoin, FederationMember, GalcomJoin,
                       GalcomMember, Hail, HailDeclined, Home, Join, Pair, SweepBegin, SweepEnd)


class RecordError(ValueError):
    pass


MAX_ID = 1_000_000
# Longest raw name accepted before cleaning; longer means something's wrong
MAX_RAW_NAME = 400
_KEY = re.compile(r"\d{1,12}-\d{1,12}-\d{1,12}")
_DATE = re.compile(r"\d{1,6}\.\d{1,2}\.\d{1,2}")


def _key(v):
    if not isinstance(v, str) or not _KEY.fullmatch(v):
        raise RecordError("bad game key")
    return v


def _id(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_ID:
        raise RecordError("bad id")
    return v


def _fed(v):
    v = _id(v)
    if v == 0:   # 0 = the federation had no id yet; the parser drops those too
        raise RecordError("bad federation id")
    return v


def _bool(v):
    if not isinstance(v, bool):
        raise RecordError("bad flag")
    return v


def _name(v):
    if not isinstance(v, str) or len(v) > MAX_RAW_NAME:
        raise RecordError("bad name")
    return clean_name(v, "", MAX_NAME)


# type -> (class, {field: validator}); field names match the protocol dataclasses
SPEC = {
    "SWEEP_BEGIN": (SweepBegin, {"key": _key}),
    "SWEEP_END": (SweepEnd, {"key": _key}),
    "ID": (CountryId, {"key": _key, "country": _id, "human": _bool, "name": _name, "ruler": _name}),
    "PAIR": (Pair, {"a": _id, "b": _id}),
    "COMMS": (Comms, {"key": _key, "a": _id, "b": _id}),
    "HAIL": (Hail, {"key": _key, "caller": _id, "target": _id, "target_human": _bool,
                    "caller_name": _name, "target_name": _name}),
    "HAIL_DECLINED": (HailDeclined, {"key": _key, "caller": _id, "target": _id}),
    "JOIN": (Join, {"key": _key, "caller": _id, "target": _id}),
    "GALCOM": (GalcomMember, {"country": _id}),
    "HOME": (Home, {"key": _key, "country": _id}),
    "GALCOM_JOIN": (GalcomJoin, {"key": _key, "country": _id}),
    "FED": (FederationMember, {"fed": _fed, "country": _id, "name": _name}),
    "FED_ASSOC": (FederationAssociate, {"fed": _fed, "country": _id, "name": _name}),
    "FED_JOIN": (FederationJoin, {"key": _key, "country": _id, "fed": _fed, "member": _bool, "name": _name}),
}
_TYPE_OF = {cls: t for t, (cls, _) in SPEC.items()}
# Records about two different empires, which the parser also requires to differ
_DISTINCT = {"PAIR": ("a", "b"), "COMMS": ("a", "b"), "HAIL_DECLINED": ("caller", "target"),
             "JOIN": ("caller", "target")}


def to_dict(date, record):
    t = _TYPE_OF[type(record)]
    d = {"t": t, "date": date}
    for field in SPEC[t][1]:
        d[field] = getattr(record, field)
    return d


def from_dict(d):
    """(date, Record) from a dict, or RecordError."""
    if not isinstance(d, dict):
        raise RecordError("not an object")
    t = d.get("t")
    if t not in SPEC:
        raise RecordError("unknown record type")
    cls, fields = SPEC[t]
    if set(d) != {"t", "date", *fields}:
        raise RecordError("wrong fields")
    date = d["date"]
    if date is not None and (not isinstance(date, str) or not _DATE.fullmatch(date)):
        raise RecordError("bad date")
    values = {name: check(d[name]) for name, check in fields.items()}
    if t in _DISTINCT:
        a, b = _DISTINCT[t]
        if values[a] == values[b]:
            raise RecordError("same empire twice")
    return date, cls(**values)
