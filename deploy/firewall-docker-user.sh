#!/usr/bin/env bash
# Keep the Proxy Chat containers off your LAN (design §3.3).
#
# Docker's forwarded traffic bypasses ufw, so this goes in Docker's own DOCKER-USER
# chain: anything leaving the ts_out or bot_out networks for a private, link-local
# or Tailscale (CGNAT) address is dropped. Replies to connections that came in are
# allowed. The api container has no route out at all (its network is internal).
#
# Idempotent: safe to run again. Run by proxychat-firewall.service at boot and
# whenever Docker restarts. Settings: deploy/firewall.env (from firewall.env.example).
set -euo pipefail
cd "$(dirname "$0")"

TS_OUT_SUBNET="172.30.1.0/24"
BOT_OUT_SUBNET="172.30.2.0/24"
BLOCKED="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16"
EXTRA_BLOCKED=""
[ -f firewall.env ] && . ./firewall.env

CHAIN=PROXYCHAT-EGRESS
iptables -w -N "$CHAIN" 2>/dev/null || iptables -w -F "$CHAIN"
iptables -w -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for dst in $BLOCKED $EXTRA_BLOCKED; do
  iptables -w -A "$CHAIN" -d "$dst" -j DROP
done
iptables -w -A "$CHAIN" -j RETURN

# DOCKER-USER exists once Docker has started
iptables -w -N DOCKER-USER 2>/dev/null || true
for src in "$TS_OUT_SUBNET" "$BOT_OUT_SUBNET"; do
  iptables -w -C DOCKER-USER -s "$src" -j "$CHAIN" 2>/dev/null \
    || iptables -w -I DOCKER-USER 1 -s "$src" -j "$CHAIN"
done

# IPv6: Docker's IPv6 is off unless you turned it on; block the private ranges anyway
if command -v ip6tables >/dev/null && ip6tables -w -L DOCKER-USER >/dev/null 2>&1; then
  ip6tables -w -N "$CHAIN" 2>/dev/null || ip6tables -w -F "$CHAIN"
  ip6tables -w -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  for dst in fc00::/7 fe80::/10; do ip6tables -w -A "$CHAIN" -d "$dst" -j DROP; done
  ip6tables -w -A "$CHAIN" -j RETURN
fi

echo "Proxy Chat egress rules in place for $TS_OUT_SUBNET and $BOT_OUT_SUBNET"
