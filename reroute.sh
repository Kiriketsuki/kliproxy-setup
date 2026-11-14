#!/usr/bin/env bash
# Restarts the work proxy and router to clear cached cooldowns and seat assignments.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
. "$HERE/hub.env"

echo "reroute: restart the work proxy and router on $HUB_SSH"
if ! ssh -o ConnectTimeout=10 "$HUB_SSH" \
  "sudo -n $HUB_DOCKER restart claude-hub-work claude-hub-router"; then
  echo "reroute: restart failed. Check SSH access and Docker on $HUB_SSH." >&2
  exit 1
fi

echo "reroute: restart complete. In each paused Claude session, press Esc if it is waiting, then enter continue."
