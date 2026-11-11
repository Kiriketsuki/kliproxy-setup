#!/usr/bin/env bash
# Prepares this machine as a hub client. Run it on each client, and again after a hub move.
# It fetches the access keys and records the hub URLs. It never prints a key.
#
# A hub session keeps the normal Claude config dir and its claude.ai login. The claude
# function in ~/.zshrc adds the hub URL and sends the key in the X-Api-Key header.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

mkdir -p "$HUB_KEY_DIR"
chmod 700 "$HUB_KEY_DIR"

for entry in $HUB_POOLS; do
  pool="${entry%%:*}"
  port="${entry##*:}"
  ( umask 077
    ssh -o LogLevel=ERROR "$HUB_SSH" "sudo -n cat '$HUB_DIR/keys/$pool.access'" > "$HUB_KEY_DIR/$pool.key"
    printf 'https://%s:%s\n' "$HUB_FQDN" "$port" > "$HUB_KEY_DIR/$pool.url"
    printf 'https://%s:%s/status/%s\n' "$HUB_FQDN" "$HUB_STATUS_PORT" "$pool" > "$HUB_KEY_DIR/$pool.status-url" )
  echo "client: pool $pool ready. URL and key in $HUB_KEY_DIR"
done

"$HERE/verify.sh"
