# Adding Proxy Chat to an existing server

This guide covers putting the bot on a server you don't own. It runs from **your
PC** and manages **its own category**, which the owner creates in the Stellaris
part of the channel list and hands to the bot. While you're testing, only the
testers can see or use any of it. Part A is for the server owner, Part B is for you.

## What the bot can and can't touch

There are two separate layers of protection.

**1. Discord's own permissions (the hard guarantee).** The bot's server role gets
only harmless permissions: View Channels, Connect, Speak and Video. Those let it
see and join voice, but not change anything. Everything that changes things
(Manage Channels, Manage Permissions, Move Members) is granted **only on the bot's
category**. Discord itself then refuses any attempt to edit, delete or reorder a
channel, role or member anywhere else, whatever the bot's code does.

**2. The bot's own rules.** Even inside its category, the bot only changes
channels **it created itself** that are **still inside the category**. If someone
drags one of its channels out, the bot stops managing it and leaves it alone. It
never edits or deletes the category itself, and never touches roles.

Inside its category the bot does three things:

- **Channels:** creates, renames, re-permissions, reorders and deletes its own voice channels.
- **Voice:** disconnects a player from one of its own channels when they're no
  longer allowed in it.
- **Messages:** sends DMs to players (join links) and, if configured, posts in one
  announcements channel.

When a player asks in game to join a channel and they're in a voice channel
elsewhere, the bot can't pull them out, because it has no Move Members there. They
get a DM link instead. That's the trade-off for the lockdown.

## What the owner should know

- The bot only creates, renames, reorders and deletes channels **it created
  itself**, and only inside the category it's given. It never touches other channels.
- On first start it creates a **#bot-commands** text channel in its category, for
  `/claim` and the other commands. It keeps that channel through clean-ups, and
  recreates it if it's deleted. It has no permissions of its own: it follows the
  category's, so whoever the owner lets see the category can see and use it.
- Inside its category, the voice channel order is:
  1. **Galactic Community** (only while a claimed empire is a member)
  2. One channel per federation with a claimed member, **A–Z**, named after the
     federation and open to its members and associates
  3. One channel per claimed empire, **A–Z**
  4. Temporary `Call: A ↔ B` channels, which are deleted once they've been empty for a minute
- Every channel it creates is **private**: hidden from `@everyone`, with access
  granted to individual players who've claimed an empire. Ordinary members don't
  see the category at all (A2 only lets the testers in).
  (Server admins with the **Administrator** permission always see every channel.
  Discord gives them that.)
- During testing, the slash commands are limited to a **Proxy Chat Tester** role
  (step A3), so nobody else can claim an empire and get channels.
- The bot is only online while your PC is running it. While it's off, its channels
  stay where they are but nothing gets updated.
- It uses slash commands only. It doesn't read messages and needs no privileged intents.

---

## Part A: the server owner

### A1. Invite the bot with only harmless permissions

The owner (or anyone with **Manage Server**) opens this link, picks the server,
and authorizes:

```
https://discord.com/oauth2/authorize?client_id=1563873901778567178&scope=bot+applications.commands&permissions=3147264
```

That grants only **View Channels, Connect, Speak, Video**. None of them let the bot
change anything. It needs Speak and Video because Discord only lets a bot give
players permissions the bot has itself.

**If the bot is already in the server** (invited with the older, bigger link):
go to **Server Settings → Roles →** the bot's role (named after the bot) and make
sure **only** View Channels, Connect, Speak and Video are on. In particular,
Administrator, Manage Server, Manage Channels, Manage Roles and Move Members must be **off**.

### A2. Create the bot's category

1. Right-click in the channel list, then **Create Category**. Name it something
   like **Stellaris Proxy Chat** and turn on **Private Category**.
2. Drag it to sit directly under the **Stellaris** category, so the two read as one section.
3. Right-click the new category, then **Edit Category**, then **Permissions**:
   - **@everyone**: View Channels ❌ (Private Category already does this)
   - Click **+** and add the **bot's role** (named after the bot). Set these to ✅:
     **View Channels, Manage Channels, Manage Permissions, Connect, Speak, Video, Move Members**

   This is the **only** place the bot gets Manage Channels, Manage Permissions and
   Move Members, so it can only change things inside this category.
   Adding the bot itself (the bot user) instead of its role works just as well.
   Either way, **Manage Permissions** here is the category's version of Manage Roles.
   - Click **+** and add the **Proxy Chat Tester** role (created in A3). Set
     ✅ **View Channels, Send Messages, Use Application Commands**. That's what
     lets testers see and use **#bot-commands**. It doesn't open up the voice
     channels, since each of those has its own per-player permissions.
4. Save. **Leave the category empty.** Don't add channels by hand. The bot fills it
   in, starting with **#bot-commands** when it first starts.

When you go live, swap the tester role on the category for `@everyone` (or your
Stellaris players' role) with the same three permissions.

Each channel the bot makes starts as a copy of the category's permissions. That
copy includes the bot role's entry, which is what lets it manage the channel
without server-wide rights. The bot then adds per-player access and removes other
roles' entries, but it never changes its own role's entry. So **don't edit the bot
role's entry on individual channels**. Change it on the category, and new channels
pick it up.

### A3. Limit the commands to testers

1. **Server Settings → Roles → Create Role** called **Proxy Chat Tester**. It
   doesn't need any permissions. Give it to everyone taking part in the test.
2. **Server Settings → Integrations →** the bot **→ Manage**.
3. Under **Roles & Members**, set **@everyone** to ❌ and add **Proxy Chat
   Tester** as ✅. That applies to every command the bot has.
4. The `/proxychat` admin commands work for anyone with **Manage Server** or the
   **Stellaris Volunteers** role (`admin_role_ids` in the host's config). The bot
   checks this itself, and anyone else who tries gets a refusal. Volunteers also need
   to get past the ❌ on @everyone above: add **Stellaris Volunteers** as ✅ there
   too, or at least on the `/proxychat` command.

When you go live, change @everyone back to ✅ (or remove the override) so
every player can `/claim`.

### A4. Send the host the IDs

Turn on **User Settings → Advanced → Developer Mode**, then:

| What | How | Required? |
|---|---|---|
| Server ID | Right-click the server icon, then **Copy Server ID** | Yes |
| Bot category ID | Right-click the **Stellaris Proxy Chat** category, then **Copy Category ID** | Yes |
| Announcements channel ID | Right-click a text channel, then **Copy Channel ID** | Optional. Gets "X and Y have established communications" posts. The bot needs View Channels and Send Messages there. For testing, use a channel only testers can see. |

---

## Part B: you (the host)

### B1. Make a separate config for this server

Use a separate config and state file for this server. That way your test
server's claims and channels stay apart, and switching between servers never
orphans channels.

Copy `config.toml` to `config.stellaris-server.toml` and change these values:

```toml
[discord]
token = ""                         # same bot, or leave empty to use PROXYCHAT_DISCORD_TOKEN
guild_id = 123456789012345678      # owner's Server ID
category_id = 123456789012345678   # the Stellaris Proxy Chat category's ID
category = "Stellaris Proxy Chat"  # ignored when category_id is set
announce_channel_id = 0            # or the announcements channel ID

[game]
log_path = ""
state_path = "state.stellaris-server.json"
poll_seconds = 1.0
```

If you keep the token in this file, copy it over from `config.toml`.

### B2. Start the bot

```bash
start_bot_stellaris_server.bat
```

Run only one config at a time. Both use the same bot account.

In the console, look for:

- `Logged in as <bot name>` means the token is fine.
- `Using category 'Stellaris Proxy Chat' (<id>)` means it found the category.
- `Server-wide permissions: none that change anything. Locked to its category.`
  means the lockdown is right. **Stop the bot** (Ctrl+C) if you instead see
  `The bot's server roles allow … server-wide`. Have the owner untick those
  permissions on the bot's role (A1), then start it again.
- `Missing on category …` means the category permissions from A2 are incomplete.
  The message lists exactly which ones.
- If you see `Category … not found, or the bot can't see it` instead, the
  category ID is wrong or the bot's role is missing from the category (A2). The bot
  creates **nothing** until this is fixed. It never makes a category of its own when
  `category_id` is set.

Nothing shows up in Discord yet. Channels only appear once someone runs `/claim`.

### B3. Smoke test without a real game

You can check it on the owner's server without a real game. In one terminal:

```bash
.venv\Scripts\python tools\fake_game.py fake_game.log
```

In a second terminal, run the bot against that log:

```bash
.venv\Scripts\python -m proxychat_bot --config config.stellaris-server.toml --log fake_game.log
```

Fake empires: **Rocks**, **Tzynn Empire**, **Glebsig Foundation**. One in-game
month passes every 3 seconds. Rocks meets Tzynn in month 3, Tzynn meets Glebsig in
month 6, and Glebsig renames to "Glebsig Union" in month 9.

| Step | Do | Expect |
|---|---|---|
| 1 | A non-tester account types `/` | The bot's commands aren't listed for them |
| 2 | Tester: `/empires` | Three empires listed |
| 3 | You: `/claim Rocks`. A second tester: `/claim Tzynn Empire` | The **Stellaris Proxy Chat** category appears for each of you, holding only your own embassy |
| 4 | Wait until month 3 has passed | Each of you can now also see the other's embassy, listed **Rocks**, then **Tzynn Empire** |
| 5 | Non-tester account | Still sees no category and no channels |
| 6 | `/proxychat assign` a third tester to **Glebsig Foundation** | **Glebsig Foundation** slots in **first** (A–Z), not at the end |
| 7 | Wait for month 9 | It's renamed **Glebsig Union** and stays in A–Z order |
| 8 | `/proxychat cleanup confirm:True` | Every empire, Galactic Community, federation and call channel is deleted and all claims are cleared. **#bot-commands** stays, along with the category. Run `/proxychat cleanup` on its own first: it lists every channel it would delete, and how many claims it would clear, without changing anything. |
| 9 | `/claim Rocks` again | Rocks' channel comes back, so clean-up doesn't break anything for the next game |

Stop both with Ctrl+C. Delete `fake_game.log`, and delete
`state.stellaris-server.json` so the fake game's claims don't carry over to the real one.

### B4. Real game

1. Every player enables **Proxy Chat** in their playset. It changes the checksum.
2. Start the bot with `start_bot_stellaris_server.bat`
   before or after launching Stellaris, either works.
3. After the first in-game month, each player runs `/claim` and picks their empire.

## Going live after testing

- Before going live, run `/proxychat cleanup confirm:True` to clear out the test channels and claims.
- The owner changes **@everyone** back to ✅ in the bot's Integrations settings (A3).
- On the category, swap the tester role for `@everyone` (or the players' role):
  View Channels, Send Messages, Use Application Commands, so players can use **#bot-commands**.
- Keep the category private. Players still see their own channels, because
  each channel grants them access directly.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Slash commands don't appear for a tester | Check that they have the Proxy Chat Tester role (A3). If nobody sees them, the bot was invited without `applications.commands`: re-open the invite link from A1. |
| `Discord refused to delete/update …` in the console | The bot role's entry on the category is missing something (A2), or the channel was made before 13 Nov 2026 (missing the bot role's entry). For an old channel, delete it by hand (the bot recreates it if needed), or **Edit Channel → Permissions → Sync now** so it gets the bot role entry back. The bot then reapplies the player permissions within 5 minutes, or straight away with `/proxychat resync`. The bot reports each refusal once and carries on with everything else. |
| Players can't be moved into a channel and get a DM link instead | Normal: Discord only lets bots move people who are already in a voice channel on that server. |
| Someone added a channel to the bot's category by hand | The bot leaves it alone, but it'll sit above the bot's channels. Better to move it elsewhere. |
| The owner dragged the bot's channels around | They're put back in order the next time the bot adds or removes a channel, or when it restarts. |
| A bot channel was deleted by hand | It's recreated on the next update (within 5 minutes, or straight away with `/proxychat resync`). |
| A new channel shows "This channel isn't in your channel list" | The server uses Discord **Onboarding**, which hides channels a member hasn't picked. Owner: **Server Settings → Onboarding → Default Channels**, add the **Stellaris Proxy Chat** category. Each player: **Browse Channels** (top of the channel list), then tick the whole category so new channels in it show up, or turn on **Show All Channels** in the server-name menu. The bot can't change this, because it would need server-wide Manage Server. |
