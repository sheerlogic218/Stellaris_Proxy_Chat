# Stellaris Proxy Chat

You can only voice-chat with players whose empires have established
communications with yours in Stellaris.

- **Mod** ([mod/proxychat](mod/proxychat)): logs player-to-player comms and a
  monthly sweep to `game.log`. **Every player** needs it, since it changes the checksum.
- **Bot** ([proxychat_bot](proxychat_bot)): runs on the **host's PC only**. It tails
  `game.log` and keeps one *embassy* voice channel per claimed empire. You may join
  your own embassy and the embassy of every empire you have comms with. Contact is
  not transitive: if A has met B and B has met C, A still can't reach C's channel.

Background: [FINDINGS.md](FINDINGS.md) (design) and [TESTING.md](TESTING.md) (in-game results).

## Setup (host)

1. **Python env** (once):
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
2. **Discord application**: at <https://discord.com/developers/applications>,
   create an app, then go to **Bot** and **Reset Token** to get a token. No
   privileged intents are needed.
3. **Invite the bot** (replace `APP_ID` with the application ID):
   ```
   https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=288362000
   ```
   This grants View Channels, Manage Channels, Manage Roles, Send Messages, Connect,
   Speak, Video and Move Members. The bot can only grant players permissions it has
   itself, which is why it needs Speak and Video.
4. **Config**: copy `config.example.toml` to `config.toml` and set `guild_id`.
   Set the token there, or in the `PROXYCHAT_DISCORD_TOKEN` environment variable.
5. **Run**:
   ```
   .venv\Scripts\python -m proxychat_bot
   ```
   Start it before or after Stellaris, either works. It replays the current
   `game.log` on startup.

## Playing

- Everyone enables **Proxy Chat** in their playset. The host runs the bot.
- After the first in-game month, each player runs `/claim` and picks their empire.
  Their embassy channel appears in the **Proxy Chat** category.
- `/empires` lists the player empires in the current game with their id numbers and
  who has claimed them. `/claim` takes a pick from the list, an id, or an exact name.
- `/contacts` lists who you can reach. `/unclaim` undoes a claim.
- **In game**, the Harm Relations button on a player empire is **Subspace Channel**
  (button, then the engine's envoy window, where no envoy is used, then the menu):
  - **Join their Voice Channel** moves you into their embassy, if you have comms with
    everyone already in it.
  - **Join the <federation> Voice Channel** (only shown when they're in a federation
    and you're a member of it or have association status with it) moves you into
    that federation's channel.
  - **Request a private call**: if they accept, the bot creates a temporary
    `Call: A ↔ B` channel only the two empires' players can see and moves you both
    in. It's deleted once it has been empty for a minute.
  - Discord only lets a bot move people who are already in a voice channel on the
    server. Anyone who isn't gets a DM with a link instead. Both features need the
    empires to be claimed.
- **Voice Chat button** (bottom-left of the main screen) opens a menu that lists
  the player empires you have communications with, plus:
  - **Return to my Voice Channel**: back to your own embassy.
  - **Join the Galactic Community Voice Channel** (only shown while the Galactic
    Community exists and you're a member). The **Galactic Community** channel is
    open to every claimed member empire. The occupancy rule doesn't apply there.
  - **Join the <federation> Voice Channel** for your own federation, and
    **Join the <federation> Voice Channel (associate)** for up to 4 federations you
    have association status with.
- **Federation channels:** every federation with at least one claimed member empire
  gets a voice channel named after the federation. Claimed members and empires with
  association status can join it; the occupancy rule doesn't apply. The channel
  appears a day after the federation forms (or someone joins or leaves it), follows
  renames at the monthly sweep, and is deleted once the federation is gone and the
  channel is empty. Gaining or losing association status shows at the next monthly
  sweep (or straight away when you use a federation join option).
  - The button lives in `interface/proxychat_maingui_flag.gui`, which replaces the
    `maingui_flag` window (the same trick Ultimate Automation uses, so those two
    mods conflict). Move it by changing its `position`.
  - The contact list uses 64 generated slots. Change the count with
    `python tools/gen_contact_slots.py --slots N`.
- Admins (Manage Server, or a role listed in `admin_role_ids`): `/proxychat status`, `/proxychat assign` (for shared
  empires or corrections), `/proxychat unassign`, `/proxychat resync`, and
  `/proxychat cleanup` (on its own it lists exactly what it would delete; `confirm:True` does it). Cleanup deletes every empire, Galactic Community,
  federation and call channel and clears all claims, but keeps the known empires and contacts.
- The bot keeps a **#bot-commands** text channel in its category (name set by
  `commands_channel`, `""` for none). It survives clean-ups, and its permissions
  follow the category's.
- **Occupancy rule:** everyone in a channel must have comms with everyone else in it.
  If A knows B and B knows C, but A and C haven't met, B's channel is **hidden** from
  C while A is in it (hidden rather than locked, so C can't see who's inside), and
  it reappears when A leaves. If two incompatible players join at the same moment,
  the first one stays and the later one is disconnected.
- When comms are established, the other player's embassy unlocks for you within a
  couple of seconds. If comms disappear (for example after loading an older save),
  the access goes away and anyone in a channel they're no longer allowed in is
  disconnected.
- Each campaign gets its own random **game key**, so claims and contacts are kept
  per campaign. Loading a different save switches automatically.

> The bot only manages channels it created (tracked in `state.json` under `owned`).
> To use a category someone else made, for example one the owner placed under their
> Stellaris section, set `category_id`.
> Setting it up on someone else's server: [SERVER_SETUP.md](SERVER_SETUP.md).

## Testing without Stellaris

```
.venv\Scripts\python tools\fake_game.py fake_game.log
.venv\Scripts\python -m proxychat_bot --log fake_game.log            # real bot, fake game
.venv\Scripts\python -m proxychat_bot --dry-run --log fake_game.log  # no Discord at all
.venv\Scripts\python -m pytest tests
```

The fake game has three empires: Rocks meets Tzynn in month 3, and Tzynn meets
Glebsig in month 6. A dry run against the real game is just
`python -m proxychat_bot --dry-run`.

## Log protocol (mod v0.1)

```
PROXYCHAT|SWEEP_BEGIN|<key>
PROXYCHAT|ID|<key>|<id>|<H|A>|<empire name>|<ruler name>
PROXYCHAT|PAIR|<id>|<id>
PROXYCHAT|SWEEP_END|<key>
PROXYCHAT|COMMS|<key>|<id>|<id>
```
`<key>` is `NNN-NNN-NNN`. `A` marks a player empire currently run by the AI (the
player dropped). Only complete sweeps replace the contact list. COMMS lines add
contacts between sweeps.
