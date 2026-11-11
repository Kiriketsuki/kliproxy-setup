# Sends each Claude Code session through the hub pool that fits the current directory.
# Source this file from ~/.zshrc after you run client-setup.sh.
#
# Settings, all optional:
#   CLAUDE_HUB_WORK_PATH   A path pattern. A directory that matches it uses the work pool.
#                          Example: "*/work/*". Empty means that each directory uses
#                          the personal pool.
#   CLAUDE_HUB_WORK_DIR    Config dir of the work account, if it differs from ~/.claude.
#   CLAUDE_POOL            Forces a pool for one session.
#   CLAUDE_DIRECT=1        Skips the hub for one session and uses the direct login.
#
# A hub session keeps the normal config dir and its claude.ai login. Artifacts and
# connectors need that login. The hub key goes in the X-Api-Key header, because
# ANTHROPIC_AUTH_TOKEN replaces the claude.ai login. Only model requests go to the hub.
# CLAUDE_HUB_POOL tells a statusline which pool the session uses.

unalias claude 2>/dev/null
function claude {
  local pool="personal" cfg="${HUB_KEY_DIR:-$HOME/.config/cliproxy}"
  local -a dir=() hub=()
  if [[ -n "$CLAUDE_HUB_WORK_PATH" && "$PWD" == ${~CLAUDE_HUB_WORK_PATH} ]]; then
    pool="work"
  fi
  [[ -z "$CLAUDE_DIRECT" && -n "$CLAUDE_POOL" ]] && pool="$CLAUDE_POOL"
  [[ "$pool" == "work" && -n "$CLAUDE_HUB_WORK_DIR" ]] && dir=(CLAUDE_CONFIG_DIR="$CLAUDE_HUB_WORK_DIR")
  if [[ -z "$CLAUDE_DIRECT" ]]; then
    if [[ ! -r "$cfg/$pool.key" || ! -r "$cfg/$pool.url" ]]; then
      echo "claude: hub pool '$pool' is not set up. This session uses the direct login." >&2
    elif ! curl -sf -m 3 -o /dev/null -H "X-Api-Key: $(<"$cfg/$pool.key")" \
        "$(<"$cfg/$pool.url")/v1/models"; then
      echo "claude: the hub does not answer. This session uses the direct login." >&2
    else
      hub=(CLAUDE_HUB_POOL="$pool" ANTHROPIC_BASE_URL="$(<"$cfg/$pool.url")"
        ANTHROPIC_CUSTOM_HEADERS="X-Api-Key: $(<"$cfg/$pool.key")"
        ENABLE_PROMPT_CACHING_1H=1)
    fi
  fi
  env "${dir[@]}" "${hub[@]}" claude "$@"
}
