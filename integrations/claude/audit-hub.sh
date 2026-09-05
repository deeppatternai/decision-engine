#!/usr/bin/env bash
set -euo pipefail

resolve_path() {
  local source="$1"
  local dir target

  if command -v perl >/dev/null 2>&1; then
    perl -MCwd=abs_path -e 'my $p = abs_path($ARGV[0]); die "resolve failed\n" unless defined $p; print $p' "$source" 2>/dev/null && return 0
  fi

  while [ -L "$source" ]; do
    dir="$(cd -P "$(dirname "$source")" && pwd)" || return 1
    target="$(readlink "$source")" || return 1
    case "$target" in
      /*) source="$target" ;;
      *) source="$dir/$target" ;;
    esac
  done

  dir="$(cd -P "$(dirname "$source")" && pwd)" || return 1
  printf "%s/%s" "$dir" "$(basename "$source")"
}

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
case "$SCRIPT_SOURCE" in
  */*) ;;
  *) SCRIPT_SOURCE="$(command -v "$SCRIPT_SOURCE" 2>/dev/null || printf "%s" "$SCRIPT_SOURCE")" ;;
esac
SCRIPT_LINK_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
SCRIPT_SOURCE="$SCRIPT_LINK_DIR/$(basename "$SCRIPT_SOURCE")"
SCRIPT_SOURCE="$(resolve_path "$SCRIPT_SOURCE")" || {
  echo "audit-hub: failed to resolve script path: $SCRIPT_SOURCE" >&2
  exit 1
}
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AUDIT_BIN="${AUDIT_HUB_AUDIT_BIN:-}"
AUDIT_HUB_USE_VALUE="${AUDIT_HUB_USE:-${AUDIT_HUB_CALLER:-}}"

if [ -z "$AUDIT_HUB_USE_VALUE" ]; then
  AUDIT_HUB_USE_VALUE="claude"
fi

case "$AUDIT_HUB_USE_VALUE" in
  codex|claude) ;;
  *)
    echo "audit-hub: AUDIT_HUB_USE must be codex or claude, got: $AUDIT_HUB_USE_VALUE" >&2
    exit 2
    ;;
esac

is_hub_audit() {
  "$1" --help 2>/dev/null | grep "Submit an audit" >/dev/null
}

find_hub_audit() {
  local candidate
  for candidate in "$SCRIPT_LINK_DIR/audit" "$HOME/.local/bin/audit" /opt/homebrew/bin/audit /usr/local/bin/audit; do
    if [ -x "$candidate" ] && is_hub_audit "$candidate"; then
      printf "%s" "$candidate"
      return 0
    fi
  done
  return 1
}

if [ -z "$AUDIT_BIN" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/audit" ]; then
    AUDIT_BIN="$REPO_ROOT/.venv/bin/audit"
  elif AUDIT_BIN="$(find_hub_audit)"; then
    :
  else
    AUDIT_BIN="python3 -m client.runner"
    cd "$REPO_ROOT"
  fi
fi

exec $AUDIT_BIN submit \
  --use "$AUDIT_HUB_USE_VALUE" \
  --audit-mode "${AUDIT_HUB_AUDIT_MODE:-triple}" \
  --tier "${AUDIT_HUB_TIER:-reasoning}" \
  --wait \
  "$@"
