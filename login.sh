#!/usr/bin/env bash
# Signs Claude accounts into the hub pools. Run it in your own terminal.
# It stays open, so you can add several accounts in one run.
#
# Usage: login.sh [pool] [--host SSH_TARGET] [--dir HUB_DIR]
#   pool         personal or work. The script asks when you give none.
#   --host       SSH alias or user@hostname of the hub host. Default: HUB_SSH in hub.env.
#   --dir        Hub directory on that host. Default: HUB_DIR in hub.env.
#
# SSH asks for a password one time if the host needs one. Each later call shares
# that connection.
#
# The script uses no SSH tunnel, because a hub host can refuse port forwards.
# The browser fails on the localhost:54545 page. You paste that address here, and the
# script sends it to the login container on the hub host.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hub.env
[ -f "$HERE/hub.env" ] || { echo "Copy hub.env.example to hub.env and set your values." >&2; exit 2; }
. "$HERE/hub.env"

usage() { sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

pool=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HUB_SSH="${2:?--host needs a value}"; shift 2 ;;
    --dir) HUB_DIR="${2:?--dir needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option '$1'." >&2; usage >&2; exit 2 ;;
    *) pool="$1"; shift ;;
  esac
done

pool_names=()
for entry in $HUB_POOLS; do pool_names+=("${entry%%:*}"); done
is_pool() { local p; for p in "${pool_names[@]}"; do [ "$p" = "$1" ] && return 0; done; return 1; }
if [ -n "$pool" ] && ! is_pool "$pool"; then
  echo "Unknown pool '$pool'. Use one of: ${pool_names[*]}" >&2
  exit 2
fi

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; D=""; G=""; R=""; N=""; fi

# One shared connection serves each call. LogLevel hides the key exchange warning.
sock="${XDG_RUNTIME_DIR:-/tmp}/claude-hub-login-$$.sock"
ssh_opts=(-o LogLevel=ERROR -o ControlPath="$sock")
hssh() { ssh "${ssh_opts[@]}" "$@"; }

name="claude-hub-login"
RUN=""
cleanup() {
  [ -n "$RUN" ] && hssh -n "$HUB_SSH" "$RUN rm -f $name" >/dev/null 2>&1
  hssh -O exit "$HUB_SSH" >/dev/null 2>&1
  return 0
}
trap cleanup EXIT

printf '%sClaude hub login%s  host: %s%s%s\n\n' "$B" "$N" "$B" "$HUB_SSH" "$N"

# This call opens the shared connection. SSH asks for a password here if it needs one.
if ! ssh "${ssh_opts[@]}" -o ControlMaster=yes -o ControlPersist=900 -fN "$HUB_SSH"; then
  echo "${R}SSH could not connect to '$HUB_SSH'.${N}" >&2
  exit 1
fi

# Finds docker on the host and whether it needs sudo. DSM keeps docker in /usr/local/bin.
probe="$(hssh -n "$HUB_SSH" '
  d=""
  for c in docker /usr/local/bin/docker; do
    if command -v "$c" >/dev/null 2>&1; then d="$c"; break; fi
  done
  if [ -z "$d" ]; then echo "ERR no-docker"
  elif "$d" ps >/dev/null 2>&1; then echo "OK $d"
  elif sudo -n "$d" ps >/dev/null 2>&1; then echo "OK sudo -n $d"
  else echo "ERR no-access"; fi')"
case "$probe" in
  "OK "*) RUN="${probe#OK }" ;;
  "ERR no-docker") echo "${R}The host '$HUB_SSH' has no docker command.${N}" >&2; exit 1 ;;
  *) echo "${R}The SSH user cannot run docker on '$HUB_SSH'.${N}" >&2
     echo "Add the user to the docker group, or give the user sudo without a password." >&2
     exit 1 ;;
esac

if ! hssh -n "$HUB_SSH" "ls '$HUB_DIR/compose.yaml' >/dev/null 2>&1 || sudo -n ls '$HUB_DIR/compose.yaml' >/dev/null 2>&1"; then
  echo "${R}No hub found in '$HUB_DIR' on '$HUB_SSH'. Run deploy.sh first, or pass --dir.${N}" >&2
  exit 1
fi

accounts() {
  hssh -n "$HUB_SSH" "ls '$HUB_DIR/$1/auth' 2>/dev/null || sudo -n ls '$HUB_DIR/$1/auth'" \
    | sed -nE 's/^claude-[0-9a-f]+-(.+)\.json$/\1/p' | sort
}

show_pools() {
  local p
  for p in "${pool_names[@]}"; do
    printf '%s%s%s\n' "$B" "$p" "$N"
    accounts "$p" | sed 's/^/  /'
  done
}

# Copies text to the clipboard of the terminal in front of the user, through SSH too.
# OSC 52 does this. tmux needs the sequence in a pass-through wrapper.
copy_to_clipboard() {
  local data
  data="$(printf '%s' "$1" | base64 | tr -d '\n')"
  if [ -n "${TMUX:-}" ]; then printf '\033Ptmux;\033\033]52;c;%s\a\033\\' "$data"
  else printf '\033]52;c;%s\a' "$data"; fi
}

choose_pool() {
  local i key
  while :; do
    i=1
    for p in "${pool_names[@]}"; do printf '%s%d%s %s   ' "$B" "$i" "$N" "$p"; i=$((i + 1)); done
    printf '%sq%s quit\n> ' "$B" "$N"
    IFS= read -rsn1 key || exit 1
    printf '\r'
    case "$key" in
      q|Q) exit 0 ;;
      [1-9]) if [ "$key" -le "${#pool_names[@]}" ]; then pool="${pool_names[$((key - 1))]}"; return; fi ;;
    esac
  done
}

# Runs one login in the current pool. Returns 0 when the pool gets a new account.
login_once() {
  local url="" before after added callback="" key rest path status

  printf '\n%sPool: %s%s\n' "$B" "$pool" "$N"
  printf '%sStarting the login process ...%s\n' "$D" "$N"
  hssh -n "$HUB_SSH" "$RUN rm -f $name" >/dev/null 2>&1 || true
  before="$(accounts "$pool")"

  # A one-shot container shares the pool config and auth directory.
  # It publishes the callback port on the hub host loopback only.
  hssh -n "$HUB_SSH" "$RUN run -dit --name $name \
    -p 127.0.0.1:54545:54545 \
    -v '$HUB_DIR/$pool/config.yaml:/CLIProxyAPI/config.yaml' \
    -v '$HUB_DIR/$pool/auth:/root/.cli-proxy-api' \
    '$HUB_IMAGE' ./CLIProxyAPI -claude-login -no-browser" >/dev/null

  # The login process needs about 8 seconds before it prints the URL.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    url="$(hssh -n "$HUB_SSH" "$RUN logs $name 2>&1" | tr -d '\r' \
      | grep -Eo 'https://claude\.ai/[^[:space:]]+' | head -1 || true)"
    [ -n "$url" ] && break
  done
  if [ -z "$url" ]; then
    echo "${R}The login process printed no URL in 30 seconds.${N}" >&2
    return 1
  fi

  cat <<EOF

${B}1.${N} Open this URL in the browser of the account that you want to add:

${D}${url}${N}

${B}2.${N} Approve. The browser then fails on a localhost:54545 page. That is correct.
${B}3.${N} Copy that address from the address bar and paste it below.

EOF

  # The first key decides. "c" copies the URL. Any other key starts the pasted address.
  while :; do
    printf '%sc%s copy URL   %spaste%s callback address + Enter   %sq%s cancel\n> ' "$B" "$N" "$B" "$N" "$B" "$N"
    IFS= read -rsn1 key || exit 1
    case "$key" in
      c|C) copy_to_clipboard "$url"; printf '\r%sURL copied to the clipboard.%s\n\n' "$G" "$N" ;;
      q|Q) printf '\rCancelled. Nothing was sent.\n'; return 1 ;;
      "") printf '\r' ;;
      *) printf '%s' "$key"; IFS= read -r rest || true; callback="$key$rest"; break ;;
    esac
  done

  case "$callback" in
    http://localhost:54545/*|http://127.0.0.1:54545/*) ;;
    *) echo "${R}The address must start with http://localhost:54545/. Nothing was sent.${N}"; return 1 ;;
  esac
  path="${callback#http://*:54545}"

  # The address goes through stdin, so no shell on the way reads its special characters.
  status="$(printf '%s\n' "http://127.0.0.1:54545$path" | hssh "$HUB_SSH" \
    'read -r u && curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$u"' || true)"
  sleep 4

  after="$(accounts "$pool")"
  added="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed '/^$/d')"
  echo
  if [ -n "$added" ]; then
    printf '%sAdded to %s:%s %s\n' "$G" "$pool" "$N" "$added"
    return 0
  fi
  printf '%sNo new account. Callback status: %s.%s\n' "$R" "${status:-none}" "$N"
  echo "The account is possibly in the pool already, or the code stopped working."
  return 1
}

show_pools
echo
[ -z "$pool" ] && { echo "Select a pool:"; choose_pool; }

while :; do
  login_once || true
  hssh -n "$HUB_SSH" "$RUN rm -f $name" >/dev/null 2>&1 || true

  echo
  show_pools
  echo
  printf '%sa%s add another to %s   %sp%s other pool   %sq%s quit\n> ' "$B" "$N" "$pool" "$B" "$N" "$B" "$N"
  while :; do
    IFS= read -rsn1 key || exit 0
    case "$key" in
      a|A) printf '\r'; break ;;
      p|P) printf '\r'; choose_pool; break ;;
      q|Q) printf '\r'; exit 0 ;;
    esac
  done
done
