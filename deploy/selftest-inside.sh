#!/usr/bin/env bash
# Check the containers' network isolation from the server itself (implementation §3g).
#   deploy/selftest-inside.sh [<a tailnet device name to try to reach, e.g. your PC>]
# Run from the deploy folder's parent or anywhere: it cds to deploy/.
set -uo pipefail
cd "$(dirname "$0")"
PEER="${1:-}"
pass=0; fail=0
# connect <service> <host> <port>: exit 0 if a TCP connection succeeds within 5 s
connect() {
  docker compose exec -T "$1" python -c "import socket,sys; s=socket.create_connection(('$2',$3),timeout=5); s.close()" >/dev/null 2>&1
}
expect() {  # expect <works|blocked> <description> <service> <host> <port>
  local want="$1" what="$2"; shift 2
  if connect "$@"; then got=works; else got=blocked; fi
  if [ "$got" = "$want" ]; then echo "  ok    $what: $got"; pass=$((pass + 1))
  else echo "  FAIL  $what: expected $want, got $got"; fail=$((fail + 1)); fi
}

gateway="$(ip route | awk '/^default/ {print $3; exit}')"
echo "LAN gateway: ${gateway:-unknown}"

expect blocked "api -> internet (1.1.1.1:443)"        api 1.1.1.1 443
expect blocked "api -> discord.com"                   api discord.com 443
[ -n "$gateway" ] && expect blocked "api -> your router ($gateway)" api "$gateway" 80
expect works   "bot -> discord.com"                   bot discord.com 443
[ -n "$gateway" ] && expect blocked "bot -> your router ($gateway)" bot "$gateway" 80
expect blocked "bot -> 192.168.1.1"                   bot 192.168.1.1 80
expect blocked "bot -> api (no shared network)"       bot api 8080

if docker compose exec -T ts tailscale status >/dev/null 2>&1; then
  echo "  ok    tailscale sidecar is logged in"; pass=$((pass + 1))
  docker compose exec -T ts tailscale funnel status 2>/dev/null | sed 's/^/        /'
  if [ -n "$PEER" ]; then
    if docker compose exec -T ts tailscale ping -c 1 --timeout 5s "$PEER" >/dev/null 2>&1; then
      echo "  FAIL  the sidecar can reach $PEER: check your tailnet ACLs"; fail=$((fail + 1))
    else
      echo "  ok    the sidecar can't reach $PEER"; pass=$((pass + 1))
    fi
  fi
else
  echo "  FAIL  tailscale sidecar isn't logged in (docker compose logs ts)"; fail=$((fail + 1))
fi

for svc in api bot; do
  user="$(docker compose exec -T "$svc" id -u 2>/dev/null | tr -d '\r')"
  if [ "$user" = 10001 ]; then echo "  ok    $svc runs as uid 10001"; pass=$((pass + 1))
  else echo "  FAIL  $svc runs as uid '$user'"; fail=$((fail + 1)); fi
done

echo "$pass passed, $fail failed."
[ "$fail" -eq 0 ]
