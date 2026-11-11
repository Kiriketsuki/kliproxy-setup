#!/usr/bin/env bash
# Checks each pool from this client through the tailnet. It never prints a key.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

rc=0
for entry in $HUB_POOLS; do
  pool="${entry%%:*}"
  port="${entry##*:}"
  key_file="$HUB_KEY_DIR/$pool.key"
  if [ ! -r "$key_file" ]; then
    echo "verify: $pool FAIL. Key file $key_file is missing. Run deploy.sh."
    rc=1
    continue
  fi
  body="$(curl -s --max-time 20 "https://$HUB_FQDN:$port/v1/models" \
    -H "Authorization: Bearer $(<"$key_file")" || true)"
  count="$(printf '%s' "$body" | jq -r '.data | length' 2>/dev/null || echo "")"
  if [ -n "$count" ]; then
    echo "verify: $pool OK on port $port. Models: $count. Zero models means no account is signed in."
  else
    echo "verify: $pool FAIL on port $port. The hub gave no model list."
    rc=1
  fi
done
exit "$rc"
