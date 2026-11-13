# Deploying the central server (Pop!_OS)

**What you end up with:** three hardened containers on your home server. Only a Tailscale Funnel address is public; nothing is port-forwarded and your home IP isn't exposed. The design is in [../docs/SERVER_RELAY_DESIGN.md](../docs/SERVER_RELAY_DESIGN.md).

**Conventions:**
- Every value marked **(placeholder)** is listed in [../docs/DEPLOYMENT_PLACEHOLDERS.md](../docs/DEPLOYMENT_PLACEHOLDERS.md), with how to find it.
- `S` below means the start of Docker's remapped user range (step 2). The containers' user 10001 is host user **`S + 10001`**.
- All commands run on the server, from `/opt/proxychat` unless they say otherwise.

> ⚠️ **One bot at a time.** The server uses the same Discord token as your local bot. Never run both at once. Step 8 moves your existing servers over (this is milestone M7), and step 9 is the switch.

---

## 1. Docker Engine

Pop!_OS is based on Ubuntu, so use Docker's Ubuntu repository:

```bash
sudo apt update && sudo apt install -y ca-certificates curl git age rsync unattended-upgrades
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$UBUNTU_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

## 2. Docker settings: user-namespace remapping

```bash
sudo git clone -b server-relay https://github.com/sheerlogic218/Stellaris_Proxy_Chat /opt/proxychat
sudo cp /opt/proxychat/deploy/daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
grep dockremap /etc/subuid
```

The last line prints `dockremap:S:65536`. Note `S`, for example `165536`. Root inside a container is then an ordinary, unprivileged user on the host.

If you already have containers, they won't be visible after remapping (they live under a new directory). That's expected; this server should only run Proxy Chat's.

## 3. Automatic security updates

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

Answer **Yes**. Manage the server over your LAN or Tailscale SSH only. Use SSH keys and turn off password login in `/etc/ssh/sshd_config` (`PasswordAuthentication no`).

## 4. Tailscale: policy, certificates and an auth key

In the [Tailscale admin console](https://login.tailscale.com/admin):

1. **Access controls:**
   - **New tailnet, nothing else in it:** replace the whole policy with [tailscale-policy-fresh.jsonc](tailscale-policy-fresh.jsonc).
   - **Otherwise:** merge in [tailscale-acl-snippet.jsonc](tailscale-acl-snippet.jsonc). **First** replace the default allow-all rule with explicit rules for your own devices, if it's still there.
2. **DNS → HTTPS Certificates:** enable. Funnel needs it.
3. **Settings → Keys → Generate auth key:** set it **single-use**, **pre-approved**, with tag **`tag:proxychat`**. This is the **(placeholder)** Tailscale auth key.

You don't need Tailscale installed on the host itself for this; the sidecar container is its own node.

## 5. Settings and secrets

```bash
cd /opt/proxychat/deploy
sudo mkdir -p config secrets data backups
sudo cp ../server.example.toml config/server.toml
sudo cp .env.example .env
sudo cp backup.env.example backup.env
```

- **`config/server.toml`:** set `owner_user_id` **(placeholder)**. Leave `public_url` for step 8.
- **`.env`:** set the pinned image digests **(placeholder)**:
  ```bash
  docker pull python:3.14-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.14-slim
  docker pull tailscale/tailscale:stable && docker inspect --format '{{index .RepoDigests 0}}' tailscale/tailscale:stable
  ```
  Check that the subnets don't clash with your LAN.
- **The secrets:** these prompts don't echo, so nothing lands in your shell history.
  ```bash
  read -rs -p "Discord bot token: " T && printf '%s' "$T" | sudo tee secrets/discord_token >/dev/null; unset T; echo
  read -rs -p "Tailscale auth key: " K && printf '%s' "$K" | sudo tee secrets/ts_authkey >/dev/null; unset K; echo
  ```
- **Ownership:** use your `S` from step 2.
  ```bash
  S=165536
  sudo chown "$((S + 10001)):$((S + 10001))" secrets/discord_token data
  sudo chmod 0400 secrets/discord_token && sudo chmod 0700 data
  sudo chown "$S:$S" secrets/ts_authkey && sudo chmod 0400 secrets/ts_authkey
  sudo chmod 0700 secrets backups
  ```

## 6. The firewall: containers stay off your LAN

```bash
sudo chmod +x /opt/proxychat/deploy/*.sh
sudo cp /opt/proxychat/deploy/systemd/proxychat-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proxychat-firewall.service
sudo iptables -L PROXYCHAT-EGRESS -n
```

The last command lists DROP rules for the private ranges. If your network uses anything unusual, copy `firewall.env.example` to `firewall.env` and set `EXTRA_BLOCKED`.

## 7. Build the images

```bash
cd /opt/proxychat/deploy
sudo docker compose build
```

## 8. Register your servers BEFORE the first start

> ⚠️ **Do this before the bot ever starts.** When the bot starts, it leaves every Discord server that isn't approved here. It's the same Discord app as your existing bot, so skipping this step makes it walk out of your servers.

These commands use one-off containers, `docker compose run --rm`, so nothing is running yet.

1. **Copy your local state files onto the server** (from your PC: scp, a USB stick, anything). Put them in `deploy/data/import/`:
   ```bash
   sudo mkdir -p data/import
   # e.g. from your PC: scp state.stellaris-server.json state.json you@server:/tmp/   then:
   sudo mv /tmp/state.stellaris-server.json /tmp/state.json data/import/
   sudo chown -R "$((S + 10001)):$((S + 10001))" data/import
   ```
2. **Approve each server and import its state.** The ids come from your local `config.*.toml` files:
   ```bash
   alias pcrun='sudo docker compose run --rm --no-deps bot python -m proxychat_server.admin_cli'
   pcrun guild approve <MAIN_GUILD_ID> --note "main server" --admin-role <ADMIN_ROLE_ID>
   pcrun import-state <MAIN_GUILD_ID> <MAIN_CATEGORY_ID> /data/import/state.stellaris-server.json --label "Main" --commands-channel ""
   pcrun guild approve <TEST_GUILD_ID> --note "test server"
   pcrun import-state <TEST_GUILD_ID> <TEST_CATEGORY_ID> /data/import/state.json --label "Test"
   pcrun status
   ```
   - `--commands-channel ""` matches a local `commands_channel = ""`, so the bot won't create a `#bot-commands` channel there.
   - `--admin-role` matches the local `admin_role_ids`.
   - Leave both options out where the local config uses the defaults.
3. **Delete the copies.** They contain players' Discord ids, and the database now has everything:
   ```bash
   sudo rm -r data/import
   ```

The bot recognises the channels it already made and keeps everyone's claims. Keep the original files on your PC as a rollback: `admin_cli export-state` writes a slot back into that format.

## 9. Start it

> ⚠️ **Stop the local bot for good** before this, on every PC that has ever run it (`start_bot*.bat`). Two copies with one token fight over the channels.

```bash
cd /opt/proxychat/deploy
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs -f
```

What to expect in the logs:
- a warning that `public_url` is a placeholder, and `owner_user_id` too if you left it;
- the bot logging in and serving your slots;
- the `ts` container logging in to Tailscale.

If a slot is refused for dangerous permissions, the log says which ones. Untick them on the bot's role in that Discord server, as in `SERVER_SETUP.md`.

The auth key is single-use, so it's spent now. You can also revoke it in the console; the node stays logged in through its saved state.

**About the slash commands:** they're now registered globally. The old per-server copies are cleared at startup, but Discord apps can take a few minutes to show the change.

## 10. The public address

```bash
sudo docker compose exec ts tailscale funnel status
```

This shows `https://proxychat.<tailnet>.ts.net`. Put it in `config/server.toml` as `public_url` **(placeholder)**, then:

```bash
sudo docker compose restart bot
```

Volunteers can now pair: an admin runs `/proxychat host` in the server, and the host runs `setup_relay.bat` and then the command it shows.

## 11. Self-tests

From a machine that is **not** on your tailnet, such as a phone hotspot or a PC with Tailscale off:

```bash
deploy/selftest-outside.sh https://proxychat.<tailnet>.ts.net
```

On the server:

```bash
sudo deploy/selftest-inside.sh <your-pc's-tailnet-name>
```

Both should end with `0 failed`. Re-run them after any change to these files.

## 11b. Backups

1. **On your PC,** not the server: `age-keygen -o proxychat-backup.key`. Keep that file safe and offline; it's the only way to read backups. Copy the `age1…` public key it prints.
2. **On the server,** in `deploy/backup.env`, set `AGE_RECIPIENT` **(placeholder)** and optionally `BACKUP_TARGET` **(placeholder)**.
3. Run one backup now, then switch on the nightly timer:
   ```bash
   sudo /opt/proxychat/deploy/backup.sh
   sudo systemctl enable --now proxychat-backup.timer
   ```
4. **Test a restore once,** on your PC: `age -d -i proxychat-backup.key proxychat-<date>.db.age > restored.db`. Then open it with `sqlite3 restored.db ".tables"`.

## 11c. More servers later

```bash
sudo docker compose exec bot python -m proxychat_server.admin_cli guild approve <GUILD_ID> --note "who it is"
```

Then their owner invites the bot and runs `/proxychat slot add`.

## 12. Keeping it updated

Weekly, or when Dependabot or CI flags something:

```bash
cd /opt/proxychat && sudo git pull
cd deploy && sudo docker compose build --pull && sudo docker compose up -d
```

If the lock files changed, the images are rebuilt from them automatically.

---

**Not yet checked on a real Pop!_OS server:** this guide and these files were written and checked on Windows (syntax and the image build) only. Three things to watch the first time:
- the `ts` container running with a read-only filesystem;
- `TS_AUTHKEY` reading from a file (`file:` prefix);
- the ownership numbers from `S`.

If `ts` fails with "read-only file system", remove `read_only: true` for `ts` only, in `docker-compose.yml`. [RUNBOOK.md](RUNBOOK.md) covers day-to-day operation.
