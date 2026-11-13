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

## Coming in M6: home server deployment (Pop!_OS)

This doesn't exist in the code yet. It's listed so the installer can plan for it.


| Value | What it is | How to find it | Who | Ask? |
|---|---|---|---|---|
| Tailscale auth key | Single use, pre-authorised, tagged `tag:proxychat` | Tailscale admin console → Settings → Keys | operator | ✅ (secret: straight to a file, deleted after first start) |
| Tailnet name | Part of `public_url` | Tailscale admin console → DNS | operator | ⚙️ |
| Tailscale ACL changes | Replace the default allow-all rule, add `tag:proxychat` and the `funnel` attribute | Admin console → Access controls. The snippet comes in `deploy/`. | operator | ⚙️ (manual) |
| Docker network subnets | Fixed subnets for the firewall rules | Defaults will be suggested, for example `172.30.1.0/24` and `172.30.2.0/24`. They must not clash with your LAN. | operator | ✅ |
| LAN ranges to block | Your home network's private ranges | Your router's settings, or `ip route` on the server | operator | ✅ |
| Backup target | Where nightly encrypted backups are copied | A path on your PC or a NAS, reached over Tailscale | operator | ✅ |
| `age` public key | Encrypts backups. The private key stays off the server. | `age-keygen` on your PC, then copy the `age1…` line | operator | ✅ |
| Image digests | Pinned `python:…-slim` and `tailscale/tailscale` images | Docker Hub, or `docker pull` then `docker inspect` | operator | ⚙️ |
| Data folder ownership | `./data` owned by the remapped container user | Computed from `/etc/subuid` (`dockremap`) | operator | ⚙️ (an installer can work it out) |
