#!/usr/bin/env bash
# Shows the router view of each pool: priority, state, weekly use and 5-hour use.
# It reads the status endpoint of the hub with the access key of the pool.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

for entry in $HUB_POOLS; do
  pool="${entry%%:*}"
  if ! body="$(curl -sf -m 8 -H "X-Api-Key: $(<"$HUB_KEY_DIR/$pool.key")" "$(<"$HUB_KEY_DIR/$pool.status-url")")"; then
    echo "status: $pool FAIL. The status endpoint gave no answer. Run client-setup.sh, then deploy.sh."
    continue
  fi
  printf '%s' "$body" | jq -r '
    def pct: if . == null then "-" else "\(. * 100 | round)%" end;
    def hrs: if . == null then "-" else "\(.)h" end;
    "\(.pool)  next seat: \(.next_seat // "-")  (updated \(.updated))",
    (["  seat", "priority", "state", "week used", "week reset", "5h used", "5h reset"] | @tsv),
    (.seats[] | ["  " + .seat, .priority, .state, (.week_used | pct), (.week_hours_left | hrs),
      (.five_hour_used | pct), (.five_hour_hours_left | hrs)] | @tsv)' | column -t -s $'\t'
  echo
done
