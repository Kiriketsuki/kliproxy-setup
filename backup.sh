#!/usr/bin/env bash
# Copies the hub state from the host to this machine as one encrypted archive.
# The archive holds the configs, the keys and the account logins. It skips the logs.
# Restore: gpg --batch --passphrase-file <pass file> -d <archive> | sudo tar -C <hub dir> -xzf -
#
# A restored login can be out of date. The proxy renews each token, and a renewal makes
# the older copy invalid. Sign the account in again if a restored login fails.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

BACKUP_DIR="${HUB_BACKUP_DIR:-$HOME/.local/share/cliproxy-hub-backups}"
PASS_FILE="$HUB_KEY_DIR/backup.pass"
KEEP=14

umask 077
mkdir -p "$BACKUP_DIR" "$HUB_KEY_DIR"
if [ ! -s "$PASS_FILE" ]; then
  head -c 32 /dev/urandom | base64 > "$PASS_FILE"
  echo "backup: created the passphrase file $PASS_FILE. Store a copy in your password manager."
fi

out="$BACKUP_DIR/claude-hub-$(date +%Y%m%d-%H%M%S).tar.gz.gpg"
ssh -o LogLevel=ERROR "$HUB_SSH" "sudo -n tar -C '$HUB_DIR' --exclude='*/logs' --exclude='router-state' -czf - ." \
  | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-file "$PASS_FILE" -o "$out"

# An archive under 1 kB means that the host sent no data.
size="$(stat -c %s "$out")"
if [ "$size" -lt 1024 ]; then
  rm -f "$out"
  echo "backup: FAIL. The host sent no data." >&2
  exit 1
fi

# Keeps the newest archives only.
ls -1t "$BACKUP_DIR"/claude-hub-*.tar.gz.gpg | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "backup: OK. $out ($size bytes). Archives kept: $(ls -1 "$BACKUP_DIR" | wc -l)."
