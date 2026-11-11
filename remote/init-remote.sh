#!/bin/sh
# Runs on the hub host as root, from the hub directory.
# Creates each pool directory and its keys one time. It never prints a key.
set -eu
umask 077

HUB_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$HUB_DIR"
chown root:root "$HUB_DIR"
chmod 700 "$HUB_DIR"
# Docker on DSM does not create a missing bind-mount directory.
mkdir -p keys router-state

for pool in "$@"; do
  mkdir -p "$pool/auth" "$pool/logs"
  if [ -f "$pool/config.yaml" ]; then
    # Adds a setting that a newer template has, with no change to the keys.
    if ! grep -q 'passthrough-headers' "$pool/config.yaml"; then
      printf 'requests:\n  passthrough-headers: true\n' >> "$pool/config.yaml"
      echo "init: $pool config exists, passthrough-headers added"
    else
      echo "init: $pool config exists, kept"
    fi
    continue
  fi
  access="sk-hub-$(openssl rand -hex 24)"
  mgmt="$(openssl rand -hex 24)"
  printf '%s\n' "$access" > "keys/$pool.access"
  printf '%s\n' "$mgmt" > "keys/$pool.mgmt"
  sed -e "s/__ACCESS_KEY__/$access/" -e "s/__MGMT_KEY__/$mgmt/" \
    config.template.yaml > "$pool/config.yaml"
  echo "init: $pool config and keys created"
done
