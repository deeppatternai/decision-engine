#!/usr/bin/env bash

set -u

PROGRAM_NAME="dp-doctor"
if [ -z "${HOME:-}" ]; then
  printf '%s: ERROR: the user home directory is unavailable\n' "$PROGRAM_NAME" >&2
  exit 1
fi
DEEPPATTERN_ROOT="$HOME/.deeppattern"
MANAGED_ROOT="$DEEPPATTERN_ROOT/decision-engine"
MANAGED_PYTHON_ROOT="$DEEPPATTERN_ROOT/de-python"
MANAGED_PYTHON="$MANAGED_PYTHON_ROOT/bin/python3"
MANAGED_PYTHON_MARKER="$MANAGED_PYTHON_ROOT/.deeppattern-python-environment"
DOCTOR_MODULE="$MANAGED_ROOT/installer/doctor.py"

usage() {
  printf 'Usage: bash "$HOME/.deeppattern/decision-engine/dp-doctor.sh" [--json]\n' >&2
}

fail() {
  printf '%s: ERROR: %s\n' "$PROGRAM_NAME" "$1" >&2
  exit 1
}

case "$#:${1:-}" in
  0:) ;;
  1:--json) ;;
  *) usage; exit 2 ;;
esac

for directory in "$DEEPPATTERN_ROOT" "$MANAGED_ROOT" "$MANAGED_PYTHON_ROOT" "$MANAGED_ROOT/installer"; do
  if [ ! -d "$directory" ]; then
    fail "the managed Decision Engine installation is incomplete; re-run the official installer, then retry Doctor"
  fi
  if [ -L "$directory" ]; then
    fail "$directory is not a trusted directory; preserve it and re-run the official installer"
  fi
done

if [ ! -f "$MANAGED_PYTHON_MARKER" ] || [ -L "$MANAGED_PYTHON_MARKER" ] \
    || [ ! -x "$MANAGED_PYTHON" ]; then
  fail "the private Deep Pattern Python environment is unavailable; re-run the official installer, then retry Doctor"
fi

if [ ! -f "$DOCTOR_MODULE" ] || [ -L "$DOCTOR_MODULE" ]; then
  fail "the installed Doctor module is unavailable or untrusted; re-run the official installer, then retry Doctor"
fi

# The entrypoint is diagnostic-only. Isolated Python plus an explicit managed
# module root prevents the caller's cwd and inherited Python/Git settings from
# selecting code or redirecting Doctor's read-only Git probes.
while IFS= read -r variable; do
  case "$variable" in
    PYTHON*|GIT_*|DE_ENDPOINT|DE_ACTIVATION_SECRET|DE_API_KEY|DE_ACCESS_TOKEN|DE_DEVICE_TOKEN)
      unset "$variable"
      ;;
  esac
done < <(compgen -e)
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

prefix_probe='import os, sys; raise SystemExit(0 if os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1]) else 1)'
if ! "$MANAGED_PYTHON" -I -X utf8 -c "$prefix_probe" "$MANAGED_PYTHON_ROOT" >/dev/null 2>&1; then
  fail "the private Deep Pattern Python environment is invalid; re-run the official installer, then retry Doctor"
fi

runner='import runpy, sys; root = sys.argv.pop(1); sys.path.insert(0, root); runpy.run_module("installer.doctor", run_name="__main__")'
exec "$MANAGED_PYTHON" -I -X utf8 -c "$runner" "$MANAGED_ROOT" "$@"
