# Deploying the central server (Pop!_OS)

**What you end up with:** three hardened containers on your home server. Only a Tailscale Funnel address is public; nothing is port-forwarded and your home IP isn't exposed. The design is in [../docs/SERVER_RELAY_DESIGN.md](../docs/SERVER_RELAY_DESIGN.md).

**Conventions:**
- Every value marked **(placeholder)** is listed in [../docs/DEPLOYMENT_PLACEHOLDERS.md](../docs/DEPLOYMENT_PLACEHOLDERS.md), with how to find it.
- `S` below means the start of Docker's remapped user range (step 2). The containers' user 10001 is host user **`S + 10001`**.
- All commands run on the server, from `/opt/proxychat` unless they say otherwise.

> ⚠️ **One bot at a time.** The server uses the same Discord token as your local bot. Never run both at once. The switch-over for your main server is M7.

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

1. **Access controls:** merge in [tailscale-acl-snippet.jsonc](tailscale-acl-snippet.jsonc). **First** replace the default allow-all rule with explicit rules for your own devices, if it's still there.
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

## 7. Start it

```bash
cd /opt/proxychat/deploy
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f
```

Expect a warning that `public_url` is still a placeholder, and that `owner_user_id` is if you left it. The bot logs in, and the `ts` container logs in to Tailscale.

The auth key is single-use, so it's spent now. You can also revoke it in the console; the node stays logged in through its saved state.

## 8. The public address

```bash
sudo docker compose exec ts tailscale funnel status
```

This shows `https://proxychat.<tailnet>.ts.net`. Put it in `config/server.toml` as `public_url` **(placeholder)**, then:

```bash
sudo docker compose restart bot
```

## 9. Self-tests

From a machine that is **not** on your tailnet, such as a phone hotspot or a PC with Tailscale off:

```bash
deploy/selftest-outside.sh https://proxychat.<tailnet>.ts.net
```

On the server:

```bash
sudo deploy/selftest-inside.sh <your-pc's-tailnet-name>
```

Both should end with `0 failed`. Re-run them after any change to these files.

## 10. Backups

1. **On your PC,** not the server: `age-keygen -o proxychat-backup.key`. Keep that file safe and offline; it's the only way to read backups. Copy the `age1…` public key it prints.
2. **On the server,** in `deploy/backup.env`, set `AGE_RECIPIENT` **(placeholder)** and optionally `BACKUP_TARGET` **(placeholder)**.
3. Run one backup now, then switch on the nightly timer:
   ```bash
   sudo /opt/proxychat/deploy/backup.sh
   sudo systemctl enable --now proxychat-backup.timer
   ```
4. **Test a restore once,** on your PC: `age -d -i proxychat-backup.key proxychat-<date>.db.age > restored.db`. Then open it with `sqlite3 restored.db ".tables"`.

## 11. Approve servers

Approving is only possible here, on the command line:

```bash
sudo docker compose exec bot python -m proxychat_server.admin_cli guild approve <GUILD_ID> --note "who it is"
```

Then the owner invites the bot and runs `/proxychat slot add`. Moving your main server over, with its channels and claims, is milestone M7: `admin_cli import-state`.

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
