# Testing the PoC mod

Mod source: [mod/proxychat](mod/proxychat). The launcher entry is
`Documents/Paradox Interactive/Stellaris/mod/proxychat.mod` and points here.

## Steps

1. Start the watcher in a terminal (the PoC's `watcher/tail_test.py` has been
   replaced by this):
   ```
   .venv\Scripts\python -m proxychat_bot --dry-run
   ```
2. In the Paradox Launcher, add **Proxy Chat (PoC)** to a playset. Launch
   **without** `-debug`, because that's the case we need to prove.
3. Start a new single-player game (non-ironman). Unpause.
4. Wait for the 1st of the next month. You should see a sweep:
   `SWEEP_BEGIN`, two `ESCTEST` lines, an `ID|..|H|<your empire>` line, `SWEEP_END`.
5. Meet someone. When first contact finishes you should see `ID` lines and
   `COMMS|<date>|<id>|<id>`. AI empires meeting each other also log, so these lines
   start showing up within a few years even if you don't explore.
   To force a sweep without waiting, open the console (`~`) and run `event proxychat.10`.

## What to record

| Question | Where to look |
|---|---|
| Does `log` write without `-debug`? | Any `PROXYCHAT` line appears at all |
| Which bracket form works in event files? | The `ESCTEST` line that shows a date (e.g. `2200.02.01`) rather than literal `[GetDate]` / `\[GetDate]` |
| Do variables print, and how? | `ID|1|...` vs `ID|1.000|...` vs `ID|0|...` (0 for everyone = the id copy from `global_event_country` failed) |
| Flush latency | The `[lag Xs]` column. Under ~2s is fine |
| Does comms fire once or twice per pair? | Count `COMMS` lines per contact |
| Script errors | `logs/error.log`. Search for `proxychat` |

If nothing appears at all, also run once **with** `-debug` to tell "log is
debug-only" apart from "the mod didn't load".

## Results so far

Test 1 (2026-10-25, new game, 2200.01.01):
- ✅ `log` writes to `game.log`. The line format is
  `[HH:MM:SS][effect_impl.cpp:N]: [<game date>] Log effect, <file/line>. PROXYCHAT|...`.
  The game date comes free in every line.
- ✅ Event files take a plain `[This.X]`. `\\[` resolves but leaves a stray `\`.
  Scripted effects need `\\[`.
- ✅ Variables print as integers (`ID|3|...`).
- ✅ COMMS fires **once per pair**, not once per direction.
- ❌ At game start, comms between starting countries (AI empires, Caravaneers and
  the like) fire **before** `global_event_country` exists, so the first country got
  id 0. Fixed: ids now wait until it exists, and the sweep fills them in.
- ❌ `GetName` resolved to **"Unidentified Empire"**. Names are rendered from the
  local player's point of view. The ID line now logs `GetNamePersistent`,
  `GetRulerName` and `GetSpeciesName` too, to find a variant that isn't hidden.
- ✅ The run was launched **without** `-debug` (confirmed by the user).

Test 2 (2026-10-25, new game, 2200 to 2209, single-player, no `-debug`):
- ✅ **`log` works in normal launches.**
- ✅ Lag from game to watcher is **0.1–1.3s**, most of it the 0.5s poll.
- ✅ The game-start fix works: starting countries get a blank id (no errors apart
  from 3 expected "Unknown property" warnings) and receive ids later
  (e.g. the Caravaneers got 16–18 when they next met someone).
- ✅ Names: "Unidentified Empire" was a **game-setup artifact**, not perspective.
  Countries simply aren't named yet at 2200.01.01. After that, `GetName` gives real
  names, even for countries the player hadn't met (Voidworms, Shroud, Hazaran...).
  `GetNamePersistent` returns raw loc keys (`NAME_Voidworms`, `%ADJECTIVE%...`), so it's useless.
  `GetRulerName` and `GetSpeciesName` resolve even at setup.
- ✅ The sweep runs every month, about 3 lines for one human.
- ℹ️ Player comms with the Locust Swarm (id 14) logged a COMMS line but no sweep
  PAIR. That's expected: it's a special non-playable country, and the sweep only
  iterates `every_playable_country`.
- ❓ Still untested: **human↔human** (needs a real MP game). That covers the PAIR
  output, H|H COMMS, and whether every client's log matches the host's.

## Next checks (mod v0.1: humans only, game key)

- [ ] Single-player dry run: `SWEEP_BEGIN|NNN-NNN-NNN` shows a 3-3-3 digit key,
      not blanks or `1.23K`-style formatting, and the key stays the same after save/reload.
- [ ] AI↔AI comms no longer log anything.
- [ ] `error.log` has no `proxychat` script errors.
- [ ] MP with a friend: `PAIR` lines appear after you meet, `/claim` + embassy
      channels unlock for both of you.

## Voice Chat button test

New in the mod: a **Voice Chat** button (bottom-left of the main screen, from
`interface/proxychat_maingui_flag.gui`) opens event `proxychat.40`.

| Check | Expected |
|---|---|
| Button visible, bottom-left, not covering anything | If it overlaps, change `position` in the .gui file |
| Empire flag (top-left) still opens the government screen | It's in the same window, which we replace |
| Button works with a planet/fleet selected | Yes (the effect uses `From` = player) |
| Menu text lists player empires you have comms with | Names, or "None yet." Blank lines or literal `[Root.GetProxychatContact1]` mean the slots didn't resolve |
| **Return to my Voice Channel** | Bot moves you to your embassy (log: `asked to return`) |
| **Join the Galactic Community Voice Channel** | Only shown once the Galactic Community exists and you're a member. The bot creates/joins a "Galactic Community" channel |
| **Join the <federation> Voice Channel** | Only shown while you're in a federation; the option names it. Bot moves you into the federation's channel (log: `asked to join the <name> channel`) |
| **Join the <federation> Voice Channel (associate)** | One per federation you have association status with (up to 4). Same as above |

## Federation channels test

| Check | Expected |
|---|---|
| Form a federation with another claimed player | A day later the bot log shows `Federation '<name>': players ...` and creates a channel named after the federation |
| Subspace Channel menu on a fellow member | Extra option **Join the <federation> Voice Channel** |
| Give a claimed player association status | Their Subspace Channel menu on a member, and their Voice Chat menu, offer the federation channel. It's open to them from the next monthly sweep, or as soon as they use the option |
| Option text | The federation's name, not `[proxychat_target.Federation.GetName]` literally |
| Leave/dissolve the federation | A day later, the channel's access updates; it's deleted once empty |
| `/proxychat status` | Lists each federation with its player members and associates |
| MP | No out-of-sync. Everyone needs the new mod version (re-upload to the Workshop) |
| `error.log` | Nothing about `proxychat`, `maingui_flag` or `proxychat_voice_button` |

## Current design (v4): Harm Relations = call button, Insult vanilla

Decided 2026-10-26: Insult stays completely vanilla (its Insult Efficiency and
other engine-side behaviour can't be reproduced exactly from script). Harm Relations
becomes **Subspace Channel**, shown only to human players looking at player empires.
Calling takes 3 clicks: the button, the engine's envoy window (no envoy is used), then
Join or Hail. Harm Relations is gone against AI empires, and AI empires can't use it.

Single-player test: set `effect set_global_flag = proxychat_test_ai_targets` in
the console so the button appears on AI empires, then use Join and Hail and check the
dry run shows `Join(...)` and `Hail(...)`.

## Subspace Channel test v3 (Insult = call, Harm Relations = Insult, for player targets)

**Dropped (not tested in-game)** in favour of v4, to keep Insult vanilla.

For a human player looking at a **player** empire:
- **Insult button → Subspace Channel**: opens the menu (2 clicks: button, then Join or Hail).
  Name and description switch per target through scripted loc
  (`GetProxychatInsultTitle` / `GetProxychatInsultDesc`).
- **Harm Relations button → Insult**: the engine's envoy window, then a full insult
  (opinion_insult −200, −5 trust, Satisfying Insults influence, 30-day cooldown,
  "Insult Received" popup for the target). No envoy is used.

For AI empires the Insult button is vanilla Insult, and **Harm Relations is
gone** (no button vs AI, and AI empires can't harm relations either). That's
because its envoy settings can't be kept for AI targets only.

Extra checks once the flag is set:
- The Harm Relations slot shows **Insult**. Using it (needs Poor or worse opinion)
  should give the AI the −200 "Insult" opinion, show an "Insult Received" popup
  (it goes to the AI, so you may not see it), and grey out for 30 days.

In single-player, AI empires aren't players, so testing needs a console flag
(open the console with `~`, non-ironman):

1. Fully restart Stellaris, start a new game, and run the dry run.
2. **Before** setting the flag: open an AI empire's diplomacy screen. The button
   should read **Insult** with the vanilla tooltip (greyed out unless their opinion
   of you is Poor or worse, which is normal).
3. Console: `effect set_global_flag = proxychat_test_ai_targets`
4. Reopen the diplomacy screen. The same button should now read **Subspace Channel**.
   One click should open the menu, with no confirmation window.
5. Pick **Join** and then **Hail**, and check the dry run shows `Join(...)` and `Hail(...)`.

| Check | Expected |
|---|---|
| Button name before / after the flag | Insult / Subspace Channel |
| Name shows as literal `[Recipient.GetProxychatInsultTitle]` | Scripted loc can't see `Recipient` in the title. Fixable |
| Confirmation window | None |
| AI gets "insulted" by the call (their opinion of you drops) | Possible: Insult may have a hard-wired opinion penalty. It doesn't matter for real player targets, but note it |
| `error.log` | Nothing about `proxychat` or `action_insult` other than the expected "already exists, using the one at" line |

## Subspace Channel test v2 (replaces Harm Relations)

**Result (2026-10-26): ✅ mostly works.**
- The override loaded (`error.log`: "using the one at ... 99_proxychat_actions.txt").
- Clicking it shows the engine's "send envoy" confirmation, but **no envoy is used**,
  and then our menu (`proxychat.20`) opens. So `on_accept` runs for this action.
- The button still read "Harm Relations": the button and the envoy prompt use the
  envoy task's text (`harm_relations`, `harm_relations_TITLE`, `_DESC`, `_desc`),
  not `ACTION_HARM_RELATION_*`. These are now overridden in `localisation/replace`.
- The text-file error from v1 is gone.
- Not yet tested: **Join** / **Hail** (the run picked "Close the channel", so
  `selectedOption 2`). Next run: pick Join and Hail and check the dry run shows
  `Join(...)` / `Hail(...)` with real ids, which also confirms the event targets
  carry over from the diplomatic action.

Same menu and hail popups as v1 below, now on `action_harm_relation` with its envoy
settings removed. It replaces Harm Relations for **every** target. Envoy settings
are per action, not per target, so it can't stay Harm Relations for AI empires.

1. Fully restart Stellaris, start a new game, and run the dry run.
2. Open an AI empire's diplomacy screen and look for **Subspace Channel** where
   Harm Relations was.
3. Same checks as v1. Also note:
   - Does clicking it ask for or use an envoy? (It shouldn't.)
   - Does the button show "Subspace Channel", or "Harm Relations"? The title may come
     from the envoy text `harm_relations_TITLE`, which I deliberately left unchanged.

## Subspace Channel test v1 (replaces Start Proxy War)

**Result (2026-10-26): ❌** The override itself loaded (`error.log`: "Object with key:
action_start_proxy_war already exists, using the one at ... 99_proxychat_actions.txt"),
but clicking the button opened the built-in Proxy War window. That click is hard-wired.
The same run hit a text-file error (real newlines in a `\n` string), which is now fixed.

`action_start_proxy_war` is redefined from `common/diplomatic_actions/99_proxychat_actions.txt`
so that clicking it opens the Proxy Chat menu (event `proxychat.20`). In this test version
AI empires can be targeted and always accept hails.

1. Fully restart Stellaris and start a new game (no Shroud DLC or traditions needed:
   the button's old requirements are replaced).
2. Run `.venv\Scripts\python -m proxychat_bot --dry-run`.
3. Meet an AI empire, open its diplomacy screen and click **Subspace Channel**
   (where Start Proxy War was).
4. Try each option:
   - **Join their embassy channel** → watcher prints `Join(...)`
   - **Hail them** → the AI accepts → you get a "Hail Accepted" popup and the
     watcher prints `Hail(...)`

| Result | Meaning |
|---|---|
| Button reads "Subspace Channel" and the menu opens | ✅ The proxy war slot can be repurposed |
| The built-in Proxy War window opens instead | ❌ The click is hard-wired to that window. Try another vanilla action, or use `/hail` |
| Button still says "Start Proxy War" | The text override in `localisation/replace` didn't apply. Cosmetic |
| Menu text shows blank empire names, or Join/Hail lines have an empty id | Event targets aren't carried from the diplomatic action into the event. Fixable by storing ids in variables |
| Button missing entirely | Check `error.log` for `proxychat` / `diplomatic` |

## Hail diplomatic action test

**Result (2026-10-26): ❌ not possible.** `error.log` shows
`[diplomaticaction_type.cpp:50]: Diplomatic action is missing token: action_proxychat_hail`.
The engine only accepts its built-in action tokens, so this is not a UI Overhaul issue.
The action and its text have been removed from the mod.

The same run confirmed mod v0.1: the game key prints as `338-133-894` (3-3-3 digits),
the sweep lists only the human empire, and there are no other `proxychat` errors.

Original test notes, for reference:

Question: will Stellaris accept a mod-defined diplomatic action (`action_proxychat_hail`)?
In this test version AI empires can be hailed and always accept, so it works in single-player.

1. Fully restart Stellaris (script changes only load at startup). A new game is safest.
2. Run `.venv\Scripts\python -m proxychat_bot --dry-run` (or `start_bot.bat` for the real bot).
3. Meet an AI empire, open its diplomacy screen and look for **Hail (Voice Call)**.
4. Use it. The AI should accept, and the watcher should print
   `Hail(... caller_name='<you>', target_name='<them>')`.

| Result | Meaning |
|---|---|
| Button shows, works, HAIL line appears | ✅ Custom diplomatic actions work: the native button route is viable |
| Button shows with raw text such as `ACTION_PROXYCHAT_HAIL_TITLE` | Action works, the text keys need renaming. Not a blocker |
| No button at all | Check `error.log` for `proxychat` / `diplomatic`. Probably the engine only supports built-in actions, so fall back to `/hail` in Discord |
| Button shows, no HAIL line | `on_accept` doesn't run for mod actions. Check `error.log` |

Also note: did the AI's reply text look broken or empty? AI empires look up
personality-specific greeting text, which this test doesn't provide.

## Notes

- The PoC logs comms for **every** country, AI included, so single-player games
  produce output. The real mod will restrict this to humans.
- The sweep only covers human countries and their contacts. In single-player that's
  you plus everyone you've met, every month.
- IDs are allocated lazily (first sweep or first comms event) and stored in the
  save, so they stay the same after reloading.
