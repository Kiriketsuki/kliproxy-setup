#!/usr/bin/env bash
# Restarts the proxy of a pool and the router to clear cached cooldowns and seat
# assignments.
#
# Usage: reroute.sh [pool ...]
#   pool   One or more pool names from HUB_POOLS. Default: work.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
. "$HERE/hub.env"

[ $# -gt 0 ] || set -- work
containers=()
for pool in "$@"; do
  case " $HUB_POOLS " in
    *" $pool:"*) containers+=("claude-hub-$pool") ;;
    *) echo "reroute: unknown pool '$pool'. Use a pool from HUB_POOLS: $HUB_POOLS" >&2; exit 2 ;;
  esac
done

echo "reroute: restart the proxy of $* and the router on $HUB_SSH"
if ! ssh -o ConnectTimeout=10 "$HUB_SSH" \
  "sudo -n $HUB_DOCKER restart ${containers[*]} claude-hub-router"; then
  echo "reroute: restart failed. Check SSH access and Docker on $HUB_SSH." >&2
  exit 1
fi

echo "reroute: restart complete. In each paused session, press Esc if it is waiting, then enter continue."
