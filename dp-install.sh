#!/usr/bin/env bash
#
# Deep Pattern (DP) first-time installer for macOS.
#
# This is the thin public entrypoint intended for:
#
#   curl -fsSL <trusted-install-url> | bash
#
# It deliberately accepts no arguments. The Decision Engine repository supplies
# the signed stable release installer; this wrapper prepares a private Python
# runtime when needed, bootstraps that installer, then opens the existing masked
# activation dialog.
#
set -euo pipefail

DE_REPO="https://github.com/deeppatternai/decision-engine.git"
AQG_REPO="https://github.com/deeppatternai/agent-quality-gates.git"
AQG_REF="main"
PROGRAM_NAME="dp-install"
MANAGED_ROOT="$HOME/.deeppattern/decision-engine"
AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
DEEPPATTERN_ROOT="$HOME/.deeppattern"
MANAGED_PYTHON_ROOT="$DEEPPATTERN_ROOT/de-python"
MANAGED_PYTHON_BIN="$MANAGED_PYTHON_ROOT/bin/python3"
PRIVATE_RUNTIME_ROOT="$DEEPPATTERN_ROOT/runtimes"
PRIVATE_RUNTIME_BACKUP_ROOT="$DEEPPATTERN_ROOT/runtime-backups"
PRIVATE_PYTHON_VERSION="3.13.15"
PRIVATE_PYTHON_BUILD="20260901"
PRIVATE_PYTHON_RELEASE="20260901"
PRIVATE_PYTHON_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PRIVATE_PYTHON_RELEASE"
PRIVATE_RUNTIME_MARKER_NAME=".deeppattern-python-runtime"
MANAGED_PYTHON_MARKER_NAME=".deeppattern-python-environment"
CLAUDE_3P_ROOT="$HOME/Library/Application Support/Claude-3p"
CLAUDE_3P_CONFIG="$CLAUDE_3P_ROOT/claude_desktop_config.json"
WORKBUDDY_STANDARD_APP="/Applications/WorkBuddy.app"
WORKBUDDY_AI_APP="/Applications/WorkBuddy AI.app"
WORKBUDDY_AI_ROOT="$HOME/.workbuddy-ai"
XCODE_SELECT_BIN="/usr/bin/xcode-select"
CLT_INSTALLER_APP="/System/Library/CoreServices/Install Command Line Developer Tools.app"
CLT_INSTALLER_BUNDLE_ID="com.apple.dt.CommandLineTools.installondemand"
SOFTWARE_UPDATE_URL="x-apple.systempreferences:com.apple.Software-Update-Settings.extension"
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

dependency_pending() {
  printf '%s: PENDING: %s\n' "$PROGRAM_NAME" "$*" >&2
  exit "$EXIT_PARTIAL"
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
  (
  # git -C does not override inherited repository/configuration redirection.
  # Use a subshell so failures cannot alter the caller's environment.
  local git_env_name
  for git_env_name in "${!GIT_@}"; do
    unset "$git_env_name" || exit 1
  done
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET -u PYTHONPATH \
    -u CLAUDE_DESKTOP_CONFIG -u CLAUDE_DESKTOP_3P_CONFIG -u WORKBUDDY_APP_ROOT \
    -u WORKBUDDY_CONFIG -u WORKBUDDY_SKILLS_DIR \
    -u BASH_ENV -u ENV \
    AQG_BACKUP_DIR="${DEEPPATTERN_ROOT:-$HOME/.deeppattern}/aqg-backups" "$@"
  )
}

confirm_dependency_install() {
  local prompt="$1" answer=""
  printf '%s [Y/N] ' "$prompt" >/dev/tty
  if ! IFS= read -r answer </dev/tty; then
    return 1
  fi
  case "$answer" in
    y|Y|yes|Yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

HOMEBREW_BIN=""
find_homebrew() {
  local candidate
  HOMEBREW_BIN=""
  for candidate in \
      /opt/homebrew/bin/brew \
      /usr/local/bin/brew \
      "$(command -v brew 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate" ] || continue
    HOMEBREW_BIN="$candidate"
    return 0
  done
  return 1
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

GIT_BIN=""
GIT_VERSION=""
try_git() {
  local candidate="$1" version
  [ -n "$candidate" ] || return 1
  [ -x "$candidate" ] || return 1
  version="$(clean_exec "$candidate" --version 2>/dev/null || true)"
  if [[ ! "$version" =~ git[[:space:]]version[[:space:]]([0-9]+)\.([0-9]+) ]]; then
    return 1
  fi
  if (( BASH_REMATCH[1] < 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] < 36) )); then
    return 1
  fi
  GIT_BIN="$candidate"
  GIT_VERSION="$version"
  return 0
}

bootstrap_git_with_homebrew() {
  local git_prefix candidate
  find_homebrew || return 1
  tty_print "Git 2.36 or newer is missing, but Homebrew is available."
  confirm_dependency_install "Install or upgrade Git with Homebrew now?" \
    || { tty_print "Homebrew Git installation was declined; trying the Apple prerequisite path."; return 1; }
  if ! clean_exec "$HOMEBREW_BIN" install git; then
    tty_print "Homebrew could not install Git; trying the Apple prerequisite path."
    return 1
  fi
  git_prefix="$(clean_exec "$HOMEBREW_BIN" --prefix git 2>/dev/null || true)"
  candidate="$git_prefix/bin/git"
  if ! try_git "$candidate"; then
    tty_print "Homebrew finished, but $candidate is not a usable Git 2.36 or newer; trying the Apple prerequisite path."
    return 1
  fi
  tty_print "Git prerequisite ready: $GIT_VERSION ($GIT_BIN)"
}

present_clt_install_ui() {
  local info_plist="$CLT_INSTALLER_APP/Contents/Info.plist" bundle_id=""
  if [ -d "$CLT_INSTALLER_APP" ] && [ ! -L "$CLT_INSTALLER_APP" ] \
      && [ -f "$info_plist" ] && [ ! -L "$info_plist" ]; then
    bundle_id="$(
      /usr/bin/plutil -extract CFBundleIdentifier raw -o - "$info_plist" \
        2>/dev/null || true
    )"
    if [ "$bundle_id" = "$CLT_INSTALLER_BUNDLE_ID" ] \
        && clean_exec /usr/bin/open "$CLT_INSTALLER_APP"; then
      tty_print "Requested the Apple Command Line Tools installer window."
      return 0
    fi
  fi
  if clean_exec /usr/bin/open "$SOFTWARE_UPDATE_URL"; then
    tty_print "The dedicated installer window was unavailable; opened System Settings > Software Update instead."
    return 0
  fi
  tty_print "macOS accepted the Command Line Tools request but did not present an installation window."
  tty_print "Open System Settings > General > Software Update manually, complete the Command Line Tools installation, then rerun this command."
  return 1
}

bootstrap_git_prerequisite() {
  local clt_ready=0
  if [ -x "$XCODE_SELECT_BIN" ] \
      && clean_exec "$XCODE_SELECT_BIN" -p >/dev/null 2>&1; then
    clt_ready=1
    if try_git "/usr/bin/git"; then
      tty_print "Apple Command Line Tools Git prerequisite ready: $GIT_VERSION ($GIT_BIN)"
      return 0
    fi
  fi
  if find_homebrew; then
    tty_print "Trying the existing Homebrew installation before requesting Apple Command Line Tools."
    if bootstrap_git_with_homebrew; then
      return 0
    fi
  fi
  if [ "$clt_ready" -eq 0 ] && [ -x "$XCODE_SELECT_BIN" ]; then
    tty_print "Git 2.36 or newer is missing and Apple Command Line Tools are not installed."
    tty_print "Apple's installer will open. After it finishes, rerun this command."
    tty_print "Older macOS releases may also require a macOS or Command Line Tools update."
    confirm_dependency_install "Open Apple's Command Line Tools installer now?" \
      || fail "Git setup was declined; install Apple Command Line Tools and Git 2.36 or newer, then retry"
    clean_exec "$XCODE_SELECT_BIN" --install \
      || fail "Apple's Command Line Tools installer could not be opened; install it manually, then retry"
    present_clt_install_ui || true
    dependency_pending "Command Line Tools installation was requested. Complete the Apple installer or Software Update flow, then rerun this command."
  fi
  fail "Git 2.36 or newer is required. Update or repair Apple Command Line Tools, or make a trusted Git 2.36 or newer installation available, then retry."
}

try_git "$(command -v git 2>/dev/null || true)" \
  || bootstrap_git_prerequisite

python_candidate_usable() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  [ -x "$candidate" ] || return 1
  clean_exec "$candidate" -c \
    'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    </dev/null >/dev/null 2>&1 || return 1
  clean_exec "$candidate" -c 'import ssl, venv, tkinter' </dev/null >/dev/null 2>&1 || return 1
  clean_exec "$candidate" -m pip --version </dev/null >/dev/null 2>&1 || return 1
}

try_python() {
  local candidate="$1"
  python_candidate_usable "$candidate" || return 1
  PYTHON_BIN="$candidate"
  return 0
}

bootstrap_python_with_homebrew() {
  local python_prefix candidate
  find_homebrew \
    || fail "Homebrew is not installed. Install it from https://brew.sh, then rerun this installer to install Python 3.13 with Tk support."
  tty_print "Python 3.12 or newer with ssl, venv, tkinter, and pip is missing, but Homebrew is available."
  confirm_dependency_install "Install Python 3.13 and Tk support with Homebrew now?" \
    || fail "Python installation was declined; install Python 3.12 or newer with ssl, venv, tkinter, and pip, then retry"
  clean_exec "$HOMEBREW_BIN" install python@3.13 python-tk@3.13 \
    || fail "Homebrew could not install Python and Tk support; correct the reported Homebrew error, then retry"
  python_prefix="$(clean_exec "$HOMEBREW_BIN" --prefix python@3.13 2>/dev/null || true)"
  candidate="$python_prefix/bin/python3.13"
  try_python "$candidate" \
    || fail "Homebrew finished, but $candidate does not provide Python 3.12+ with ssl, venv, tkinter, and pip"
  tty_print "Python prerequisite ready: $PYTHON_BIN"
}

ensure_private_directory() {
  local path="$1" description="$2"
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -d "$path" ] && [ ! -L "$path" ] \
      || blocked "$path is not a regular $description directory; preserve it and stop"
    return 0
  fi
  clean_exec /bin/mkdir -m 700 "$path" \
    || fail "could not create the private $description directory at $path"
}

ensure_deeppattern_runtime_directories() {
  ensure_private_directory "$DEEPPATTERN_ROOT" "Deep Pattern"
  ensure_private_directory "$PRIVATE_RUNTIME_ROOT" "Deep Pattern runtime"
}

select_private_runtime_spec() {
  local machine_arch translated="0"
  machine_arch="$(/usr/bin/uname -m)"
  if [ -x /usr/sbin/sysctl ]; then
    translated="$(/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true)"
  fi
  if [ "$translated" = "1" ]; then
    machine_arch="arm64"
  fi

  case "$machine_arch" in
    arm64|aarch64)
      PRIVATE_RUNTIME_ARCH="aarch64"
      PRIVATE_RUNTIME_SHA256="b9054a9d3d54f4cb5573d44907fddb29874b08909bde73f29f2868cf872223ee"
      PRIVATE_RUNTIME_ASSET_SIZE="25293188"
      ;;
    x86_64|amd64)
      PRIVATE_RUNTIME_ARCH="x86_64"
      PRIVATE_RUNTIME_SHA256="49f0d97f506b855eed60b74a8ac138595c5b39799a6aa5e0d7ca8abe1019a4d4"
      PRIVATE_RUNTIME_ASSET_SIZE="25037769"
      ;;
    *)
      fail "unsupported Mac architecture for the private Python runtime: $machine_arch"
      ;;
  esac

  PRIVATE_RUNTIME_ID="cpython-$PRIVATE_PYTHON_VERSION+$PRIVATE_PYTHON_BUILD-$PRIVATE_RUNTIME_ARCH-apple-darwin"
  PRIVATE_RUNTIME_ASSET="$PRIVATE_RUNTIME_ID-install_only.tar.gz"
  PRIVATE_RUNTIME_URL="$PRIVATE_PYTHON_BASE_URL/${PRIVATE_RUNTIME_ASSET/+/%2B}"
  PRIVATE_RUNTIME_DIR="$PRIVATE_RUNTIME_ROOT/$PRIVATE_RUNTIME_ID"
  PRIVATE_RUNTIME_BIN="$PRIVATE_RUNTIME_DIR/python/bin/python3"
  PRIVATE_RUNTIME_MARKER="$PRIVATE_RUNTIME_DIR/$PRIVATE_RUNTIME_MARKER_NAME"
}

runtime_marker_matches() {
  [ -f "$PRIVATE_RUNTIME_MARKER" ] && [ ! -L "$PRIVATE_RUNTIME_MARKER" ] \
    && /usr/bin/grep -Fxq "schema=1" "$PRIVATE_RUNTIME_MARKER" \
    && /usr/bin/grep -Fxq "runtime_id=$PRIVATE_RUNTIME_ID" "$PRIVATE_RUNTIME_MARKER" \
    && /usr/bin/grep -Fxq "sha256=$PRIVATE_RUNTIME_SHA256" "$PRIVATE_RUNTIME_MARKER"
}

private_runtime_is_usable() {
  [ -d "$PRIVATE_RUNTIME_DIR" ] && [ ! -L "$PRIVATE_RUNTIME_DIR" ] \
    && runtime_marker_matches \
    && python_candidate_usable "$PRIVATE_RUNTIME_BIN"
}

next_runtime_backup_path() {
  local label="$1" stamp candidate suffix=0
  ensure_private_directory "$PRIVATE_RUNTIME_BACKUP_ROOT" "Deep Pattern runtime backup"
  stamp="$(/bin/date '+%Y%m%d-%H%M%S')"
  candidate="$PRIVATE_RUNTIME_BACKUP_ROOT/$stamp-$label"
  while [ -e "$candidate" ] || [ -L "$candidate" ]; do
    suffix=$((suffix + 1))
    candidate="$PRIVATE_RUNTIME_BACKUP_ROOT/$stamp-$suffix-$label"
  done
  printf '%s\n' "$candidate"
}

quarantine_runtime_path() {
  local source="$1" label="$2" destination
  [ -e "$source" ] || [ -L "$source" ] || return 0
  [ -d "$source" ] && [ ! -L "$source" ] \
    || blocked "$source is not a regular Deep Pattern runtime directory; preserve it and stop"
  destination="$(next_runtime_backup_path "$label")" \
    || fail "could not allocate a private runtime backup path"
  clean_exec /bin/mv "$source" "$destination" \
    || fail "could not preserve $source at $destination"
  tty_print "Preserved the previous runtime state at $destination."
}

download_private_python_runtime() {
  local stage archive extract listing actual_size actual_sha member unsafe=0

  ensure_deeppattern_runtime_directories
  select_private_runtime_spec
  if [ -e "$PRIVATE_RUNTIME_DIR" ] || [ -L "$PRIVATE_RUNTIME_DIR" ]; then
    if private_runtime_is_usable; then
      PRIVATE_BASE_PYTHON="$PRIVATE_RUNTIME_BIN"
      tty_print "Reusing Deep Pattern private Python $PRIVATE_PYTHON_VERSION at $PRIVATE_RUNTIME_DIR."
      return 0
    fi
    confirm_dependency_install "The Deep Pattern private Python runtime is incomplete or invalid. Preserve it and download a verified replacement?" \
      || return 1
    quarantine_runtime_path "$PRIVATE_RUNTIME_DIR" "private-python-runtime"
  else
    tty_print "Deep Pattern requires its verified private Python $PRIVATE_PYTHON_VERSION runtime."
    tty_print "Downloading about 25 MB into $PRIVATE_RUNTIME_ROOT without changing system Python."
  fi

  [ -x /usr/bin/curl ] || {
    tty_print "The system curl executable is unavailable; private Python cannot be downloaded."
    return 1
  }
  [ -x /usr/bin/openssl ] || {
    tty_print "The system OpenSSL executable is unavailable; the Python download cannot be verified."
    return 1
  }
  [ -x /usr/bin/tar ] || {
    tty_print "The system tar executable is unavailable; private Python cannot be unpacked."
    return 1
  }

  stage="$(clean_exec /usr/bin/mktemp -d "$PRIVATE_RUNTIME_ROOT/.python-runtime-stage.XXXXXX")" \
    || fail "could not create a private Python staging directory"
  archive="$stage/$PRIVATE_RUNTIME_ASSET"
  extract="$stage/extract"
  listing="$stage/archive-members.txt"
  clean_exec /bin/mkdir -m 700 "$extract" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not prepare private Python extraction"; }

  tty_print "Downloading Deep Pattern private Python $PRIVATE_PYTHON_VERSION for $PRIVATE_RUNTIME_ARCH..."
  if ! clean_exec /usr/bin/curl --fail --location --show-error --progress-bar \
      --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 2 \
      --output "$archive" "$PRIVATE_RUNTIME_URL"; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "Private Python download failed; no existing runtime was overwritten."
    return 1
  fi

  actual_size="$(/usr/bin/stat -f '%z' "$archive" 2>/dev/null || true)"
  actual_sha="$(/usr/bin/openssl dgst -sha256 "$archive" 2>/dev/null | /usr/bin/awk '{print $NF}')"
  if [ "$actual_size" != "$PRIVATE_RUNTIME_ASSET_SIZE" ] \
      || [ "$actual_sha" != "$PRIVATE_RUNTIME_SHA256" ]; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "Private Python download failed its fixed size or SHA-256 check; nothing was installed."
    return 1
  fi

  if ! clean_exec /usr/bin/tar -tzf "$archive" >"$listing"; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "Private Python archive could not be inspected; nothing was installed."
    return 1
  fi
  while IFS= read -r member; do
    [ -n "$member" ] || continue
    case "$member" in
      python|python/|python/*) ;;
      *) unsafe=1; break ;;
    esac
    case "/$member/" in
      *"/../"*) unsafe=1; break ;;
    esac
  done <"$listing"
  if [ "$unsafe" -ne 0 ] || [ ! -s "$listing" ]; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "Private Python archive has an unexpected path layout; nothing was installed."
    return 1
  fi
  if ! clean_exec /usr/bin/tar -xzf "$archive" -C "$extract"; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "Private Python archive extraction failed; nothing was installed."
    return 1
  fi
  if [ ! -d "$extract/python" ] || [ -L "$extract/python" ] \
      || [ -n "$(/usr/bin/find "$extract" -mindepth 1 -maxdepth 1 ! -name python -print -quit)" ] \
      || ! python_candidate_usable "$extract/python/bin/python3"; then
    clean_exec /bin/rm -rf "$stage"
    tty_print "The verified private Python archive does not provide ssl, venv, tkinter, and pip on this Mac."
    return 1
  fi

  clean_exec /bin/mkdir -m 700 "$stage/runtime" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not prepare the private Python runtime"; }
  clean_exec /bin/mv "$extract/python" "$stage/runtime/python" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not stage the private Python runtime"; }
  printf '%s\n' \
    "schema=1" \
    "runtime_id=$PRIVATE_RUNTIME_ID" \
    "python_version=$PRIVATE_PYTHON_VERSION" \
    "architecture=$PRIVATE_RUNTIME_ARCH" \
    "source=$PRIVATE_RUNTIME_URL" \
    "sha256=$PRIVATE_RUNTIME_SHA256" \
    >"$stage/runtime/$PRIVATE_RUNTIME_MARKER_NAME" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not record private Python ownership"; }
  /bin/chmod 600 "$stage/runtime/$PRIVATE_RUNTIME_MARKER_NAME" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not protect private Python ownership metadata"; }
  if [ -e "$PRIVATE_RUNTIME_DIR" ] || [ -L "$PRIVATE_RUNTIME_DIR" ]; then
    clean_exec /bin/rm -rf "$stage"
    blocked "$PRIVATE_RUNTIME_DIR appeared during download; preserve it and retry"
  fi
  clean_exec /bin/mv "$stage/runtime" "$PRIVATE_RUNTIME_DIR" \
    || { clean_exec /bin/rm -rf "$stage"; fail "could not activate the private Python runtime"; }
  clean_exec /bin/rm -rf "$stage"
  private_runtime_is_usable \
    || blocked "$PRIVATE_RUNTIME_DIR was installed but failed final verification; preserve it and stop"
  PRIVATE_BASE_PYTHON="$PRIVATE_RUNTIME_BIN"
  tty_print "Deep Pattern private Python runtime ready at $PRIVATE_RUNTIME_DIR."
}

select_fallback_python() {
  PYTHON_BIN=""
  try_python "${DE_PYTHON:-}" \
    || try_python "$(command -v python3 2>/dev/null || true)" \
    || try_python "$(command -v python 2>/dev/null || true)" \
    || try_python "/opt/homebrew/bin/python3" \
    || try_python "/usr/local/bin/python3" \
    || try_python "/opt/anaconda3/bin/python" \
    || try_python "/opt/anaconda3/bin/python3" \
    || bootstrap_python_with_homebrew
  PRIVATE_BASE_PYTHON="$PYTHON_BIN"
  tty_print "Using $PRIVATE_BASE_PYTHON only to prepare the private Deep Pattern environment."
}

create_managed_python_environment() {
  local base_python="$1" partial_python_root=""

  ensure_private_directory "$DEEPPATTERN_ROOT" "Deep Pattern"
  clean_exec /bin/mkdir -m 700 "$MANAGED_PYTHON_ROOT" \
    || blocked "$MANAGED_PYTHON_ROOT appeared while preparing Python; preserve it and retry"
  partial_python_root="$MANAGED_PYTHON_ROOT"
  cleanup_partial_python() {
    if [ "$partial_python_root" = "$MANAGED_PYTHON_ROOT" ] \
        && [ -d "$partial_python_root" ] && [ ! -L "$partial_python_root" ]; then
      /bin/rm -rf -- "$partial_python_root"
    fi
  }
  trap 'cleanup_partial_python' EXIT
  trap 'cleanup_partial_python; exit 129' HUP
  trap 'cleanup_partial_python; exit 130' INT
  trap 'cleanup_partial_python; exit 143' TERM

  if ! clean_exec "$base_python" -m venv "$MANAGED_PYTHON_ROOT"; then
    fail "could not create the private Deep Pattern Python environment with $base_python"
  fi
  try_python "$MANAGED_PYTHON_BIN" \
    || fail "the private Deep Pattern Python environment is incomplete"
  printf '%s\n' \
    "schema=1" \
    "base_python=$base_python" \
    >"$MANAGED_PYTHON_ROOT/$MANAGED_PYTHON_MARKER_NAME" \
    || fail "could not record the private Deep Pattern Python environment"
  /bin/chmod 600 "$MANAGED_PYTHON_ROOT/$MANAGED_PYTHON_MARKER_NAME" \
    || fail "could not protect the private Deep Pattern Python metadata"

  partial_python_root=""
  trap - EXIT HUP INT TERM
  tty_print "Created the private Deep Pattern Python environment at $MANAGED_PYTHON_ROOT."
}

PYTHON_BIN=""
if try_python "$MANAGED_PYTHON_BIN"; then
  tty_print "Reusing the private Deep Pattern Python environment at $MANAGED_PYTHON_ROOT."
else
  if [ -e "$MANAGED_PYTHON_ROOT" ] || [ -L "$MANAGED_PYTHON_ROOT" ]; then
    [ -d "$MANAGED_PYTHON_ROOT" ] && [ ! -L "$MANAGED_PYTHON_ROOT" ] \
      || blocked "$MANAGED_PYTHON_ROOT is not a regular managed Python directory; preserve it and stop"
    confirm_dependency_install "The private Deep Pattern Python environment is unusable. Preserve it and rebuild it?" \
      || blocked "$MANAGED_PYTHON_ROOT was preserved; repair was declined"
    quarantine_runtime_path "$MANAGED_PYTHON_ROOT" "de-python"
  fi
  PRIVATE_BASE_PYTHON=""
  if [ -n "${DE_PYTHON:-}" ]; then
    python_candidate_usable "$DE_PYTHON" \
      || fail "DE_PYTHON does not provide Python 3.12+ with ssl, venv, tkinter, and pip"
    PRIVATE_BASE_PYTHON="$DE_PYTHON"
    tty_print "Using the explicitly selected DE_PYTHON only to prepare the private Deep Pattern environment."
  elif ! download_private_python_runtime; then
    tty_print "A verified private Python runtime could not be used. An existing compatible Python or optional Homebrew fallback will be checked."
    select_fallback_python
  fi
  create_managed_python_environment "$PRIVATE_BASE_PYTHON"
fi
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

trusted_git_from_source() {
  run_source_python -c \
    'from installer import updater
print(updater._resolve_git_executable(minimum_version=updater._MINIMUM_GIT_VERSION))'
}

select_trusted_git_prerequisite() {
  local selected=""
  if selected="$(trusted_git_from_source 2>/dev/null)" && [ -n "$selected" ]; then
    try_git "$selected" \
      || fail "the trusted Git selector returned an unusable Git executable"
    tty_print "Trusted Git prerequisite ready: $GIT_VERSION ($GIT_BIN)"
    return 0
  fi

  bootstrap_git_prerequisite
  if selected="$(trusted_git_from_source 2>/dev/null)" && [ -n "$selected" ]; then
    try_git "$selected" \
      || fail "the trusted Git selector returned an unusable Git executable after dependency setup"
    tty_print "Trusted Git prerequisite ready: $GIT_VERSION ($GIT_BIN)"
    return 0
  fi
  fail "Git 2.36 or newer is not available in a trusted system location accepted by the signed installer; install or repair system Git, then retry"
}

select_trusted_git_prerequisite

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

update_existing_managed_root() {
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
        if recovery is not None and recovery.status == "deferred_active_session":
            result = recovery
        elif recovery is not None and recovery.status in {
            "repair_required",
            "retry_pending",
            "skipped_locked",
        }:
            raise ShellError(f"managed update recovery returned {recovery.status}")
        else:
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
deferred_statuses = {"deferred_active_session"}
blocker_pids = ",".join(
    str(blocker.pid) for blocker in result.blockers if blocker.pid is not None
)
print(f"{result.status}\t{blocker_pids}")
if result.status not in accepted_statuses | deferred_statuses:
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

parse_managed_update_result() {
  local raw="$1"
  managed_update_status=""
  managed_update_blocker_pids=""
  case "$raw" in
    *$'\t'*)
      managed_update_status="${raw%%$'\t'*}"
      managed_update_blocker_pids="${raw#*$'\t'}"
      ;;
    *)
      managed_update_status="$raw"
      ;;
  esac
  [[ "$managed_update_status" =~ ^[a-z_]+$ ]] || return 1
  if [ -n "$managed_update_blocker_pids" ]; then
    [[ "$managed_update_blocker_pids" =~ ^[0-9]+(,[0-9]+)*$ ]] || return 1
  fi
}

process_field() {
  local pid="$1" field="$2"
  [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] || return 1
  case "$field" in
    uid|ppid|command)
      /bin/ps -p "$pid" -o "$field=" 2>/dev/null \
        | /usr/bin/sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
      ;;
    *) return 1 ;;
  esac
}

likely_host_for_pid() {
  local current="$1" command parent depth=0
  while [[ "$current" =~ ^[0-9]+$ ]] && [ "$current" -gt 1 ] \
      && [ "$depth" -lt 8 ]; do
    command="$(process_field "$current" command || true)"
    case "$command" in
      *"/ChatGPT.app/Contents/Resources/codex"*) printf 'Codex (ChatGPT.app)\n'; return 0 ;;
      *"/Codex.app/"*) printf 'Codex\n'; return 0 ;;
      *"/Claude.app/"*|*"/Claude Desktop.app/"*) printf 'Claude Desktop\n'; return 0 ;;
      *"/Cursor.app/"*) printf 'Cursor\n'; return 0 ;;
      *"/Qoder CN IDE.app/"*) printf 'Qoder CN IDE\n'; return 0 ;;
      *"/Qoder IDE.app/"*) printf 'Qoder IDE\n'; return 0 ;;
      *"/Qoder CN.app/"*) printf 'Qoder CN\n'; return 0 ;;
      *"/Qoder.app/"*) printf 'Qoder\n'; return 0 ;;
      *"/TRAE"*".app/"*) printf 'TRAE\n'; return 0 ;;
      *"/WorkBuddy AI.app/"*) printf 'WorkBuddy AI\n'; return 0 ;;
      *"/WorkBuddy.app/"*) printf 'WorkBuddy\n'; return 0 ;;
      *"/CodeBuddy"*".app/"*) printf 'CodeBuddy\n'; return 0 ;;
    esac
    parent="$(process_field "$current" ppid || true)"
    [[ "$parent" =~ ^[0-9]+$ ]] || break
    current="$parent"
    depth=$((depth + 1))
  done
  printf 'unknown Agent host\n'
}

describe_active_shim_sessions() {
  local values="$1" pid label parent
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    label="$(likely_host_for_pid "$pid")"
    parent="$(process_field "$pid" ppid || true)"
    [[ "$parent" =~ ^[0-9]+$ ]] || parent="unknown"
    printf '  PID=%s likely-host=%s parent-pid=%s\n' "$pid" "$label" "$parent"
  done <<<"$(printf '%s' "$values" | /usr/bin/tr ',' '\n')"
}

run_managed_update_once() {
  local raw result_status=0
  managed_update_status=""
  managed_update_blocker_pids=""
  if raw="$(update_existing_managed_root)"; then
    result_status=0
  else
    result_status=$?
  fi
  parse_managed_update_result "$raw" || {
    managed_update_status="unknown"
    managed_update_blocker_pids=""
    return 1
  }
  return "$result_status"
}

resume_managed_root=0
activated_repair_mode=0
de_update_deferred=0
deferred_update_sessions=""
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
    tty_print "Found a complete signed and activated Decision Engine install; checking the signed stable update and repairing host configuration without reopening activation."
  else
    tty_print "Found a complete signed Decision Engine stable install with activation pending; checking the signed stable update before resuming setup. Active MCP sessions will defer the update without blocking activation."
  fi
  resume_managed_root=1
fi

if [ "$resume_managed_root" -eq 1 ]; then
  activation_state_before_update="$existing_activation_state"
  config_digest_before_update="$(managed_config_digest)" \
    || blocked "could not fingerprint the Decision Engine configuration before update"
  tty_print "Checking for and applying the newest signed Decision Engine stable release..."
  managed_update_status=""
  managed_update_blocker_pids=""
  if ! run_managed_update_once; then
    if [ "$managed_update_status" = "deferred_active_session" ] \
        && [ -n "$managed_update_blocker_pids" ] \
        && validate_complete_managed_root \
        && [ "$(managed_activation_state)" = "$activation_state_before_update" ] \
        && [ "$(managed_config_digest)" = "$config_digest_before_update" ]; then
      de_update_deferred=1
      deferred_update_sessions="$(
        describe_active_shim_sessions "$managed_update_blocker_pids"
      )"
      tty_print "Active MCP sessions are using the current verified Decision Engine release. The signed update is deferred; setup will continue without stopping Agent applications."
    elif validate_complete_managed_root \
        && [ "$(managed_activation_state)" = "$activation_state_before_update" ] \
        && [ "$(managed_config_digest)" = "$config_digest_before_update" ]; then
      fail "signed stable update status ${managed_update_status:-unknown}; the existing release and activation state were verified and preserved"
    else
      blocked "signed stable update did not complete and the previous release could not be re-verified; preserve the managed root and use the owner-guided recovery flow"
    fi
  fi
  validate_complete_managed_root \
    || blocked "signed stable update returned $managed_update_status, but the managed release no longer validates"
  [ "$(managed_activation_state)" = "$activation_state_before_update" ] \
    || blocked "signed stable update returned $managed_update_status, but the activation state changed"
  [ "$(managed_config_digest)" = "$config_digest_before_update" ] \
    || blocked "signed stable update returned $managed_update_status, but the protected activation configuration changed"
  if [ "$de_update_deferred" -eq 0 ]; then
    tty_print "Decision Engine signed stable update status: $managed_update_status"
  fi
fi

catalog_root="$source_root"
if [ "$de_update_deferred" -eq 1 ]; then
  catalog_root="$MANAGED_ROOT"
fi
if ! source_detected_clients="$(
  cd "$catalog_root"
  de_exec "$PYTHON_BIN" -c \
    'from installer import mcp_config; print("\n".join(mcp_config.detect_clients()))' \
    | tr -d '\r'
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

AQG_LAYOUT=""
AQG_MANAGED_TARGET=""

verify_aqg_checkout() {
  local actual_remote current_sha managed_name managed_target status_output versions_root
  AQG_LAYOUT="regular"
  AQG_MANAGED_TARGET=""
  if [ -L "$AQG_ROOT" ]; then
    versions_root="$(dirname "$AQG_ROOT")/versions"
    [ -d "$versions_root" ] && [ ! -L "$versions_root" ] \
      || fail "$AQG_ROOT is a symlink without a regular sibling versions directory; preserve it and stop"
    managed_target="$(clean_exec "$PYTHON_BIN" -I -B - "$AQG_ROOT" "$versions_root" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
versions = Path(sys.argv[2])
try:
    target = root.resolve(strict=True)
    versions = versions.resolve(strict=True)
except OSError:
    raise SystemExit(1)
if (
    target.parent != versions
    or not target.is_dir()
):
    raise SystemExit(1)
if re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", target.name) is None:
    raise SystemExit(1)
print(target)
PY
)" || fail "$AQG_ROOT is not an AQG-managed versions/<release-or-commit> symlink; preserve it and stop"
    AQG_LAYOUT="managed"
    AQG_MANAGED_TARGET="$managed_target"
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
  current_sha="$(clean_exec "$GIT_BIN" -C "$AQG_ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
  [[ "$current_sha" =~ ^[0-9a-f]{40}$ ]] \
    || fail "$AQG_ROOT does not resolve to a full Git commit; preserve it and stop"
  if [ "$AQG_LAYOUT" = "managed" ]; then
    managed_name="${AQG_MANAGED_TARGET##*/}"
    if [[ "$managed_name" =~ ^[0-9a-f]{40}$ ]]; then
      [ "$managed_name" = "$current_sha" ] \
        || fail "$AQG_ROOT commit-named target does not match its checked-out commit; preserve it and stop"
    else
      clean_exec "$PYTHON_BIN" -I -B - "$AQG_ROOT/VERSION" "$managed_name" "$current_sha" <<'PY' \
        || fail "$AQG_ROOT release-named target does not match VERSION/HEAD or the official retry naming rule; preserve it and stop"
from pathlib import Path
import re
import sys

version = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
name, head = sys.argv[2:]
def release(value):
    return len(value) <= 40 and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", value
    ) is not None

# Mirror stage.version_name without invoking it (it inspects occupied paths).
reissue = f"{version}-{head[:12]}"
bases = {version if release(version) else head, reissue if release(reissue) else head}
legacy = name == version and re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?", version)
matches = legacy or name in bases or any(
    re.fullmatch(re.escape(base[:23]) + r"-[0-9a-f]{16}", name) for base in bases
)
raise SystemExit(0 if matches else 1)
PY
    fi
  fi
}

sync_aqg_checkout() {
  local aqg_archive target_sha
  verify_aqg_checkout
  if [ "$AQG_LAYOUT" = "managed" ]; then
    update_managed_aqg
    verify_aqg_checkout
    return 0
  fi
  tty_print "Synchronizing Agent Quality Gates from $AQG_REPO at $AQG_REF..."
  if [ "$AQG_LAYOUT" = "regular" ]; then
    clean_exec "$GIT_BIN" -C "$AQG_ROOT" config --local core.autocrlf false \
      || fail "could not pin LF-safe Git configuration for the AQG checkout; preserve it and stop"
    clean_exec "$GIT_BIN" -C "$AQG_ROOT" config --local core.eol lf \
      || fail "could not pin LF-safe Git configuration for the AQG checkout; preserve it and stop"
    aqg_archive="$(clean_exec mktemp)" \
      || fail "could not create a temporary AQG archive; preserve it and stop"
    if ! clean_exec "$GIT_BIN" -C "$AQG_ROOT" archive --output="$aqg_archive" HEAD \
        || ! clean_exec tar -xf "$aqg_archive" -C "$AQG_ROOT"; then
      clean_exec rm -f "$aqg_archive"
      fail "could not rematerialize LF-safe AQG files; preserve it and stop"
    fi
    clean_exec rm -f "$aqg_archive"
    clean_exec "$GIT_BIN" -C "$AQG_ROOT" add --update \
      || fail "could not refresh the LF-safe AQG index; preserve it and stop"
    if ! clean_exec "$GIT_BIN" -C "$AQG_ROOT" diff --cached --quiet HEAD --; then
      clean_exec "$GIT_BIN" -C "$AQG_ROOT" reset --quiet HEAD -- . || true
      fail "AQG files changed while line endings were normalized; preserve them and stop"
    fi
  fi
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

update_managed_aqg() {
  local status=0
  tty_print "Checking AQG signed stable through its transactional multi-host updater..."
  verify_aqg_checkout
  clean_exec env AQG_ROOT="$AQG_ROOT" "$PYTHON_BIN" -I -B - "$AQG_ROOT" "$AQG_REPO" <<'PY' || status=$?
from pathlib import Path
import sys

root = Path(sys.argv[1]).absolute()
sys.path.insert(0, str(root))
from scripts.aqg_update.run import check

result = check(root=root, remote=sys.argv[2], channel="stable", apply=True)
print(f"AQG signed update: status={result.outcome}; {result.detail}")
if result.pending:
    print("AQG host approvals remain pending: " + ", ".join(result.pending))
    raise SystemExit(4)
if result.outcome in {"current", "applied"}:
    raise SystemExit(0)
if result.outcome in {"too-soon", "disabled"}:
    print("AQG update check was skipped by updater policy; latest release is not confirmed.")
    raise SystemExit(0)
raise SystemExit(4 if result.outcome in {"pending", "busy", "deferred"} else 2)
PY
  case "$status" in
    0) verify_aqg_checkout ;;
    4) dependency_pending "AQG update needs attention; resolve the reported pending state and retry. No checkout reset was attempted." ;;
    *) fail "AQG signed update did not complete; inspect the status above. No checkout reset was attempted." ;;
  esac
}

aqg_backup_residue() {
  clean_exec "$PYTHON_BIN" -I -B - "$DEEPPATTERN_ROOT" "$@" <<'PY'
import os
from pathlib import Path
import stat
import sys
import tempfile

dp, action = Path(sys.argv[1]), sys.argv[2]
source = dp / "versions/aqg-backups"
destination = dp / "aqg-backups"

def directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(f"not a private, owned regular directory: {path}")
    return info

def identity():
    # Do not traverse backup contents. Rename preserves files and links intact.
    parts = []
    for path in (dp, source.parent, source):
        info = directory(path)
        parts.extend((info.st_dev, info.st_ino, info.st_mtime_ns))
    return ":".join(map(str, parts))

try:
    if action == "inspect" and not os.path.lexists(source):
        raise SystemExit(0)
    frozen = identity()
    if action == "inspect":
        print(frozen)
        raise SystemExit(0)
    if action != "move" or len(sys.argv) != 4 or frozen != sys.argv[3]:
        raise ValueError("backup directory identity changed after confirmation; nothing moved")
    if os.path.lexists(destination):
        directory(destination)
    else:
        destination.mkdir(mode=0o700)
    directory(destination)
    archive = Path(tempfile.mkdtemp(prefix="legacy-versions-", dir=destination))
    target = archive / "aqg-backups"
    try:
        # Parent metadata may change when the archive base is created; freeze
        # the source itself again immediately before the atomic rename.
        info = directory(source)
        expected = ":".join(map(str, (info.st_dev, info.st_ino, info.st_mtime_ns)))
        if expected != ":".join(frozen.split(":")[-3:]):
            raise ValueError("backup directory changed before move; nothing moved")
        directory(dp)
        directory(source.parent)
        directory(destination)
        os.rename(source, target)
    except BaseException:
        archive.rmdir()
        raise
    print(f"PRESERVE archived legacy AQG backups: {target}")
except (OSError, ValueError) as exc:
    print(f"AQG backup relocation refused: {exc}", file=sys.stderr)
    raise SystemExit(2)
PY
}

prepare_aqg_versions() {
  local identity
  identity="$(aqg_backup_residue inspect)" \
    || fail "could not safely inspect legacy AQG backups; nothing was moved"
  [ -n "$identity" ] || return 0
  tty_print "Historical AQG backups were found at $DEEPPATTERN_ROOT/versions/aqg-backups."
  tty_print "Automatically archiving them under $DEEPPATTERN_ROOT/aqg-backups; contents are preserved, not deleted or merged."
  aqg_backup_residue move "$identity" \
    || fail "legacy AQG backups could not be relocated safely; inspect the reason above and retry"
}

ensure_aqg_update_layout() {
  verify_aqg_checkout
  tty_print "Ensuring AQG uses its official managed-update version layout..."
  clean_exec env AQG_ROOT="$AQG_ROOT" "$PYTHON_BIN" -I -B - "$AQG_ROOT" <<'PY' \
    || fail "AQG managed-update layout was not established; resolve the migration reason above and retry"
from pathlib import Path
import sys

root = Path(sys.argv[1]).absolute()
sys.path.insert(0, str(root))
from scripts.aqg_update.migrate import ensure_managed_layout

result = ensure_managed_layout(root)
print(f"AQG layout: {result.reason}")
if not result.managed or not root.is_symlink():
    raise SystemExit(2)
PY
  verify_aqg_checkout
  [ "$AQG_LAYOUT" = "managed" ] \
    || fail "AQG did not produce a verified managed versions directory"
  tty_print "AQG managed entrance: $AQG_ROOT -> $AQG_MANAGED_TARGET"
  if [[ "${AQG_MANAGED_TARGET##*/}" =~ ^[0-9a-f]{40}$ ]]; then
    tty_print "AQG retained a legacy commit-named version; the official updater owns future version naming."
  fi
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

reconcile_codex_hooks_after_aqg_migration() {
  local active_root
  comma_list_contains "${aqg_selected_clients:-}" "codex" || return 0
  [ -L "$AQG_ROOT" ] || return 0

  verify_aqg_checkout
  [ "$AQG_LAYOUT" = "managed" ] && [ -n "$AQG_MANAGED_TARGET" ] || return 1
  active_root="$AQG_MANAGED_TARGET"

  tty_print "Rebinding Codex hooks to the stable AQG managed entrance..."
  clean_exec env \
    PATH="$PYTHON_DIR:$PATH" \
    AQG_BACKUP_DIR="$DEEPPATTERN_ROOT/aqg-backups" \
    "$PYTHON_BIN" "$active_root/scripts/install_aqg_codex_hooks.py" \
    --apply --aqg-root "$AQG_ROOT" || return 1
  clean_exec env PATH="$PYTHON_DIR:$PATH" \
    "$PYTHON_BIN" "$active_root/scripts/install_aqg_codex_hooks.py" \
    --verify --aqg-root "$AQG_ROOT"
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

prepare_aqg_versions

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
    print(client)' \
    | tr -d '\r'
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
ensure_aqg_update_layout
reconcile_codex_hooks_after_aqg_migration \
  || fail "AQG migrated to its managed layout, but Codex hooks could not be rebound to the managed entrance; Decision Engine was not installed"
converge_managed_claude_hooks \
  || fail "AQG-managed Claude hooks could not be converged safely; Decision Engine was not installed"
tty_print "Verifying AQG multi-host configuration..."
run_aqg_clients "verify" --verify \
  || fail "AQG multi-host verification failed; Decision Engine was not installed"
if ! clean_exec env PATH="$PYTHON_DIR:$PATH" \
    "$PYTHON_BIN" "$AQG_ROOT/scripts/aqg_doctor.py"; then
  fail "AQG Doctor failed after multi-host configuration; Decision Engine was not installed"
fi
verify_aqg_checkout
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
if [ -e "$CLAUDE_3P_CONFIG" ] || [ -L "$CLAUDE_3P_CONFIG" ]; then
  if [ -L "$CLAUDE_3P_CONFIG" ] || [ ! -f "$CLAUDE_3P_CONFIG" ]; then
    blocked \
      "$CLAUDE_3P_CONFIG is not a regular configuration file; preserve it and repair the Claude third-party profile before continuing"
  fi
  claude_3p_profile_detected=1
fi

detect_managed_clients() {
  run_managed_python -c \
    'from installer import mcp_config; print("\n".join(mcp_config.detect_clients()))' \
    | tr -d '\r'
}

if [ "$claude_3p_profile_detected" -eq 1 ]; then
  run_managed_python -c \
    'from installer import mcp_config; raise SystemExit(0 if "claude-desktop-3p" in mcp_config.CLIENTS else 1)' \
    </dev/null >/dev/null 2>&1 \
    || blocked "Decision Engine $managed_version does not support the configured Claude third-party profile"
fi

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
  if ! run_managed_python -c \
      'from installer import gui_setup; import sys; gui_setup.prepare_gui_environment(sys.executable)' \
      </dev/null; then
    tty_print "Warning: optional activation UI preparation failed; continuing with the existing activation fallback."
  fi
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

deferred_activation_state=""
if [ "$de_update_deferred" -eq 1 ]; then
  deferred_activation_state="$(managed_activation_state)" \
    || blocked "could not verify the final activation state after deferring the signed update"
  tty_print "Decision Engine deferred update report:"
  if [ "$deferred_activation_state" = "activated" ]; then
    tty_print "Activation: complete"
  else
    tty_print "Activation: pending"
  fi
  tty_print "DE update: deferred because Agent MCP sessions are active"
  tty_print "Active MCP sessions recorded when this update was deferred:"
  printf '%s\n' "$deferred_update_sessions" >/dev/tty
  tty_print "Fully quit every listed Agent application before reopening any of them."
  tty_print "The first new MCP session will retry the pending signed update."
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

if [ "$de_update_deferred" -eq 1 ]; then
  if [ "$deferred_activation_state" = "activated" ]; then
    printf '%s: SUCCESS_WITH_RESTART_REQUIRED: device activation and host configuration are complete; the current verified DE release was preserved and its signed update remains pending.\n' \
      "$PROGRAM_NAME"
    exit 0
  fi
  printf '%s: PARTIAL: device activation and the signed DE update remain pending; the current verified release and host configuration were preserved.\n' \
    "$PROGRAM_NAME" >&2
  exit "$EXIT_PARTIAL"
fi

printf '%s: PASS: all detected supported hosts are configured on disk; restart the host applications before runtime verification.\n' \
  "$PROGRAM_NAME"
