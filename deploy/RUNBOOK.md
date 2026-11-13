# Runbook

All commands run on the server, in `/opt/proxychat/deploy`, with `sudo`. To shorten them:

```bash
alias pc='sudo docker compose exec bot python -m proxychat_server.admin_cli'
```

## Everyday

| Want to | Run |
|---|---|
| See the overall state | `pc status` |
| See recent actions | `pc audit -n 50` |
| Approve a Discord server | `pc guild approve <GUILD_ID> --note "who"` |
| List servers / slots / relays | `pc guild list` · `pc slot list` · `pc relay list` |
| Container logs | `sudo docker compose logs -f bot` (or `api`, `ts`) |
| Restart after a config change | `sudo docker compose restart bot api` |
| Check the bot is alive | `sudo docker compose ps` (health column), `pc status` |

## When something goes wrong

| Situation | Do this |
|---|---|
| **A relay key leaked, or a volunteer left** | In Discord: `/proxychat relay revoke`. Or here: `pc relay list`, then `pc relay revoke <key_id>`. |
| **Every relay key may be compromised** | `pc relay revoke-all`. Every host pairs again with a new `/proxychat host` code. |
| **A server is misbehaving** | `pc guild freeze <GUILD_ID>`: the bot stays but does nothing there. Undo with `pc guild unfreeze <GUILD_ID>`. |
| **Remove a server for good** | `pc guild remove <GUILD_ID>`: relays revoked straight away, data deleted after 7 days, and the bot leaves on its next check. |
| **Suspected API compromise** | 1. `sudo docker compose exec ts tailscale funnel off`: the API is off the internet at once. 2. `sudo docker compose stop api`. 3. Inspect: `sudo docker compose logs api`, `pc audit -n 200`. The bot keeps working, with no new events. 4. Once it's clear: `pc relay revoke-all`, `sudo docker compose up -d --build`, then `sudo docker compose restart ts` (it reapplies `ts-serve.json`, which turns Funnel back on). |
| **Suspected Discord token leak** | 1. Discord Developer Portal → your app → **Bot** → **Reset Token**. 2. Write it with no echo: `read -rs -p "Token: " T && printf '%s' "$T" \| sudo tee secrets/discord_token >/dev/null; unset T`. 3. `sudo docker compose up -d bot`. |
| **Maintenance** | `pc maintenance on`: the API answers 503 and relays back off. `pc maintenance off` when done. |
| **Bot looks stuck** | `sudo docker compose restart bot`. Queued events wait in the database and are applied after the restart. |
| **Too many alert DMs about failed sign-ins** | Someone is probing. Rate limits already contain it. Check `sudo docker compose logs api`. If it persists, turn Funnel off for a while, as above. |

## Backups and restore

| Want to | Run |
|---|---|
| Back up right now | `sudo ./backup.sh` |
| When the last backups ran | `systemctl list-timers proxychat-backup.timer`, `ls -lt backups/` |
| **Restore** | 1. Decrypt on your PC: `age -d -i proxychat-backup.key proxychat-<date>.db.age > proxychat.db`. 2. Copy it to the server. 3. `sudo docker compose stop bot api`. 4. Back up the current file: `sudo mv data/proxychat.db data/proxychat.db.broken`, and also remove `data/proxychat.db-wal` and `data/proxychat.db-shm` if they exist. 5. `sudo cp proxychat.db data/` and `sudo chown <S+10001>:<S+10001> data/proxychat.db`. 6. `sudo docker compose up -d`. |

## Turning it all off

```bash
sudo docker compose exec ts tailscale funnel off   # off the internet, straight away
sudo docker compose down                           # everything stops; data stays in ./data
```

Players' channels stay in Discord while the bot is off. It picks up where it left off when started again.
