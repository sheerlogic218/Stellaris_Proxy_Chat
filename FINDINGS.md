# Stellaris Proxy Chat — Feasibility Findings

Target game version: **Stellaris Cygnus v4.5.2** (from `launcher-settings.json`).
All claims below were checked against the vanilla install and the engine's own
`logs/script_documentation/*.log` dumps, not taken from the `mod/knowledge` notes.

**Verdict: feasible.** The game side is easy. Two things need care: tying in-game
empires to Discord users, and the fact that Discord can't do true per-listener
audio.

---

## 1. Game-side hooks (verified)

From `common/on_actions/00_on_actions.txt`:

| on_action | Fires when | Scope |
|---|---|---|
| `on_post_communications_established_always_fire` | **Every** comms establishment, including `establish_communications_no_message` | `This` = establishing country, `From` = other country |
| `on_post_communications_established` | Same, but **skips** silent establishments | same |
| `on_pre_communications_established` | Right before (skips silent ones) | same |

Use **`..._always_fire`**. The non-`always` variants miss scripted and silent comms
(origins, conquest `action.8`, Curators, etc.).

Gaps, and how to cover them:
- **No hook for losing comms.** `remove_communications` exists as an effect
  (vanilla uses it for pre-FTLs). Comms between real empires are effectively
  permanent, but a country can be destroyed (`on_country_destroyed`).
- **No MP save-load hook.** `on_single_player_save_game_load` says *"Does not run
  when loading MP saves due to OOS concerns"*.
- Both gaps are covered by a **periodic full sweep**: on `on_monthly_pulse` (or
  yearly), log every human↔human pair where `has_communications = <target>`
  holds. With ≤ ~10 humans this costs almost nothing, and the watcher can rebuild
  its whole state from one sweep.

## 2. Getting data out of the game (verified, with one caveat)

- The `log = "..."` effect is documented as *"Prints a message to game.log"*, and
  works in any scope.
- Localisation substitution works inside it. Vanilla `print_scope_effect` writes
  `log = "- This: \\[This.GetName]"`. Note the **`\\[` escape**, which `log` strings need.
- File: `Documents/Paradox Interactive/Stellaris/logs/game.log`. It is **truncated on
  every game launch**, and each line has a real-time `[HH:MM:SS]` prefix but no game date.

Example lines the mod would emit:
```
[21:14:03][...]: PROXYCHAT|COMMS|2231.04.01|3|United Nations of Earth|7|Tzynn Empire
[21:14:03][...]: PROXYCHAT|SWEEP_BEGIN|2231.05.01
[21:14:03][...]: PROXYCHAT|PAIR|3|7
[21:14:03][...]: PROXYCHAT|SWEEP_END|2231.05.01
```

**To test first:** whether `log` writes to `game.log` without the `-debug`
launch flag. I expect it does, since `game.log` fills during normal play (yours
has `eventcommands.cpp` lines), but nothing confirms it yet. If it doesn't, the
fallback is to have the watcher parse **autosaves** instead. Saves contain the comms
relations, and the `save_parser.py` approach in Stellaris_Tech_Tree already reads
`.sav` zips. That costs latency (one save per autosave interval), but no mod
logging is needed.

Also to test: how fast `game.log` flushes. Paradox logs are usually written
promptly, but measure it.

## 3. Multiplayer / "only the host runs software"

- Script effects run deterministically on **every** client, so the host's own
  `game.log` gets the lines too. **Only the host needs the watcher and bot.** ✅
- **Every player still needs the mod installed.** Events and on_actions change the
  game checksum, and MP requires matching checksums. A Steam Workshop subscribe
  takes care of this. There's no way to make it host-only on the game side.
- The mod has to stay **OOS-safe**: it may only read state and `log`. If it sets
  anything (such as an ID variable, below), it must be deterministic. Never use
  local-only triggers like `logged_in_to_pdx_account` in logic.
- Ironman: a modded checksum already rules out achievements, which is normal for MP mods.

## 4. Identity: empire ↔ Discord user (the main design problem)

The `Country` localisation commands include `GetName`, `GetAdj`, `GetRulerName`
and so on, but **no player/account name and no numeric country ID**. Empire names
can also be renamed during play. Options:

1. **Mod-assigned stable ID (recommended).** On `on_game_start_country` (or the
   first sweep), give each human country a `proxychat_id` variable from a global
   counter, then log `\\[This.proxychat_id]` (the loc docs confirm
   `[Scope.my_variable]` works). This is deterministic and OOS-safe. Each sweep
   also logs `id → current empire name`, so renames are tracked.
2. Players link themselves in Discord: `/claim` shows a dropdown of empire names the
   watcher has seen, and the host can override.
3. Optional cross-check: the save file's `player={ name=... country=N }` block maps
   Paradox account names to country IDs.

## 5. Discord side: what's actually possible

Discord voice channels are all-hear-all. A bot **cannot** make A hear B while C
in the same channel doesn't. Contact is also **not transitive** (A–B and B–C in
contact doesn't mean A–C). So "proxy chat" has to be approximated:

| Approach | How | Verdict |
|---|---|---|
| **Permission-gated "embassy" channels** | Each empire gets a home VC. The bot adds a `Connect` permission overwrite on each player's channel for everyone they have comms with. Players hop into each other's channels. | **Recommended.** Simple, uses standard bot permissions (Manage Channels/Roles), and models the non-transitive graph exactly. |
| Per-pair channels | One VC per contact pair, created on demand | Channel count grows quadratically (10 players → 45). Fine for small games. |
| Bot moves people into cliques | Compute groups and force-move members | Fights users, and cliques aren't unique. No. |
| Bot relays/mixes audio per listener | Receive and re-send voice | Discord voice receive is unofficial, and one bot connection per guild means N bots. Far too complex. |

Nice extras: a text "subspace channel" per pair, federation-wide VCs (the sweep can
also log `is_in_federation_with`), and a lobby/"pre-contact" channel where everyone
is muted from each other.

## 6. Proposed architecture

```
Stellaris (host)                         Host machine
 mod: on_post_communications_  ──log──▶ game.log ──tail──▶ watcher ──▶ Discord bot
      established_always_fire                              (state:     (discord.py:
 mod: monthly sweep of human pairs                          pairs,      /claim, perms
                                                            names)      on VCs)
```

- **Mod**: `common/on_actions/proxychat_on_actions.txt` plus
  `events/proxychat_events.txt` (hidden `country_event`s, `is_triggered_only = yes`,
  `hide_window = yes`), filtered to `is_ai = no` on both sides.
- **Watcher + bot**: one Python process (the language you already use).
  It tails `game.log`, handles truncation on relaunch, parses `PROXYCHAT|` lines,
  stores state in JSON/SQLite so restarts survive, and diffs the state to
  apply permission overwrites.

Sketch of the mod events (not tested yet):
```pdx
namespace = proxychat

country_event = {            # fired from on_post_communications_established_always_fire
    id = proxychat.1
    hide_window = yes
    is_triggered_only = yes
    trigger = { is_ai = no  from = { is_ai = no } }
    immediate = {
        log = "PROXYCHAT|COMMS|\\[GetDate]|\\[This.proxychat_id]|\\[This.GetName]|\\[From.proxychat_id]|\\[From.GetName]"
    }
}
```

## 7. Risks / open questions

1. `log` in non-debug builds (§2). **Test this first;** it decides the approach.
2. `is_ai = no` during hotjoin/drop: a dropped player's empire turns AI, so the
   sweep should log all *playable* countries that have a claimed ID, not only
   `is_ai = no`.
3. `GetDate` as an unscoped loc command inside `log` is listed in the loc docs but
   not yet tested.
4. Does `_always_fire` fire once or twice per pair (once per direction)? The watcher
   should be idempotent either way.
