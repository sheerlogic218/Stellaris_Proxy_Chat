#!/usr/bin/env bash
# Nightly encrypted backup of the Proxy Chat database (design §3.5, implementation §3f).
#
#   1. a consistent copy, made by the bot container (SQLite's online backup)
#   2. encrypted with age to your public key; the private key never sits on this server
#   3. the plaintext copy is deleted; 14 encrypted copies are kept
#   4. optionally copied off this machine with rsync (e.g. to your PC over Tailscale)
#
# If anything fails, the operator gets a Discord DM (admin_cli alert).
# Settings: deploy/backup.env (from backup.env.example). Run by proxychat-backup.timer.
set -euo pipefail
cd "$(dirname "$0")"

AGE_RECIPIENT=""
BACKUP_TARGET=""
KEEP=14
[ -f backup.env ] && . ./backup.env

fail() {
  echo "Backup failed: $1" >&2
  docker compose exec -T bot python -m proxychat_server.admin_cli --db /data/proxychat.db \
    alert "Nightly backup failed: $1" >/dev/null 2>&1 || true
  exit 1
}
trap 'fail "unexpected error on line $LINENO"' ERR

case "$AGE_RECIPIENT" in age1*) ;; *) fail "AGE_RECIPIENT isn't set in deploy/backup.env" ;; esac
command -v age >/dev/null || fail "age isn't installed (sudo apt install age)"

stamp="$(date +%Y-%m-%d)"
plain="backup-$stamp.db"           # inside ./data, written by the container
mkdir -p backups
rm -f "data/$plain"
docker compose exec -T bot python -m proxychat_server.admin_cli --db /data/proxychat.db \
  backup "/data/$plain" >/dev/null || fail "the database copy didn't work"
age -r "$AGE_RECIPIENT" -o "backups/proxychat-$stamp.db.age" "data/$plain" || fail "encryption didn't work"
rm -f "data/$plain"
chmod 600 "backups/proxychat-$stamp.db.age"

# Keep the newest $KEEP
ls -1t backups/proxychat-*.db.age | tail -n +"$((KEEP + 1))" | xargs -r rm -f

if [ -n "$BACKUP_TARGET" ]; then
  rsync -a --delete backups/ "$BACKUP_TARGET" || fail "copying to $BACKUP_TARGET didn't work"
fi
echo "Backup done: backups/proxychat-$stamp.db.age"
