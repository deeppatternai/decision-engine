#!/usr/bin/env bash
#
# Deep Pattern (DP) first-time installer for macOS.
#
# This is the thin public entrypoint intended for:
#
#   curl -fsSL <trusted-install-url> | bash
#
# It deliberately accepts no arguments. The Decision Engine repository supplies
# the signed stable release installer; this wrapper only bootstraps that
# installer, then opens the existing masked activation dialog.
#
set -euo pipefail

DE_REPO="https://github.com/deeppatternai/decision-engine.git"
AQG_REPO="https://github.com/deeppatternai/agent-quality-gates.git"
AQG_REF="main"
PROGRAM_NAME="dp-install"
MANAGED_ROOT="$HOME/.deeppattern/decision-engine"
AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
CLAUDE_3P_ROOT="$HOME/Library/Application Support/Claude-3p"
CLAUDE_3P_CONFIG="$CLAUDE_3P_ROOT/claude_desktop_config.json"
WORKBUDDY_STANDARD_APP="/Applications/WorkBuddy.app"
WORKBUDDY_AI_APP="/Applications/WorkBuddy AI.app"
WORKBUDDY_AI_ROOT="$HOME/.workbuddy-ai"
EXIT_USAGE=2
EXIT_BLOCKED=3
EXIT_PARTIAL=4

fail() {
  printf '%s: ERROR: %s\n' "$PROGRAM_NAME" "$*" >&2
  exit "$EXIT_USAGE"
}

blocked() {
  printf '%s: BLOCKED: %s\n' "$PROGRAM_NAME" "$*" >&2
  exit "$EXIT_BLOCKED"
}

if [ "$#" -ne 0 ]; then
  fail "this first-time installer does not accept arguments; run it without arguments"
fi

if [ "$(uname -s)" != "Darwin" ]; then
  fail "this entrypoint currently supports macOS only"
fi

# curl | bash has a pipe on stdin. Read all interactive decisions from the
# controlling terminal so the pipe never becomes an accidental prompt source.
# The activation window is the only product decision made during this command.
if [ ! -r /dev/tty ] || [ ! -w /dev/tty ] || ! ( : </dev/tty ) 2>/dev/null; then
  fail "an interactive macOS Terminal is required for activation"
fi

tty_print() {
  printf '%s\n' "$*" >/dev/tty
}

clean_exec() {
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET -u PYTHONPATH \
    -u CLAUDE_DESKTOP_CONFIG -u CLAUDE_DESKTOP_3P_CONFIG -u WORKBUDDY_APP_ROOT \
    -u WORKBUDDY_CONFIG -u WORKBUDDY_SKILLS_DIR \
    -u BASH_ENV -u ENV "$@"
}

append_client_line() {
  local current="$1" client="$2"
  if [ -n "$current" ]; then
    printf '%s\n%s' "$current" "$client"
  else
    printf '%s' "$client"
  fi
}

line_list_contains() {
  local values="$1" expected="$2"
  printf '%s\n' "$values" | grep -Fxq "$expected"
}

comma_list_contains() {
  local values="$1" expected="$2"
  printf '%s' "$values" | tr ',' '\n' | grep -Fxq "$expected"
}

regular_app_has_bundle_id() {
  local app_path="$1" expected_bundle_id="$2" actual_bundle_id
  [ -d "$app_path" ] && [ ! -L "$app_path" ] || return 1
  [ -f "$app_path/Contents/Info.plist" ] \
    && [ ! -L "$app_path/Contents/Info.plist" ] || return 1
  actual_bundle_id="$(
    /usr/bin/plutil -extract CFBundleIdentifier raw -o - \
      "$app_path/Contents/Info.plist" 2>/dev/null || true
  )"
  [ "$actual_bundle_id" = "$expected_bundle_id" ]
}

workbuddy_variant="none"
if [ -x "$WORKBUDDY_STANDARD_APP/Contents/MacOS/Electron" ] \
    && [ ! -L "$WORKBUDDY_STANDARD_APP" ] \
    && [ ! -L "$WORKBUDDY_STANDARD_APP/Contents/MacOS/Electron" ]; then
  workbuddy_variant="standard"
elif [ -e "$WORKBUDDY_AI_APP" ] || [ -L "$WORKBUDDY_AI_APP" ]; then
  [ -d "$WORKBUDDY_AI_APP" ] && [ ! -L "$WORKBUDDY_AI_APP" ] \
    || blocked "$WORKBUDDY_AI_APP is not a regular application bundle; preserve it and stop"
  [ -f "$WORKBUDDY_AI_APP/Contents/MacOS/Electron" ] \
    && [ -x "$WORKBUDDY_AI_APP/Contents/MacOS/Electron" ] \
    && [ ! -L "$WORKBUDDY_AI_APP/Contents/MacOS/Electron" ] \
    || blocked "$WORKBUDDY_AI_APP has no trusted regular Electron executable"
  workbuddy_bundle_id="$(
    /usr/bin/plutil -extract CFBundleIdentifier raw -o - \
      "$WORKBUDDY_AI_APP/Contents/Info.plist" 2>/dev/null || true
  )"
  [ "$workbuddy_bundle_id" = "com.workbuddy.workbuddy-ai" ] \
    || blocked "$WORKBUDDY_AI_APP has unexpected product identity; no WorkBuddy configuration was changed"
  if [ -e "$WORKBUDDY_AI_ROOT" ] || [ -L "$WORKBUDDY_AI_ROOT" ]; then
    [ -d "$WORKBUDDY_AI_ROOT" ] && [ ! -L "$WORKBUDDY_AI_ROOT" ] \
      || blocked "$WORKBUDDY_AI_ROOT is not a regular WorkBuddy AI data directory; preserve it and stop"
  fi
  workbuddy_variant="ai"
fi

de_exec() {
  if [ "$workbuddy_variant" = "ai" ]; then
    clean_exec env \
      WORKBUDDY_APP_ROOT="$WORKBUDDY_AI_APP" \
      WORKBUDDY_CONFIG="$WORKBUDDY_AI_ROOT/mcp.json" \
      WORKBUDDY_SKILLS_DIR="$WORKBUDDY_AI_ROOT/skills" \
      "$@"
  else
    clean_exec "$@"
  fi
}

if ! command -v git >/dev/null 2>&1; then
  fail "Git 2.45 or newer is required; install or upgrade Git, then retry"
fi
GIT_BIN="$(command -v git)"
GIT_VERSION="$(clean_exec "$GIT_BIN" --version 2>/dev/null || true)"
if [[ ! "$GIT_VERSION" =~ git[[:space:]]version[[:space:]]([0-9]+)\.([0-9]+) ]]; then
  fail "could not verify the installed Git version"
fi
if (( BASH_REMATCH[1] < 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] < 45) )); then
  fail "Git 2.45 or newer is required"
fi

try_python() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  [ -x "$candidate" ] || return 1
  clean_exec "$candidate" -c \
    'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    </dev/null >/dev/null 2>&1 || return 1
  clean_exec "$candidate" -c 'import ssl, venv, tkinter' </dev/null >/dev/null 2>&1 || return 1
  clean_exec "$candidate" -m pip --version </dev/null >/dev/null 2>&1 || return 1
  PYTHON_BIN="$candidate"
  return 0
}

PYTHON_BIN=""
try_python "${DE_PYTHON:-}" \
  || try_python "$(command -v python3 2>/dev/null || true)" \
  || try_python "/opt/homebrew/bin/python3" \
  || try_python "/usr/local/bin/python3" \
  || try_python "/opt/anaconda3/bin/python" \
  || try_python "/opt/anaconda3/bin/python3" \
  || fail "Python 3.12 or newer with ssl, venv, tkinter, and pip is required; install it, then retry"
PYTHON_DIR="$(dirname "$PYTHON_BIN")"

managed_root_was_present=0
if [ -e "$MANAGED_ROOT" ] || [ -L "$MANAGED_ROOT" ]; then
  managed_root_was_present=1
fi

tmp_root="$(clean_exec mktemp -d /tmp/dp-install.XXXXXX)"
cleanup() {
  clean_exec rm -rf "$tmp_root"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

tty_print "Checking the official Decision Engine source and signed stable channel..."
if ! remote_refs="$(clean_exec env GIT_TERMINAL_PROMPT=0 "$GIT_BIN" ls-remote "$DE_REPO" refs/heads/main refs/heads/stable 2>/dev/null)"; then
  fail "could not read the Decision Engine product repository; check GitHub access and network"
fi
if ! printf '%s\n' "$remote_refs" | grep -Eq '[[:space:]]refs/heads/main$'; then
  fail "the official repository did not expose refs/heads/main"
fi
if ! printf '%s\n' "$remote_refs" | grep -Eq '[[:space:]]refs/heads/stable$'; then
  fail "the official repository did not expose refs/heads/stable"
fi

source_root="$tmp_root/decision-engine"
if ! clean_exec env GIT_TERMINAL_PROMPT=0 "$GIT_BIN" clone --depth 1 --branch main --single-branch \
    "$DE_REPO" "$source_root"; then
  fail "could not obtain the official Decision Engine installer source"
fi
actual_remote="$(clean_exec "$GIT_BIN" -C "$source_root" remote get-url origin 2>/dev/null || true)"
[ "$actual_remote" = "$DE_REPO" ] || fail "the downloaded installer source has an unexpected origin"
[ -f "$source_root/install.sh" ] || fail "the downloaded source has no install.sh"
[ -f "$source_root/AI_SETUP.md" ] || fail "the downloaded source has no AI_SETUP.md"
[ -z "$(clean_exec "$GIT_BIN" -C "$source_root" status --porcelain)" ] \
  || fail "the downloaded installer source is not clean"
source_sha="$(clean_exec "$GIT_BIN" -C "$source_root" rev-parse HEAD)"

# The bootstrap source and signed stable release must stay in the product
# repository. Development repositories are deliberately excluded here.
if ! grep -Fq \
    '("github", "https://github.com/deeppatternai/decision-engine.git")' \
    "$source_root/installer/managed_install.py"; then
  fail "the Decision Engine product repository does not declare the approved signed-stable remote contract; no product state was changed"
fi

run_source_python() {
  (
    cd "$source_root"
    de_exec "$PYTHON_BIN" "$@"
  )
}

managed_root_git_state_is_safe() {
  local status_output status_line
  if ! status_output="$(
    clean_exec "$GIT_BIN" -C "$MANAGED_ROOT" \
      status --porcelain=v1 --untracked-files=all
  )"; then
    return 1
  fi

  while IFS= read -r status_line; do
    [ -n "$status_line" ] || continue
    if [ "$status_line" = "?? stopper-ui.json" ]; then
      [ -f "$MANAGED_ROOT/stopper-ui.json" ] \
        && [ ! -L "$MANAGED_ROOT/stopper-ui.json" ] \
        || return 1
      continue
    fi
    return 1
  done <<<"$status_output"
}

validate_complete_managed_root() {
  [ ! -L "$MANAGED_ROOT" ] || return 1
  [ -d "$MANAGED_ROOT" ] || return 1
  [ -d "$MANAGED_ROOT/.git" ] || return 1
  [ -f "$MANAGED_ROOT/.managed-install.json" ] || return 1
  [ -f "$MANAGED_ROOT/.runtime/update-state.json" ] || return 1
  [ -f "$MANAGED_ROOT/.runtime/update-protocol.json" ] || return 1
  [ -f "$MANAGED_ROOT/config.json" ] || return 1
  [ -f "$MANAGED_ROOT/VERSION" ] || return 1
  [ -f "$MANAGED_ROOT/installer/permanent_setup.py" ] || return 1
  [ -f "$MANAGED_ROOT/installer/mcp_config.py" ] || return 1
  managed_root_git_state_is_safe || return 1
  run_source_python -c \
    'from pathlib import Path
import sys
from installer import managed_install, update_transaction, updater

root = Path(sys.argv[1])
reader = updater._GitReader(root)
identity = managed_install.validate_managed_identity(
    root, updater._read_remotes(reader)
)
state = updater._read_update_state(identity.canonical_root)
update_transaction._require_protocol_ready(identity.canonical_root)
_code, head_output = reader.run("head")
head = updater._single_commit(head_output, "managed HEAD")
version = (identity.canonical_root / "VERSION").read_text(encoding="utf-8").strip()
if head != state.last_release_commit or version != state.last_version:
    raise SystemExit(1)' \
    "$MANAGED_ROOT" </dev/null >/dev/null 2>&1
}

managed_activation_state() {
  run_source_python -c \
    'from installer import activate, config
data = config.load_json(config.de_config_path())
print("activated" if activate.is_permanently_activated(data) else "unactivated")' \
    </dev/null
}

managed_activation_recovery_pending() {
  run_source_python -c \
    'from installer import activate, config
path = activate.activation_recovery_marker_path(config.de_config_path())
raise SystemExit(0 if path.exists() else 1)' \
    </dev/null >/dev/null 2>&1
}

managed_config_digest() {
  clean_exec "$PYTHON_BIN" -c \
    'from pathlib import Path
import hashlib
import sys
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$MANAGED_ROOT/config.json" </dev/null
}

update_activated_managed_root() {
  (
    cd "$MANAGED_ROOT"
    de_exec "$PYTHON_BIN" - "$MANAGED_ROOT" <<'PY'
from pathlib import Path
import sys
import time

from installer import (
    launcher,
    update_coordination,
    update_transaction,
    updater,
)
from installer.config import ShellError
from installer.release_acquisition import load_trusted_release_keys

root = Path(sys.argv[1])
deadline = time.monotonic() + launcher.STARTUP_UPDATE_BUDGET_SECONDS
wait_budget = (
    launcher.STARTUP_UPDATE_BUDGET_SECONDS
    + launcher.STARTUP_LEADER_GRACE_SECONDS
)
try:
    with update_coordination.startup_update_gate(
        root, timeout_seconds=wait_budget
    ) as startup_gate:
        recovery = launcher._finalize_journal(root)
        if recovery is not None and recovery.status in {
            "repair_required",
            "retry_pending",
            "deferred_active_session",
            "skipped_locked",
        }:
            raise ShellError(f"managed update recovery returned {recovery.status}")
        if getattr(startup_gate, "waited", False):
            raise ShellError("another managed update attempt completed; retry to verify the current signed stable release")
        if not launcher._updates_enabled(root):
            raise ShellError("managed update protocol is not ready")
        state = updater._read_update_state(root)
        if launcher._head_commit(root) != state.last_release_commit:
            raise ShellError("managed HEAD differs from the protected release state")
        trusted_keys = load_trusted_release_keys(
            root,
            deadline=deadline,
            expected_commit=state.last_release_commit,
        )
        if not trusted_keys:
            raise ShellError("managed release trust store contains no active key")
        result = launcher._attempt_update(root, trusted_keys, deadline=deadline)
except (
    ShellError,
    OSError,
    ValueError,
    update_coordination.InstallTransactionBusy,
) as exc:
    print(f"dp-install: managed stable update refused: {exc}", file=sys.stderr)
    raise SystemExit(1)

accepted_statuses = {"up_to_date", "candidate_ready", "updated"}
print(result.status)
if result.status not in accepted_statuses:
    blocker_pids = ",".join(
        str(blocker.pid) for blocker in result.blockers if blocker.pid is not None
    )
    details = [f"status={result.status}"]
    if result.error_code:
        details.append(f"error_code={result.error_code}")
    if blocker_pids:
        details.append(f"active_shim_pids={blocker_pids}")
    print(
        "dp-install: managed stable update did not apply: " + ", ".join(details),
        file=sys.stderr,
    )
raise SystemExit(0 if result.status in accepted_statuses else 1)
PY
  )
}

resume_managed_root=0
activated_repair_mode=0
if [ "$managed_root_was_present" -eq 1 ]; then
  if ! validate_complete_managed_root; then
    blocked \
      "$MANAGED_ROOT exists but is not a complete, clean, verified managed stable install. Preserve it and use the managed uninstall/replacement flow"
  fi
  if ! existing_activation_state="$(managed_activation_state)"; then
    blocked \
      "$MANAGED_ROOT is managed, but its activation state could not be verified. Preserve it and use the managed repair flow"
  fi
  if managed_activation_recovery_pending; then
    blocked \
      "$MANAGED_ROOT has an activation recovery marker; do not retry automatically. Preserve it and use the owner-guided activation recovery flow"
  fi
  if [ "$existing_activation_state" = "activated" ]; then
    activated_repair_mode=1
    tty_print "Found a complete signed and activated Decision Engine install; entering signed stable update and host repair mode without reopening activation."
  else
    tty_print "Found a complete signed Decision Engine stable install with activation pending; resuming setup without replacing it."
  fi
  resume_managed_root=1
fi

if [ "$activated_repair_mode" -eq 1 ]; then
  config_digest_before_update="$(managed_config_digest)" \
    || blocked "could not fingerprint the activated Decision Engine configuration before update"
  tty_print "Checking and applying the newest signed Decision Engine stable release before host repair..."
  if ! managed_update_status="$(update_activated_managed_root)"; then
    if validate_complete_managed_root \
        && [ "$(managed_activation_state)" = "activated" ] \
        && [ "$(managed_config_digest)" = "$config_digest_before_update" ]; then
      fail "signed stable update status ${managed_update_status:-unknown}; the existing activated release was verified and preserved. Close every configured Agent host and retry"
    fi
    blocked "signed stable update did not complete and the previous activated release could not be re-verified; preserve the managed root and use the owner-guided recovery flow"
  fi
  validate_complete_managed_root \
    || blocked "signed stable update returned $managed_update_status, but the managed release no longer validates"
  [ "$(managed_activation_state)" = "activated" ] \
    || blocked "signed stable update returned $managed_update_status, but permanent activation was not preserved"
  [ "$(managed_config_digest)" = "$config_digest_before_update" ] \
    || blocked "signed stable update returned $managed_update_status, but the protected activation configuration changed"
  tty_print "Decision Engine signed stable update status: $managed_update_status"
fi

if ! source_detected_clients="$(
  cd "$source_root"
  de_exec "$PYTHON_BIN" -c \
    'from installer import mcp_config; print("\n".join(mcp_config.detect_clients()))'
)"; then
  fail "could not inspect the current Decision Engine host adapter catalog"
fi

# These bundles are distinct products, not aliases for the supported Desktop
# adapters with similar names. Report them explicitly when the current product
# source does not recognize them so a partial install cannot look complete.
unsupported_installed_hosts=""
if regular_app_has_bundle_id "/Applications/CodeBuddy Studio.app" "com.codebuddy.ride" \
    && ! line_list_contains "$source_detected_clients" "codebuddy-studio"; then
  unsupported_installed_hosts="$(append_client_line \
    "$unsupported_installed_hosts" \
    "codebuddy-studio (CodeBuddy Studio.app): DE adapter unavailable")"
fi
if regular_app_has_bundle_id "/Applications/CodeBuddy.app" "com.tencent.codebuddy" \
    && ! line_list_contains "$source_detected_clients" "codebuddy"; then
  unsupported_installed_hosts="$(append_client_line \
    "$unsupported_installed_hosts" \
    "codebuddy (CodeBuddy.app): DE adapter unavailable")"
fi
if regular_app_has_bundle_id "/Applications/Qoder IDE.app" "com.qoder.ide" \
    && ! line_list_contains "$source_detected_clients" "qoder-ide"; then
  unsupported_installed_hosts="$(append_client_line \
    "$unsupported_installed_hosts" \
    "qoder-ide (Qoder IDE.app): separate product; DE adapter unavailable")"
fi
if regular_app_has_bundle_id "/Applications/Qoder CN IDE.app" "com.aliyun.lingma.ide" \
    && ! line_list_contains "$source_detected_clients" "qoder-cn-ide"; then
  unsupported_installed_hosts="$(append_client_line \
    "$unsupported_installed_hosts" \
    "qoder-cn-ide (Qoder CN IDE.app): separate product; DE adapter unavailable")"
fi

verify_aqg_checkout() {
  local actual_remote status_output resolved parent version
  if [ -L "$AQG_ROOT" ]; then
    # Same managed-layout contract as de-aqg-install. Resolve aliases on both
    # sides (macOS /var, relative links) without accepting escaped targets.
    resolved="$(cd "$AQG_ROOT" 2>/dev/null && pwd -P)" \
      || fail "$AQG_ROOT is a symlink that cannot be resolved; preserve it and stop"
    parent="$(cd "$(dirname "$AQG_ROOT")" 2>/dev/null && pwd -P)" \
      || fail "$AQG_ROOT has no resolvable parent directory; preserve it and stop"
    [ "${resolved%/*}" = "$parent/versions" ] \
      || fail "$AQG_ROOT is a symlink outside the managed versions directory; preserve it and stop"
    version="${resolved##*/}"
    [[ "$version" =~ ^[0-9a-f]{40}$ ]] \
      || fail "$AQG_ROOT is not a full-commit checkout in the managed versions directory; preserve it and stop"
  elif [ ! -d "$AQG_ROOT" ]; then
    fail "$AQG_ROOT is not a regular AQG checkout directory; preserve it and stop"
  fi
  [ -e "$AQG_ROOT/.git" ] \
    || fail "$AQG_ROOT is not a Git checkout; preserve it and stop"
  [ -f "$AQG_ROOT/AI_SETUP.md" ] \
    || fail "$AQG_ROOT is missing AI_SETUP.md; preserve it and stop"
  [ -f "$AQG_ROOT/scripts/install_aqg_clients.py" ] \
    || fail "$AQG_ROOT is missing the multi-host AQG installer; preserve it and stop"
  [ -f "$AQG_ROOT/scripts/aqg_doctor.py" ] \
    || fail "$AQG_ROOT is missing AQG Doctor; preserve it and stop"
  actual_remote="$(clean_exec "$GIT_BIN" -C "$AQG_ROOT" remote get-url origin 2>/dev/null || true)"
  case "$actual_remote" in
    "$AQG_REPO"|git@github.com:deeppatternai/agent-quality-gates.git|ssh://git@github.com/deeppatternai/agent-quality-gates.git) ;;
    *) fail "$AQG_ROOT has an unexpected Git origin; preserve it and stop" ;;
  esac
  status_output="$(clean_exec "$GIT_BIN" -C "$AQG_ROOT" status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
    || fail "could not verify the AQG checkout state; preserve it and stop"
  [ -z "$status_output" ] \
    || fail "$AQG_ROOT has local changes; preserve it and stop"
}

sync_aqg_checkout() {
  local target_sha
  verify_aqg_checkout
  if [ -L "$AQG_ROOT" ]; then
    tty_print "Reusing the managed AQG version; subsequent updates belong to AQG's signed channel."
    return 0
  fi
  tty_print "Synchronizing Agent Quality Gates from $AQG_REPO at $AQG_REF..."
  if ! clean_exec env GIT_TERMINAL_PROMPT=0 "$GIT_BIN" -C "$AQG_ROOT" fetch \
      --depth 1 "$AQG_REPO" "refs/heads/$AQG_REF"; then
    fail "could not read the AQG product target $AQG_REF; the existing checkout was preserved"
  fi
  target_sha="$(clean_exec "$GIT_BIN" -C "$AQG_ROOT" rev-parse --verify FETCH_HEAD 2>/dev/null || true)"
  [[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] \
    || fail "the AQG product target did not resolve to a valid commit; the existing checkout was preserved"
  clean_exec "$GIT_BIN" -C "$AQG_ROOT" checkout --detach "$target_sha" \
    || fail "could not switch the AQG checkout to the approved target; preserve it and stop"
  clean_exec "$GIT_BIN" -C "$AQG_ROOT" remote set-url origin "$AQG_REPO" \
    || fail "AQG was updated, but its origin could not be normalized to the product repository"
  verify_aqg_checkout
}

run_aqg_clients() {
  local phase="$1" status
  shift
  if [ -z "${aqg_selected_clients:-}" ]; then
    tty_print "AQG $phase: no supported installed AQG host requires configuration."
    return 0
  fi
  # Preserve the exact host selection made below while using AQG's
  # installed-supported mode so mixed-scope clients keep their user commands
  # when no PROJECT_ROOT was selected.
  if (
    cd "$AQG_ROOT"
    clean_exec env PATH="$PYTHON_DIR:$PATH" "$PYTHON_BIN" -c \
      'import sys
from scripts import install_aqg_clients

clients = tuple(client for client in sys.argv[1].split(",") if client)
detection = install_aqg_clients.DetectionResult(
    selected=clients,
    evidence={client: "selected by dp-install" for client in clients},
    conflicts=(),
)
install_aqg_clients.detect_clients = lambda home=None: detection
raise SystemExit(
    install_aqg_clients.main(
        ["--installed-supported", "--aqg-root", sys.argv[2], *sys.argv[3:]]
    )
)' "$aqg_selected_clients" "$AQG_ROOT" "$@"
  ); then
    return 0
  else
    status=$?
  fi
  if [ "$status" -eq 3 ]; then
    tty_print "AQG $phase: no supported installed AQG host requires configuration."
    return 0
  fi
  return "$status"
}

workbuddy_ai_de_is_approved() {
  [ "$workbuddy_variant" = "ai" ] || return 1
  clean_exec "$PYTHON_BIN" - \
    "$WORKBUDDY_AI_ROOT/mcp.json" \
    "$WORKBUDDY_AI_ROOT/mcp-approvals.json" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

config_path = Path(sys.argv[1])
approvals_path = Path(sys.argv[2])
try:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    approvals = json.loads(approvals_path.read_text(encoding="utf-8"))
    entry = config["mcpServers"]["decision-engine"]
    command = entry["command"]
    args = entry.get("args", [])
    env = entry.get("env", {})
    if not isinstance(command, str) or not isinstance(args, list) or not isinstance(env, dict):
        raise ValueError("unexpected Decision Engine MCP entry")
    fingerprint_input = "|".join((
        command,
        ",".join(sorted(str(value) for value in args)),
        ",".join(sorted(env)),
    ))
    fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    approved = isinstance(approvals, dict) and f"{fingerprint}::decision-engine" in approvals
except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
    approved = False
raise SystemExit(0 if approved else 1)
PY
}

inspect_managed_claude_hooks() {
  clean_exec env PATH="$PYTHON_DIR:$PATH" "$PYTHON_BIN" -c \
    'from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import install_aqg_hooks
state, detail = install_aqg_hooks.inspect_install(
    Path.home() / ".claude" / "settings.json", Path(sys.argv[1])
)
print(f"{state}: {detail}")
raise SystemExit({"complete": 0, "missing": 0, "stale": 10}.get(state, 11))' \
    "$AQG_ROOT"
}

converge_managed_claude_hooks() {
  local status=0
  inspect_managed_claude_hooks || status=$?
  case "$status" in
    0) return 0 ;;
    10)
      tty_print "Converging stale AQG-managed Claude hooks through the official backup-first installer..."
      clean_exec env PATH="$PYTHON_DIR:$PATH" \
        "$PYTHON_BIN" "$AQG_ROOT/scripts/install_aqg_hooks.py" \
        --uninstall --aqg-root "$AQG_ROOT" || return 1
      clean_exec env PATH="$PYTHON_DIR:$PATH" \
        "$PYTHON_BIN" "$AQG_ROOT/scripts/install_aqg_hooks.py" \
        --apply --aqg-root "$AQG_ROOT" || return 1
      inspect_managed_claude_hooks
      ;;
    *)
      printf '%s: ERROR: AQG-managed Claude hook state is invalid; refusing to rewrite it\n' "$PROGRAM_NAME" >&2
      return 1
      ;;
  esac
}

install_aqg_dependencies() {
  tty_print "Installing or verifying AQG runtime dependencies with $PYTHON_BIN..."
  if clean_exec "$PYTHON_BIN" -c \
      'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' \
      </dev/null >/dev/null 2>&1; then
    clean_exec "$PYTHON_BIN" -m pip install -r "$AQG_ROOT/requirements.txt"
  else
    clean_exec "$PYTHON_BIN" -m pip install --user -r "$AQG_ROOT/requirements.txt"
  fi
}

if [ -e "$AQG_ROOT" ] || [ -L "$AQG_ROOT" ]; then
  sync_aqg_checkout
else
  tty_print "Installing Agent Quality Gates from the product repository at $AQG_REF..."
  clean_exec mkdir -p "$(dirname "$AQG_ROOT")"
  if ! clean_exec env GIT_TERMINAL_PROMPT=0 "$GIT_BIN" clone \
      --config core.autocrlf=false --config core.eol=lf \
      --depth 1 --branch "$AQG_REF" --single-branch "$AQG_REPO" "$AQG_ROOT"; then
    fail "could not obtain the AQG product source at $AQG_REF; Decision Engine was not installed"
  fi
  verify_aqg_checkout
fi

if ! aqg_detected_clients="$(
  cd "$AQG_ROOT"
  clean_exec env PATH="$PYTHON_DIR:$PATH" "$PYTHON_BIN" -c \
    'from scripts import install_aqg_clients

for client in install_aqg_clients.installed_supported_clients():
    print(client)'
)"; then
  fail "could not inspect the current AQG host adapter catalog"
fi
aqg_selected_clients=""
while IFS= read -r aqg_client; do
  [ -n "$aqg_client" ] || continue
  # WorkBuddy AI is a separate macOS product identity whose real data root is
  # .workbuddy-ai. Do not let a leftover .workbuddy directory select the old
  # profile; AQG's registered workbuddy-ai profile owns this variant.
  if [ "$workbuddy_variant" = "ai" ] && [ "$aqg_client" = "workbuddy" ]; then
    continue
  fi
  if [ -n "$aqg_selected_clients" ]; then
    aqg_selected_clients="$aqg_selected_clients,$aqg_client"
  else
    aqg_selected_clients="$aqg_client"
  fi
done <<<"$aqg_detected_clients"

install_aqg_dependencies \
  || fail "AQG dependency installation failed; Decision Engine was not installed"

tty_print "Planning AQG configuration for every supported host detected by AQG..."
run_aqg_clients "dry-run" \
  || fail "AQG multi-host dry-run failed; Decision Engine was not installed"
tty_print "Applying AQG skills, rules, and only the lifecycle hooks supported by each detected host..."
run_aqg_clients "apply" --apply \
  || fail "AQG multi-host configuration failed; Decision Engine was not installed"
converge_managed_claude_hooks \
  || fail "AQG-managed Claude hooks could not be converged safely; Decision Engine was not installed"
tty_print "Verifying AQG multi-host configuration..."
run_aqg_clients "verify" --verify \
  || fail "AQG multi-host verification failed; Decision Engine was not installed"
if ! clean_exec env PATH="$PYTHON_DIR:$PATH" \
    "$PYTHON_BIN" "$AQG_ROOT/scripts/aqg_doctor.py"; then
  fail "AQG Doctor failed after multi-host configuration; Decision Engine was not installed"
fi
aqg_sha="$(clean_exec "$GIT_BIN" -C "$AQG_ROOT" rev-parse HEAD)"

tty_print "Installing the signed Decision Engine stable release..."
if [ "$resume_managed_root" -eq 1 ]; then
  tty_print "Reusing the existing signed Decision Engine stable checkout without cloning or replacing it."
else
  bootstrap_client="$(printf '%s\n' "$source_detected_clients" | sed -n '1p')"
  [ -n "$bootstrap_client" ] \
    || blocked "no supported Agent host was detected before the signed stable core install"
  core_install_status=0
  (
    cd "$source_root"
    # Run only the signed-core phase here. AQG has already passed its complete
    # equivalent of WITH_AQG=1 above. Calling main's `install.sh de` with
    # WITH_MCP=1 lets its newer host catalog flow into an older signed stable
    # release before that release can publish its own contract. It also opens
    # permanent setup inside the bootstrap, which would duplicate the dialog
    # owned below. bootstrap_client satisfies the bootstrap's non-empty target
    # invariant only; no external host entry is written in this phase.
    de_exec env GIT_TERMINAL_PROMPT=0 "$PYTHON_BIN" -c \
      'from installer import bootstrap_managed_install
import sys
bootstrap_managed_install.bootstrap_new_install(clients=(sys.argv[1],))' \
      "$bootstrap_client"
  ) || core_install_status=$?
  if [ "$core_install_status" -ne 0 ]; then
    if managed_activation_recovery_pending; then
      fail "the signed stable core landed, but managed-core recovery is required; no automatic retry was attempted"
    fi
    fail "the signed stable core bootstrap failed; inspect the installer output above"
  fi
  if ! validate_complete_managed_root; then
    fail "the core installation did not produce a complete verified managed stable install; inspect the installer output above"
  fi
fi
validate_complete_managed_root \
  || fail "the managed stable install changed or became incomplete before activation"
managed_sha="$(clean_exec "$GIT_BIN" -C "$MANAGED_ROOT" rev-parse HEAD)"
managed_version="$(clean_exec tr -d '[:space:]' < "$MANAGED_ROOT/VERSION")"
[ -n "$managed_version" ] || fail "the managed install has an empty VERSION"

# The temporary main checkout is only the trusted bootstrap driver. Once the
# signed stable checkout has landed, all runtime operations must use that copy
# so its Python modules, release identity, and host contracts stay aligned.
run_managed_python() {
  (
    cd "$MANAGED_ROOT"
    # Do not allow an inherited PYTHONPATH to shadow the landed managed copy.
    de_exec "$PYTHON_BIN" "$@"
  )
}

ensure_managed_runtime_git_excludes() {
  run_managed_python -c \
    'from pathlib import Path
import os
import stat
import sys

root = Path(sys.argv[1])
git_dir = root / ".git"
info_dir = git_dir / "info"
exclude_path = info_dir / "exclude"
marker = b"/stopper-ui.json"

for directory in (git_dir, info_dir):
    if directory == info_dir and not directory.exists():
        directory.mkdir(mode=0o700)
    entry = directory.lstat()
    if not stat.S_ISDIR(entry.st_mode) or directory.is_symlink():
        raise SystemExit("refusing an unsafe managed Git metadata directory")
    if hasattr(os, "getuid") and entry.st_uid != os.getuid():
        raise SystemExit("managed Git metadata is not owned by this user")

flags = os.O_RDWR | os.O_CREAT
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(exclude_path, flags, 0o600)
try:
    entry = os.fstat(fd)
    if not stat.S_ISREG(entry.st_mode):
        raise SystemExit("managed Git exclude path is not a regular file")
    if hasattr(os, "getuid") and entry.st_uid != os.getuid():
        raise SystemExit("managed Git exclude file is not owned by this user")
    if entry.st_size > 128 * 1024:
        raise SystemExit("managed Git exclude file is oversized")
    with os.fdopen(fd, "r+b", closefd=False) as handle:
        data = handle.read()
        if marker not in data.splitlines():
            handle.seek(0, os.SEEK_END)
            if data and not data.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(marker + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
finally:
    os.close(fd)' \
    "$MANAGED_ROOT"
}

ensure_macos_stopper_host() {
  local state
  tty_print "Installing and verifying the macOS Stopper host bridge..."
  run_managed_python -m installer.stopper_launch_agent install \
    --de-root "$MANAGED_ROOT" >/dev/tty || return 1
  state="$(run_managed_python -c \
    'from pathlib import Path
import sys
from installer import stopper_launch_agent

result = stopper_launch_agent.status(de_root=Path(sys.argv[1]))
print(result.get("status", "unknown"))' \
    "$MANAGED_ROOT")" || return 1
  [ "$state" = "ready" ]
}

# The third-party provider profile is its own registered Decision Engine host
# (claude-desktop-3p), so the signed release detects, writes, and verifies it
# through the ordinary client path. Only the fail-closed file guard stays here:
# it must refuse before any host write, and it protects a product-owned file
# rather than a Decision Engine one.
claude_3p_profile_detected=0
if [ -d "$CLAUDE_3P_ROOT" ] || [ -e "$CLAUDE_3P_CONFIG" ] \
    || [ -L "$CLAUDE_3P_CONFIG" ]; then
  if [ -e "$CLAUDE_3P_CONFIG" ] || [ -L "$CLAUDE_3P_CONFIG" ]; then
    if [ -L "$CLAUDE_3P_CONFIG" ] || [ ! -f "$CLAUDE_3P_CONFIG" ]; then
      blocked \
        "$CLAUDE_3P_CONFIG is not a regular configuration file; preserve it and repair the Claude third-party profile before continuing"
    fi
  fi
  claude_3p_profile_detected=1
fi

detect_managed_clients() {
  run_managed_python -c \
    'from installer import mcp_config; print("\n".join(mcp_config.detect_clients()))'
}

normalize_managed_clients_for_variant() {
  local clients="$1" normalized="" client
  if [ "$workbuddy_variant" != "ai" ] \
      || ! line_list_contains "$clients" "workbuddy-ai"; then
    printf '%s\n' "$clients"
    return 0
  fi
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    [ "$client" = "workbuddy" ] && continue
    normalized="$(append_client_line "$normalized" "$client")"
  done <<<"$clients"
  printf '%s\n' "$normalized"
}

if ! managed_detected_clients="$(detect_managed_clients)"; then
  fail "could not detect hosts through the signed Decision Engine stable release"
fi
managed_skill_route_exclusions=""
if [ "$workbuddy_variant" = "ai" ] \
    && line_list_contains "$managed_detected_clients" "workbuddy-ai"; then
  managed_skill_route_exclusions="workbuddy"
fi
managed_detected_clients="$(
  normalize_managed_clients_for_variant "$managed_detected_clients"
)"
source_clients_for_catalog="$(
  normalize_managed_clients_for_variant "$source_detected_clients"
)"

managed_mcp_supports_allow_unactivated() {
  run_managed_python -c \
    'import inspect
from installer import mcp_config
supports_status = "allow_unactivated" in inspect.signature(mcp_config.entry_status).parameters
supports_write = "allow_unactivated" in inspect.signature(mcp_config.write_entry).parameters
raise SystemExit(0 if supports_status and supports_write else 1)' \
    </dev/null >/dev/null 2>&1
}

managed_mcp_allow_unactivated=0
if managed_mcp_supports_allow_unactivated; then
  managed_mcp_allow_unactivated=1
fi

managed_supports_claude_desktop_3p() {
  run_managed_python -c \
    'from installer import mcp_config; raise SystemExit(0 if "claude-desktop-3p" in mcp_config.CLIENTS else 1)' \
    </dev/null >/dev/null 2>&1
}

# Old stable releases may detect a stale config path even when their own
# product-identity guard refuses to write that host. Use the release's dry-run
# as the final, non-mutating preflight before activation receives the list.
detected_clients=""
unwritable_clients=""
while IFS= read -r client; do
  [ -n "$client" ] || continue
  if [ "$managed_mcp_allow_unactivated" = "1" ]; then
    if run_managed_python -m installer.mcp_config --write --dry-run \
        --allow-unactivated --client "$client" >/dev/null; then
      detected_clients="$(append_client_line "$detected_clients" "$client")"
    else
      unwritable_clients="$(append_client_line "$unwritable_clients" "$client")"
    fi
  elif run_managed_python -m installer.mcp_config --write --dry-run \
      --client "$client" >/dev/null; then
    detected_clients="$(append_client_line "$detected_clients" "$client")"
  else
    unwritable_clients="$(append_client_line "$unwritable_clients" "$client")"
  fi
done <<<"$managed_detected_clients"

if [ "$claude_3p_profile_detected" = "1" ]; then
  managed_supports_claude_desktop_3p \
    || fail "signed stable $managed_version does not provide the claude-desktop-3p host adapter required by the detected third-party profile"
fi

if [ -z "$detected_clients" ]; then
  blocked \
    "no Agent host accepted by Decision Engine $managed_version passed its non-mutating wiring preflight"
fi

tty_print "Detected all supported Decision Engine hosts present on this Mac for signed stable $managed_version:"
tty_print "Every listed host will be configured; each passed the stable release's wiring preflight."
printf '%s\n' "$detected_clients" >/dev/tty
if line_list_contains "$detected_clients" "claude-desktop-3p"; then
  tty_print "claude-desktop-3p is the third-party provider profile; it is configured independently of claude-desktop."
fi
tty_print "Running this command authorizes setup for every listed host."

catalog_missing_clients=""
while IFS= read -r source_client; do
  [ -n "$source_client" ] || continue
  if ! printf '%s\n' "$managed_detected_clients" | grep -Fxq "$source_client"; then
    catalog_missing_clients="$(append_client_line "$catalog_missing_clients" "$source_client")"
  fi
done <<<"$source_clients_for_catalog"
if [ -n "$catalog_missing_clients" ]; then
  tty_print "Installed host adapters not present in signed stable $managed_version and therefore not configured:"
  printf '%s\n' "$catalog_missing_clients" >/dev/tty
fi
if [ -n "$unwritable_clients" ]; then
  tty_print "Hosts detected by signed stable $managed_version but rejected by its own wiring preflight and therefore not configured:"
  printf '%s\n' "$unwritable_clients" >/dev/tty
fi
if [ -n "$unsupported_installed_hosts" ]; then
  tty_print "Installed products not recognized by the current Decision Engine adapter catalog:"
  printf '%s\n' "$unsupported_installed_hosts" >/dev/tty
fi

verify_managed_mcp_wiring() {
  local allow_unactivated="${1:-0}" client status
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    if [ "$allow_unactivated" = "1" ] \
        && [ "$managed_mcp_allow_unactivated" = "1" ]; then
      if ! status="$(run_managed_python -c \
        'from installer import mcp_config; import sys; print(mcp_config.entry_status(sys.argv[1], allow_unactivated=True))' \
        "$client")"; then
        printf '%s: ERROR: could not verify MCP wiring for %s\n' "$PROGRAM_NAME" "$client" >&2
        return 1
      fi
    elif ! status="$(run_managed_python -c \
        'from installer import mcp_config; import sys; print(mcp_config.entry_status(sys.argv[1]))' \
        "$client")"; then
      printf '%s: ERROR: could not verify MCP wiring for %s\n' "$PROGRAM_NAME" "$client" >&2
      return 1
    fi
    if [ "$status" != "ready" ]; then
      printf '%s: ERROR: MCP wiring for %s is %s, not ready\n' \
        "$PROGRAM_NAME" "$client" "${status:-unknown}" >&2
      return 1
    fi
  done <<<"$detected_clients"
}

repair_managed_skill_routes() {
  # The core body is installed before activation, so its initial skill-route
  # pass sees no DE MCP entries. Re-run the existing setup-only repair after
  # publishing the entries so every skill-capable detected host is usable in
  # both activated and unactivated installations.
  run_managed_python -c \
    'import sys
from installer import config, install

excluded = frozenset(client for client in sys.argv[1].split(",") if client)
result = install.repair_detected_skill_routes(
    config.managed_component_root("decision-engine"),
    excluded_clients=excluded,
)
raise SystemExit(1 if result.failed else 0)' \
    "$managed_skill_route_exclusions"
}

wire_all_detected_hosts() {
  local allow_unactivated="${1:-0}" failed=0 client
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    if [ "$allow_unactivated" = "1" ] \
        && [ "$managed_mcp_allow_unactivated" = "1" ]; then
      run_managed_python -m installer.mcp_config --write --allow-unactivated --client "$client" || {
        printf '%s: ERROR: MCP wiring failed for %s\n' "$PROGRAM_NAME" "$client" >&2
        failed=1
      }
    else
      run_managed_python -m installer.mcp_config --write --client "$client" || {
        printf '%s: ERROR: MCP wiring failed for %s\n' "$PROGRAM_NAME" "$client" >&2
        failed=1
      }
    fi
  done <<<"$detected_clients"
  [ "$failed" -eq 0 ] || return 1
  if printf '%s\n' "$detected_clients" | grep -Fxq codex; then
    run_managed_python -m installer.codex_routing || return 1
  fi
  repair_managed_skill_routes || {
    printf '%s: ERROR: managed skill routing failed for one or more hosts\n' "$PROGRAM_NAME" >&2
    return 1
  }
  verify_managed_mcp_wiring "$allow_unactivated"
}

wire_unactivated_mcp() {
  tty_print "activation was cancelled. Publishing managed MCP entries for all listed hosts so the unactivated DE Lite path remains available..."
  wire_all_detected_hosts 1
}

capability_report_incomplete=0
print_host_capability_report() {
  local client display_name capability aqg_state
  tty_print "Decision Engine host capability report:"
  tty_print "The MCP status below proves the configuration on disk. Restarting the host is still required before runtime use."
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    display_name="$client"
    if [ "$client" = "workbuddy" ] && [ "$workbuddy_variant" = "ai" ]; then
      display_name="workbuddy-ai"
    fi
    capability="$(run_managed_python -c \
      'from installer.client_hosts.registry import CLIENT_SPECS
import sys
spec = CLIENT_SPECS[sys.argv[1]]
skills = "managed" if spec.skill_delivery_mode != "none" else "not-supported"
print(f"skills={skills}; routing={spec.routing_kind}")' \
      "$client")" || {
        capability="skills=unknown; routing=unknown"
        capability_report_incomplete=1
      }
    aqg_state="not-selected"
    if comma_list_contains "$aqg_selected_clients" "$client"; then
      aqg_state="configured-and-verified"
    elif [ "$client" = "workbuddy" ] \
        && [ "$workbuddy_variant" = "ai" ] \
        && comma_list_contains "$aqg_selected_clients" "workbuddy-ai"; then
      aqg_state="configured-and-verified"
    fi
    if [ "$client" = "claude-desktop-3p" ]; then
      # MCP-only third-party provider profile. Its connector state and its
      # on-disk configuration are reported separately from runtime, which
      # Decision Engine cannot observe until this host restarts and actually
      # calls a tool. The literal capabilities below are re-derived from the
      # registry above and flagged here if they ever drift.
      if [ "$capability" != "skills=not-supported; routing=mcp-only" ]; then
        capability_report_incomplete=1
      fi
      tty_print "claude-desktop-3p: DE MCP=connector-written; config=disk-ready; skills=not-supported; routing=mcp-only; AQG=unsupported-for-this-profile; runtime=unverified; runtime-verification=restart-required"
      continue
    fi
    tty_print "$display_name: DE MCP=disk-ready; $capability; AQG=$aqg_state; runtime=restart-required"
  done <<<"$detected_clients"
  tty_print "Agent Quality Gates host capability report:"
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    tty_print "$client: AQG=configured-and-verified"
  done <<<"$(printf '%s' "$aqg_selected_clients" | tr ',' '\n')"
  if [ -z "$aqg_selected_clients" ]; then
    tty_print "(no supported AQG host detected)"
  fi
  if [ -n "$catalog_missing_clients" ]; then
    tty_print "Pending signed stable adapters (not configured):"
    printf '%s\n' "$catalog_missing_clients" >/dev/tty
  fi
  if [ -n "$unwritable_clients" ]; then
    tty_print "Rejected host configurations (not configured):"
    printf '%s\n' "$unwritable_clients" >/dev/tty
  fi
  if [ -n "$unsupported_installed_hosts" ]; then
    tty_print "Unsupported installed products (not configured by DE):"
    printf '%s\n' "$unsupported_installed_hosts" >/dev/tty
  fi
}

run_selected_permanent_setup() {
  local client
  set --
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    set -- "$@" --client "$client"
  done <<<"$detected_clients"
  run_managed_python -m installer.permanent_setup "$@"
}

ensure_managed_runtime_git_excludes \
  || fail "the managed runtime Git exclusion could not be installed safely; no activation attempt was made"
ensure_macos_stopper_host \
  || fail "the managed core is complete, but the owned macOS Stopper host bridge could not be installed and verified; the existing activation state was preserved"
validate_complete_managed_root \
  || fail "the managed stable install changed while preparing the Stopper host bridge"

if [ "$activated_repair_mode" -eq 1 ]; then
  if ! wire_all_detected_hosts; then
    fail "activated host repair failed; the existing core and activation credentials were preserved"
  fi
  tty_print "Decision Engine host repair is complete; the existing activation and signed core were preserved."
  tty_print "Restart the configured host applications before using repaired MCP and skill routes."
else
  activation_status=0
  run_selected_permanent_setup || activation_status=$?

  case "$activation_status" in
    0)
      if ! (
        cd "$MANAGED_ROOT"
        clean_exec "$PYTHON_BIN" -c \
          'from installer import activate, config; raise SystemExit(0 if activate.is_permanently_activated(config.load_json(config.de_config_path())) else 1)'
      ); then
        fail "activation returned success but the managed device binding could not be verified"
      fi
      if ! wire_all_detected_hosts; then
        fail "activation succeeded, but at least one detected host MCP entry is not ready"
      fi
      tty_print "Decision Engine installation is complete and the device is activated."
      tty_print "Restart the configured host applications before using Decision Engine."
      ;;
    2)
      if ! wire_unactivated_mcp; then
        fail "core installation is complete, but unactivated host wiring failed"
      fi
      tty_print "activation was cancelled; core installation is complete in the unactivated state."
      tty_print "Restart the configured host applications to use the DE Lite path."
      ;;
    *)
      actual_activation_state="$(managed_activation_state 2>/dev/null || true)"
      if [ "$actual_activation_state" = "activated" ]; then
        printf '%s: device is activated, but post-activation validation failed (setup exit %s).\n' \
          "$PROGRAM_NAME" "$activation_status" >&2
        printf '%s: fix the Doctor item and rerun this installer; the saved device credentials will be reused without another activation.\n' \
          "$PROGRAM_NAME" >&2
      else
        printf '%s: activation failed; core installation is complete, but the device remains unactivated.\n' "$PROGRAM_NAME" >&2
        printf '%s: rerun the managed activation flow after correcting the owner values or network.\n' "$PROGRAM_NAME" >&2
      fi
      exit 1
      ;;
  esac
fi

tty_print "Running final Decision Engine Doctor..."
run_managed_python -m installer.doctor \
  || fail "Decision Engine Doctor still reports a blocking failure after installation or repair"

print_host_capability_report

manual_host_action_pending=0
if [ "$workbuddy_variant" = "ai" ]; then
  if workbuddy_ai_de_is_approved; then
    tty_print "WorkBuddy AI has approved the Decision Engine MCP connector."
  else
    manual_host_action_pending=1
    tty_print "WorkBuddy AI detected the Decision Engine MCP connector, but its third-party MCP security gate still requires one in-app approval."
    tty_print "In WorkBuddy AI > MCP Service Management, switch decision-engine on and approve it; then restart WorkBuddy AI. Reconnect alone cannot approve an untrusted server."
  fi
fi

printf '%s: source=%s\n' "$PROGRAM_NAME" "$source_sha"
printf '%s: decision-engine-version=%s\n' "$PROGRAM_NAME" "$managed_version"
printf '%s: decision-engine=%s\n' "$PROGRAM_NAME" "$managed_sha"
printf '%s: aqg=%s\n' "$PROGRAM_NAME" "$aqg_sha"

if [ -n "$catalog_missing_clients" ] \
    || [ -n "$unwritable_clients" ] \
    || [ -n "$unsupported_installed_hosts" ] \
    || [ "$capability_report_incomplete" -ne 0 ] \
    || [ "$manual_host_action_pending" -ne 0 ]; then
  printf '%s: PARTIAL: supported components were installed, but one or more detected hosts are unsupported, unconfigured, unverifiable, or awaiting in-app approval.\n' \
    "$PROGRAM_NAME" >&2
  exit "$EXIT_PARTIAL"
fi

printf '%s: PASS: all detected supported hosts are configured on disk; restart the host applications before runtime verification.\n' \
  "$PROGRAM_NAME"
