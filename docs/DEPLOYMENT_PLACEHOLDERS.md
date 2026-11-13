# Deployment placeholders: values to fill in

Everything specific to a deployment is left as a placeholder in the repo. This is the
list to go through before going live, and the list of questions a future installer
script could ask.

**Conventions:**
- In config files a placeholder looks like `CHANGE_ME_<NAME>`. Code treats any value starting with `CHANGE_ME` as **unset**: the feature that needs it refuses to work and says so, and the server logs a warning at startup.
- `grep -rn CHANGE_ME` finds every placeholder still left in a config file.
- Values typed on the command line are shown as `<NAME>` instead.
- Real ids and addresses never go in the repo. The repo is public, so keep them in your local, git-ignored `server.toml` and in your notes.

**Columns:**
- **Who** is who knows the value:
  - **operator** means you, running the central server;
  - **owner** means a Discord server owner using the bot;
  - **host** means a volunteer running a relay.
- **Ask?** says whether an installer could simply ask for the value (✅), or whether it's derived, generated or needs a manual step first (⚙️).

---

## Central server: `server.toml` (copy of `server.example.toml`)

| Placeholder or key | What it is | How to find it | Example | Who | Ask? |
|---|---|---|---|---|---|
| `CHANGE_ME_OWNER_USER_ID` (`[server] owner_user_id`) | Your Discord user id, for alert DMs | Discord → Settings → Advanced → Developer Mode on, then right-click yourself → **Copy User ID** | `123456789012345678` | operator | ✅ |
| `CHANGE_ME_PUBLIC_URL` (`[server] public_url`) | The HTTPS address relays connect to: your Tailscale Funnel name. Until it's set, `/proxychat host` refuses. | After Funnel is set up in M6: `docker exec ts tailscale funnel status`, or the machine's DNS name in the Tailscale admin console | `https://proxychat.tail1234.ts.net` | operator | ⚙️ (exists only after Tailscale is set up) |
| `[server] db_path` | Where the database lives | Default suits the container | `/data/proxychat.db` | operator | default |
| `[server] token_file` | Where the bot reads its token | Default suits the container. The file itself is set up in M6. | `/run/secrets/discord_token` | operator | default |
| `[api] listen_host` / `listen_port` | Where the API listens in its container | Must match the proxy target in `deploy/ts-serve.json` (M6) | `0.0.0.0` / `8080` | operator | default |
| `[api] health_port` | Health check port, localhost only | Default | `8081` | operator | default |
| `[api] min_relay_version` | Oldest relay version accepted | Raise it when the relay protocol changes | `0.1.0` | operator | no (a release decision) |

## Central server: secrets (never in any file in the repo)

| Value | What it is | How to find it | Who | Ask? |
|---|---|---|---|---|
| Discord bot token | The bot's login | Developer Portal → your app → **Bot** → **Reset Token**. It goes in the token file (M6), with mode 0400. | operator | ✅ (an installer must write it straight to the secret file, never echo it) |

## Central server: command-line values (admin CLI)

| Value | Used in | How to find it | Who | Ask? |
|---|---|---|---|---|
| `<GUILD_ID>` for each server to approve | `admin_cli guild approve <GUILD_ID>` | The server owner: Developer Mode → right-click the server → **Copy Server ID**. The bot also DMs you the id when an unapproved server adds it. | owner gives it to operator | ✅ |
| `<MAIN_GUILD_ID>`, `<MAIN_CATEGORY_ID>`, `<STATE_FILE>` | `admin_cli import-state <MAIN_GUILD_ID> <MAIN_CATEGORY_ID> <STATE_FILE> --label "<LABEL>"`, to move the main server over in M7 | `guild_id` and `category_id` in your local `config.stellaris-server.toml`, and `state.stellaris-server.json`. Copy the state file onto the server first. | operator | ⚙️ (an installer could read them from the old config) |
| `<LABEL>` | The slot's name for the main server | Your choice | operator | ✅ |
| Guild cap | `admin_cli set max-guilds <N>` | Already defaults to 5. Only change it deliberately. | operator | no |

## Discord application settings (Developer Portal)

| Setting | Value | When | Who |
|---|---|---|---|
| **Public Bot** | On, so other servers can invite it. Unapproved ones are left automatically. | Before opening up (M7–M8) | operator |
| Invite link `client_id` | Your application id. `SERVER_SETUP.md` still has it hard-coded; the M8 docs make it generic. | M8 | operator |

## Discord server owners (in Discord, no files)

| Value | How | Who |
|---|---|---|
| The slot's category | `/proxychat slot add category:<CATEGORY> label:<LABEL>`. The category needs the bot's role with Manage Channels, Manage Permissions and Move Members on it. | owner |
| Admin roles | `/proxychat roles add <ROLE>` (server managers only) | owner |

---

## Relay (on each game host's PC)

Set by `python -m proxychat_relay pair <server_url> <code> --label <name> [--log <path>]`. It's stored in
`%APPDATA%\ProxyChatRelay\relay.toml`, and the private key goes in Windows Credential Manager (`proxychat-relay`).
There are no placeholders in files: everything comes from these arguments.

| Value | What it is | Default or how to find it | Who | Ask? |
|---|---|---|---|---|
| Server URL | The central server's `public_url` | Shown by `/proxychat host` | host | ✅ (paste from Discord) |
| Pairing code | One-time, 10 minutes | `/proxychat host` (an admin runs it) | host | ✅ |
| Relay label | How the host shows up in `/proxychat relay list` | The host's name | host | ✅ |
| `game.log` path | Only if Stellaris logs somewhere unusual | `Documents\Paradox Interactive\Stellaris\logs\game.log` | host | ✅ (with the default filled in) |

## Home server deployment (Pop!_OS)

Step by step in [deploy/DEPLOY.md](../deploy/DEPLOY.md). Each file below is a copy of a committed
`*.example` file, and is ignored by git.

| Placeholder or value | File | What it is | How to find it | Who | Ask? |
|---|---|---|---|---|---|
| `CHANGE_ME_PYTHON_DIGEST` | `deploy/.env` (`PYTHON_IMAGE`) | Pins the Python base image | `docker pull python:3.14-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.14-slim` | operator | ⚙️ (an installer can run it) |
| `CHANGE_ME_TAILSCALE_DIGEST` | `deploy/.env` (`TS_IMAGE`) | Pins the Tailscale image | The same command for `tailscale/tailscale:stable` | operator | ⚙️ |
| `TS_HOSTNAME` | `deploy/.env` | The tailnet machine name, so the address is `https://<this>.<tailnet>.ts.net` | Your choice. Default `proxychat`. | operator | ✅ |
| `EDGE_SUBNET`, `TS_OUT_SUBNET`, `BOT_OUT_SUBNET` | `deploy/.env` (and the two `*_OUT` ones in `deploy/firewall.env` if changed) | Container networks the firewall targets | Defaults `172.30.0-2.0/24`. They must not overlap your LAN (`ip route`). | operator | ✅ |
| `EXTRA_BLOCKED` | `deploy/firewall.env` | Extra ranges the containers must not reach. All private ranges are already blocked. | Usually empty | operator | ✅ |
| Discord bot token | `deploy/secrets/discord_token` (mode 0400, owner `S+10001`) | The bot's login | Developer Portal → **Bot** → **Reset Token** | operator | ✅ (secret: no echo, straight to the file) |
| Tailscale auth key | `deploy/secrets/ts_authkey` (mode 0400, owner `S`) | Logs the sidecar in once. Single-use, pre-approved, tagged `tag:proxychat`. | Tailscale admin console → Settings → Keys | operator | ✅ (secret) |
| `S` | used in commands | Start of Docker's remapped user range | `grep dockremap /etc/subuid` after step 2 of DEPLOY.md | operator | ⚙️ (an installer can read it) |
| Tailscale ACL changes | admin console | Replace allow-all, add `tag:proxychat` and the `funnel` attribute | [deploy/tailscale-acl-snippet.jsonc](../deploy/tailscale-acl-snippet.jsonc) | operator | ⚙️ (manual) |
| `CHANGE_ME_AGE_RECIPIENT` | `deploy/backup.env` | Encrypts backups. The private key stays on your PC. | `age-keygen` on your PC, then copy the `age1…` line | operator | ✅ |
| `BACKUP_TARGET` | `deploy/backup.env` | Where encrypted backups are copied | For example `you@your-pc:/backups/proxychat/`, reached over SSH. Empty means keep them on the server only. | operator | ✅ |
| Install path | `deploy/systemd/*.service` | Where the repo is cloned | `/opt/proxychat`. Edit the units if you use another path. | operator | ✅ |
