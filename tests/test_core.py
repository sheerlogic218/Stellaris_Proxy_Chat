from proxychat_bot.embassy import desired_embassies
from proxychat_bot.logwatch import LogTailer
from proxychat_bot.protocol import Comms, CountryId, Pair, SweepBegin, SweepEnd, parse_line
from proxychat_bot.state import Game, GameSwitched, PairsChanged, Processor, Store

PREFIX = "[10:50:45][effect_impl.cpp:22217]: [2203.12.4] Log effect, file: events/proxychat_events.txt line: 16. "
K = "123-456-789"


def rec(text):
    return parse_line(PREFIX + "PROXYCHAT|" + text)


def feed_all(proc, *texts):
    changes = []
    for t in texts:
        changes += proc.feed(*rec(t))
    return changes


# ---- protocol -------------------------------------------------------------

def test_parse_real_line_shapes():
    assert rec(f"COMMS|{K}|9|14") == ("2203.12.4", Comms(K, 9, 14))
    assert rec(f"ID|{K}|9|H|Rocks|Yrekkt Okk Rekt'Ikt") == ("2203.12.4", CountryId(K, 9, True, "Rocks", "Yrekkt Okk Rekt'Ikt"))
    assert rec(f"SWEEP_BEGIN|{K}")[1] == SweepBegin(K)
    assert rec(f"SWEEP_END|{K}")[1] == SweepEnd(K)
    assert rec("PAIR|3|7")[1] == Pair(3, 7)


def test_parse_rejects_unset_variables_and_noise():
    assert rec(f"COMMS|{K}||") is None          # ids not assigned yet at game start
    assert rec("ID|--|5|A|Unidentified Empire|x") is None
    assert parse_line("[10:00:00][x.cpp:1]: something else") is None
    assert rec("PAIR|4|4") is None


def test_parse_name_with_pipe_keeps_ruler_last():
    _, r = rec(f"ID|{K}|2|A|Odd|Name|The Ruler")
    assert (r.name, r.ruler, r.human) == ("Odd|Name", "The Ruler", False)


# ---- state ----------------------------------------------------------------

def test_sweep_replaces_pairs_and_reports_changes():
    proc = Processor(Store())
    changes = feed_all(proc, f"SWEEP_BEGIN|{K}", f"ID|{K}|1|H|A|r", f"ID|{K}|2|H|B|r", "PAIR|1|2", "PAIR|2|1", f"SWEEP_END|{K}")
    assert GameSwitched(K) in changes
    assert PairsChanged(K, frozenset([frozenset((1, 2))]), frozenset()) in changes
    # A later sweep without the pair (e.g. an older save was loaded) removes it
    changes = feed_all(proc, f"SWEEP_BEGIN|{K}", f"SWEEP_END|{K}")
    assert PairsChanged(K, frozenset(), frozenset([frozenset((1, 2))])) in changes
    assert proc.store.current.pairs == set()


def test_incomplete_sweep_is_ignored():
    proc = Processor(Store())
    feed_all(proc, f"COMMS|{K}|1|2")
    feed_all(proc, f"SWEEP_BEGIN|{K}")      # game closed mid-sweep, then relaunched
    feed_all(proc, f"SWEEP_BEGIN|{K}", "PAIR|1|2", f"SWEEP_END|{K}")
    assert proc.store.current.pairs == {frozenset((1, 2))}


def test_comms_is_idempotent_and_games_are_separate():
    proc = Processor(Store())
    assert feed_all(proc, f"COMMS|{K}|1|2")
    assert not [c for c in feed_all(proc, f"COMMS|{K}|2|1") if isinstance(c, PairsChanged)]
    other = "999-888-777"
    assert GameSwitched(other) in feed_all(proc, f"SWEEP_BEGIN|{other}", f"SWEEP_END|{other}")
    assert proc.store.current.pairs == set()
    assert proc.store.games[K].pairs == {frozenset((1, 2))}


def test_store_round_trip(tmp_path):
    store = Store(tmp_path / "state.json")
    proc = Processor(store)
    feed_all(proc, f"ID|{K}|1|H|Rocks|r", f"COMMS|{K}|1|2")
    store.current.claims[42] = 1
    store.current.channels[1] = 555
    store.save()
    loaded = Store.load(tmp_path / "state.json")
    assert loaded.current_key == K
    g = loaded.current
    assert g.pairs == {frozenset((1, 2))} and g.claims == {42: 1} and g.channels == {1: 555}
    assert g.countries[1].name == "Rocks"


# ---- embassy permissions ------------------------------------------------

def test_embassy_access_is_not_transitive():
    g = Game(K)
    g.pairs = {frozenset((1, 2)), frozenset((2, 3))}
    g.claims = {101: 1, 102: 2, 103: 3, 104: 2}    # 104 shares empire 2
    e = desired_embassies(g)
    assert e[1].allowed_users == {101, 102, 104}
    assert e[2].allowed_users == {101, 102, 103, 104}
    assert e[3].allowed_users == {102, 103, 104}    # Rocks (101) can't reach Glebsig
    assert e[1].name == "Empire 1"                  # no ID line seen yet


def test_unclaimed_empires_get_no_channel():
    g = Game(K)
    g.pairs = {frozenset((1, 2))}
    g.claims = {101: 1}
    assert set(desired_embassies(g)) == {1}
    assert desired_embassies(None) == {}


# ---- log tailing ----------------------------------------------------------

def test_tailer_handles_partial_lines_and_truncation(tmp_path):
    path = tmp_path / "game.log"
    t = LogTailer(path)
    assert t.poll() == []
    path.write_text("one\ntw", encoding="utf-8")
    assert t.poll() == ["one"]
    with open(path, "a", encoding="utf-8") as f:
        f.write("o\n")
    assert t.poll() == ["two"]
    path.write_text("new\n", encoding="utf-8")   # game relaunched
    assert t.poll() == ["new"]


def test_parse_hail():
    from proxychat_bot.protocol import Hail
    from proxychat_bot.state import Hailed
    _, r = rec(f"HAIL|{K}|9|14|A|Rocks|Locust Swarm")
    assert r == Hail(K, 9, 14, False, "Rocks", "Locust Swarm")
    assert Hailed(r) in Processor(Store()).feed("2210.1.1", r)
    assert rec(f"HAIL|{K}||14|A|Rocks|X") is None


def test_parse_join_and_decline():
    from proxychat_bot.protocol import HailDeclined, Join
    from proxychat_bot.state import Requested
    assert rec(f"JOIN|{K}|1|7")[1] == Join(K, 1, 7)
    assert rec(f"HAIL_DECLINED|{K}|1|7")[1] == HailDeclined(K, 1, 7)
    assert rec(f"JOIN|{K}|1|") is None            # target never got an id
    assert Requested(Join(K, 1, 7)) in Processor(Store()).feed(None, Join(K, 1, 7))


def test_calls_teach_empire_names_before_first_sweep():
    proc = Processor(Store())
    feed_all(proc, f"HAIL|{K}|1|2|H|Solaris|Krithakkan Commerce League")
    g = proc.store.current
    assert g.countries[1].name == "Solaris" and g.countries[2].name == "Krithakkan Commerce League"
    # A later sweep's ID line still wins (it also has the ruler)
    feed_all(proc, f"ID|{K}|1|H|Solaris Union|Queen Sol")
    assert (g.countries[1].name, g.countries[1].ruler) == ("Solaris Union", "Queen Sol")


def test_galcom_members_follow_sweeps():
    from proxychat_bot.protocol import GalcomJoin, GalcomMember, Home
    from proxychat_bot.state import GalcomChanged, Requested
    assert rec("GALCOM|3")[1] == GalcomMember(3)
    assert rec(f"HOME|{K}|3")[1] == Home(K, 3)
    assert rec(f"GALCOM_JOIN|{K}|3")[1] == GalcomJoin(K, 3)
    assert rec(f"HOME|{K}|") is None

    proc = Processor(Store())
    changes = feed_all(proc, f"SWEEP_BEGIN|{K}", "GALCOM|1", "GALCOM|2", f"SWEEP_END|{K}")
    assert GalcomChanged(K) in changes and proc.store.current.galcom == {1, 2}
    # Galcom lines outside a sweep are ignored; a later sweep replaces the set
    feed_all(proc, "GALCOM|9", f"SWEEP_BEGIN|{K}", "GALCOM|2", f"SWEEP_END|{K}")
    assert proc.store.current.galcom == {2}
    # Joining from the menu proves membership straight away
    changes = feed_all(proc, f"GALCOM_JOIN|{K}|5")
    assert proc.store.current.galcom == {2, 5}
    assert Requested(GalcomJoin(K, 5)) in changes


def test_store_round_trip_keeps_galcom(tmp_path):
    store = Store(tmp_path / "state.json")
    store.game(K).galcom = {1, 4}
    store.current_key = K
    store.save()
    assert Store.load(tmp_path / "state.json").current.galcom == {1, 4}


# ---- federations ----------------------------------------------------------

def test_parse_federation_records():
    from proxychat_bot.protocol import FederationAssociate, FederationJoin, FederationMember
    assert rec("FED|4|2|The Galactic Union")[1] == FederationMember(4, 2, "The Galactic Union")
    assert rec("FED_ASSOC|4|3|Pipes | Union")[1] == FederationAssociate(4, 3, "Pipes | Union")
    assert rec(f"FED_JOIN|{K}|3|4|A|The Galactic Union")[1] == FederationJoin(K, 3, 4, False, "The Galactic Union")
    assert rec(f"FED_JOIN|{K}|2|4|M|The Galactic Union")[1].member is True
    # Unset federation id (empty or 0) and bad roles are dropped
    assert rec("FED||2|X") is None
    assert rec("FED|0|2|X") is None
    assert rec(f"FED_JOIN|{K}|2|4|Q|X") is None


def test_federations_follow_sweeps_and_requests():
    from proxychat_bot.protocol import FederationJoin
    from proxychat_bot.state import Federation, FederationsChanged, Requested
    proc = Processor(Store())
    changes = feed_all(proc, f"SWEEP_BEGIN|{K}", "FED|4|1|Union", "FED|4|2|Union", "FED_ASSOC|4|3|Union",
                       f"SWEEP_END|{K}")
    assert FederationsChanged(K) in changes
    assert proc.store.current.federations == {4: Federation(4, "Union", {1, 2}, {3})}
    # Same again: nothing changed
    changes = feed_all(proc, f"SWEEP_BEGIN|{K}", "FED|4|1|Union", "FED|4|2|Union", "FED_ASSOC|4|3|Union",
                       f"SWEEP_END|{K}")
    assert FederationsChanged(K) not in changes
    # Renamed, and 2 left: the sweep replaces everything
    feed_all(proc, f"SWEEP_BEGIN|{K}", "FED|4|1|New Union", f"SWEEP_END|{K}")
    assert proc.store.current.federations == {4: Federation(4, "New Union", {1}, set())}
    # A menu request proves membership before the next sweep, and moves 1 out of its old federation
    changes = feed_all(proc, f"FED_JOIN|{K}|1|7|M|Pact", f"FED_JOIN|{K}|5|4|A|New Union")
    feds = proc.store.current.federations
    assert feds[7].members == {1} and feds[4].members == set() and feds[4].associates == {5}
    assert Requested(FederationJoin(K, 5, 4, False, "New Union")) in changes
    # Sweep lines outside a sweep are ignored
    feed_all(proc, "FED|9|1|Stray")
    assert 9 not in proc.store.current.federations


def test_federation_rooms_need_a_claimed_member():
    from proxychat_bot.embassy import desired_federations
    from proxychat_bot.state import Federation
    game = Game(K, claims={101: 1, 103: 3})
    game.federations = {4: Federation(4, "Union", {2}, {3})}
    assert desired_federations(game) == {}            # only an associate is claimed
    game.claims[102] = 2
    room = desired_federations(game)[4]
    assert room.name == "Union" and room.allowed_users == {102, 103}
    game.federations[4].name = "  "
    assert desired_federations(game)[4].name == "Federation 4"


def test_store_round_trip_keeps_federations(tmp_path):
    from proxychat_bot.state import Federation
    store = Store(tmp_path / "state.json")
    game = store.game(K)
    game.federations = {4: Federation(4, "Union", {1, 2}, {3})}
    game.fed_channels = {4: 555}
    store.current_key = K
    store.save()
    loaded = Store.load(tmp_path / "state.json").current
    assert loaded.federations == game.federations and loaded.fed_channels == {4: 555}
