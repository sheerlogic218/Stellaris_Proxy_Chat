"""Writes a fake multiplayer game.log, for testing the bot without Stellaris.

  python tools/fake_game.py fake_game.log [--month-seconds 3]

Point the bot at it with:  python -m proxychat_bot --log fake_game.log
Scenario (one in-game month per --month-seconds):
  month 1   sweep with three player empires, no contacts
  month 3   Rocks <-> Tzynn establish comms
  month 6   Tzynn <-> Glebsig establish comms (Rocks still can't reach Glebsig)
  month 9   Glebsig renames to "Glebsig Union"
  month 12  Rocks and Tzynn form the "Rocky Accord" federation; Glebsig gets association status
  then sweeps continue monthly until Ctrl+C
"""

import argparse
import time
from datetime import datetime

KEY = "111-222-333"
EMPIRES = {1: ["Rocks", "Yrekkt Okk"], 2: ["Tzynn Empire", "Kalak Vor"], 3: ["Glebsig Foundation", "Glebsig Prime"]}


def line(date, text):
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"[{stamp}][effect_impl.cpp:22217]: [{date}] Log effect, file: events/proxychat_events.txt line: 1. PROXYCHAT|{text}\n"


def ids(country):
    name, ruler = EMPIRES[country]
    return f"ID|{KEY}|{country}|H|{name}|{ruler}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("log")
    p.add_argument("--month-seconds", type=float, default=3.0)
    args = p.parse_args()

    pairs = set()
    with open(args.log, "w", encoding="utf-8") as f:  # truncate, like a game launch
        f.write("[00:00:00][game_application.cpp:250]: Game Version: Fake\n")

    month = 0
    while True:
        month += 1
        y, m = 2200 + (month - 1) // 12, (month - 1) % 12 + 1
        date = f"{y}.{m}.1"
        out = []
        if month == 3:
            pairs.add((1, 2))
            out += [line(f"{y}.{m}.1", ids(1)), line(date, ids(2)), line(date, f"COMMS|{KEY}|1|2")]
        if month == 6:
            pairs.add((2, 3))
            out += [line(date, ids(2)), line(date, ids(3)), line(date, f"COMMS|{KEY}|3|2")]
        if month == 9:
            EMPIRES[3][0] = "Glebsig Union"
        feds = []
        if month >= 12:
            feds = ["FED|1|1|Rocky Accord", "FED|1|2|Rocky Accord", "FED_ASSOC|1|3|Rocky Accord"]
        out.append(line(date, f"SWEEP_BEGIN|{KEY}"))
        out += [line(date, ids(c)) for c in EMPIRES]
        out += [line(date, f) for f in feds]
        for a, b in sorted(pairs):
            out += [line(date, f"PAIR|{a}|{b}"), line(date, f"PAIR|{b}|{a}")]
        out.append(line(date, f"SWEEP_END|{KEY}"))
        with open(args.log, "a", encoding="utf-8") as f:
            f.writelines(out)
        print(f"{date}: wrote {len(out)} lines, pairs={sorted(pairs)}", flush=True)
        time.sleep(args.month_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
