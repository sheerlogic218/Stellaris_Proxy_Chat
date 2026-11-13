#!/usr/bin/env bash
# Probe the public API like a stranger would (design §2, implementation §3g).
# Run from a machine that is NOT on your tailnet (or with Tailscale off):
#   deploy/selftest-outside.sh https://proxychat.<tailnet>.ts.net
# Everything should fail cheaply. Needs curl.
set -uo pipefail
URL="${1:?usage: selftest-outside.sh https://<public_url>}"
URL="${URL%/}"
pass=0; fail=0
check() {  # check <description> <expected status> <curl args...>
  local what="$1" want="$2"; shift 2
  local got
  got="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@")"
  if [ "$got" = "$want" ]; then echo "  ok    $what ($got)"; pass=$((pass + 1))
  else echo "  FAIL  $what: expected $want, got $got"; fail=$((fail + 1)); fi
}

echo "Probing $URL"
check "unknown path"                404 -X POST "$URL/admin"
check "GET on a real path"          404 "$URL/v1/events"
check "unsigned events"             401 -X POST -H 'Content-Type: application/json' -d '{}' "$URL/v1/events"
check "made-up relay key"           401 -X POST -H 'Content-Type: application/json' -H 'X-PC-Key: k_0000' -d '{}' "$URL/v1/heartbeat"
check "pairing with a bogus code"   401 -X POST -H 'Content-Type: application/json' \
      -d '{"code":"AAAA-AAAA-AAAA","public_key":"AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE","label":"probe","relay_version":"9.9.9"}' "$URL/v1/pair"
big="$(head -c 20000 /dev/zero | tr '\0' 'a')"
check "oversized body"              413 -X POST -H 'Content-Type: application/json' --data-binary "{\"x\":\"$big\"}" "$URL/v1/pair"

if curl -s -D - -o /dev/null --max-time 15 "$URL/" | grep -qi '^server:'; then
  echo "  FAIL  the response has a Server header"; fail=$((fail + 1))
else
  echo "  ok    no Server header"; pass=$((pass + 1))
fi

echo "  ...  40 quick unauthenticated requests (expect some 429s)"
limited=0
for _ in $(seq 40); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST -H 'Content-Type: application/json' -d '{}' "$URL/v1/events")"
  [ "$code" = 429 ] && limited=$((limited + 1))
done
if [ "$limited" -gt 0 ]; then echo "  ok    rate limited ($limited of 40 got 429)"; pass=$((pass + 1))
else echo "  FAIL  no rate limiting seen"; fail=$((fail + 1)); fi

echo "$pass passed, $fail failed. (The probes count as failed sign-ins: expect an alert DM.)"
[ "$fail" -eq 0 ]
