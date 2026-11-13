"""Record <-> dict conversion: what relays send and what the server accepts."""

import json

import pytest

from proxychat_core.protocol import (Comms, CountryId, FederationAssociate, FederationJoin, FederationMember,
                                     GalcomJoin, GalcomMember, Hail, HailDeclined, Home, Join, Pair, SweepBegin,
                                     SweepEnd, parse_line)
from proxychat_core.records import SPEC, RecordError, from_dict, to_dict

K = "123-456-789"
PREFIX = "[10:50:45][effect_impl.cpp:22217]: [2203.12.4] Log effect, file: events/proxychat_events.txt line: 16. "

EVERY_KIND = [
    SweepBegin(K), SweepEnd(K), CountryId(K, 9, True, "Rocks", "Yrekkt"), Pair(3, 7), Comms(K, 1, 2),
    Hail(K, 1, 2, False, "Rocks", "Tzynn"), HailDeclined(K, 1, 2), Join(K, 1, 2), GalcomMember(4),
    Home(K, 1), GalcomJoin(K, 1), FederationMember(5, 1, "Union"), FederationAssociate(5, 2, "Union"),
    FederationJoin(K, 1, 5, True, "Union"),
]


def test_every_record_type_is_covered():
    assert {type(r) for r in EVERY_KIND} == {cls for cls, _ in SPEC.values()}


@pytest.mark.parametrize("record", EVERY_KIND, ids=lambda r: type(r).__name__)
def test_round_trip_through_json(record):
    d = json.loads(json.dumps(to_dict("2200.1.1", record)))
    assert from_dict(d) == ("2200.1.1", record)


def test_parsed_log_lines_round_trip():
    for text in [f"COMMS|{K}|9|14", f"ID|{K}|9|H|Rocks|Ruler", f"FED_JOIN|{K}|3|12|A|The Union", "PAIR|1|2"]:
        date, record = parse_line(PREFIX + "PROXYCHAT|" + text)
        assert from_dict(to_dict(date, record)) == (date, record)


def test_date_may_be_missing():
    assert from_dict(to_dict(None, Pair(1, 2))) == (None, Pair(1, 2))


def good():
    return to_dict("2200.1.1", Comms(K, 1, 2))


@pytest.mark.parametrize("change", [
    lambda d: d.update(t="DROP_TABLES"),
    lambda d: d.pop("b"),
    lambda d: d.update(extra=1),
    lambda d: d.update(a=True),                 # bools aren't ids
    lambda d: d.update(a="1"),
    lambda d: d.update(a=-1),
    lambda d: d.update(a=10**9),
    lambda d: d.update(a=1.0),
    lambda d: d.update(key="123-456"),
    lambda d: d.update(key="1-2-3; DROP"),
    lambda d: d.update(key=123),
    lambda d: d.update(date="yesterday"),
    lambda d: d.update(date=2200),
    lambda d: d.update(b=1),                    # an empire with itself
], ids=range(14))
def test_rejects_anything_unexpected(change):
    d = good()
    change(d)
    with pytest.raises(RecordError):
        from_dict(d)


@pytest.mark.parametrize("bad", [None, [], "COMMS", 5, {"t": None}])
def test_rejects_non_objects(bad):
    with pytest.raises(RecordError):
        from_dict(bad)


def test_names_are_cleaned_and_bounded():
    _, r = from_dict(to_dict(None, CountryId(K, 1, True, "@everyone **x**​", "<@1>")))
    assert (r.name, r.ruler) == ("everyone x", "1")
    _, r = from_dict(to_dict(None, CountryId(K, 1, True, "y" * 300, "")))
    assert len(r.name) == 100
    with pytest.raises(RecordError):
        from_dict(to_dict(None, CountryId(K, 1, True, "y" * 5000, "")))
    with pytest.raises(RecordError):
        from_dict({**to_dict(None, CountryId(K, 1, True, "a", "")), "name": 5})


def test_federation_zero_is_rejected():
    with pytest.raises(RecordError):
        from_dict(to_dict(None, FederationMember(0, 1, "x")))


def test_flags_must_be_booleans():
    d = to_dict(None, FederationJoin(K, 1, 5, True, "U"))
    d["member"] = 1
    with pytest.raises(RecordError):
        from_dict(d)
