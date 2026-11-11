#!/usr/bin/env bash
# Deploys the hub to the host in hub.env, starts it, and opens it on the tailnet.
# Safe to run again. It keeps existing configs, keys and logins.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

pools=()
for entry in $HUB_POOLS; do pools+=("${entry%%:*}"); done

echo "deploy: copy files to $HUB_SSH:$HUB_DIR"
# DSM refuses rsync over SSH for a normal user, so tar carries the files.
tar -C "$HERE/remote" --exclude=__pycache__ -cf - . | ssh "$HUB_SSH" "sudo -n mkdir -p '$HUB_DIR' \
  && sudo -n tar -C '$HUB_DIR' --no-same-owner -xf - \
  && sudo -n sh '$HUB_DIR/init-remote.sh' ${pools[*]}"

echo "deploy: start containers"
ssh "$HUB_SSH" "cd / && sudo -n env HUB_IMAGE='$HUB_IMAGE' $HUB_DOCKER compose \
  --project-directory '$HUB_DIR' -f '$HUB_DIR/compose.yaml' up -d --force-recreate router \
  && sudo -n env HUB_IMAGE='$HUB_IMAGE' $HUB_DOCKER compose --project-directory '$HUB_DIR' -f '$HUB_DIR/compose.yaml' up -d"

echo "deploy: open the ports on the tailnet"
ports=("$HUB_STATUS_PORT")
for entry in $HUB_POOLS; do ports+=("${entry##*:}"); done
for port in "${ports[@]}"; do
  ssh "$HUB_SSH" "sudo -n $HUB_TAILSCALE serve --bg --https=$port http://127.0.0.1:$port" >/dev/null
done
ssh "$HUB_SSH" "sudo -n $HUB_TAILSCALE serve status"

"$HERE/client-setup.sh"
