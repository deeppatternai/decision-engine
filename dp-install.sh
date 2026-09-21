#!/usr/bin/env bash
#
# Deep Pattern (DP) first-time installer for macOS and Linux.
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
APT_GET_BIN="/usr/bin/apt-get"
DPKG_BIN="/usr/bin/dpkg"
DPKG_QUERY_BIN="/usr/bin/dpkg-query"
DNF_BIN=""
RPM_BIN="/usr/bin/rpm"
SUDO_BIN="/usr/bin/sudo"
EXIT_USAGE=2
EXIT_BLOCKED=3
EXIT_PARTIAL=4
PLATFORM_KERNEL=""
PLATFORM_FAMILY=""
PLATFORM_DISPLAY_NAME=""
LINUX_DISTRO_ID=""
LINUX_VERSION_ID=""
LINUX_MACHINE_ARCH=""
PRIVATE_RUNTIME_DOWNLOAD_LABEL=""
LINUX_PACKAGEKIT_NOTICE_SHOWN=0

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

[ -x /usr/bin/uname ] \
  || fail "the trusted /usr/bin/uname tool is unavailable; platform identity cannot be verified"
PLATFORM_KERNEL="$(/usr/bin/uname -s 2>/dev/null || true)"
case "$PLATFORM_KERNEL" in
  Darwin) PLATFORM_FAMILY="macos"; PLATFORM_DISPLAY_NAME="this Mac" ;;
  Linux) PLATFORM_FAMILY="linux"; PLATFORM_DISPLAY_NAME="this Linux desktop" ;;
  *) fail "unsupported platform: ${PLATFORM_KERNEL:-unknown}; supported platforms are macOS and supported Ubuntu, Debian, or Fedora desktops" ;;
esac

read_linux_os_release() {
  local key value
  [ -r /etc/os-release ] \
    || fail "Linux installation requires a readable /etc/os-release; supported families are Ubuntu 22.04+, Debian 12+, and Fedora 44+"
  while IFS='=' read -r key value; do
    case "$key" in
      ID)
        value="${value#\"}"
        value="${value%\"}"
        LINUX_DISTRO_ID="$value"
        ;;
      VERSION_ID)
        value="${value#\"}"
        value="${value%\"}"
        LINUX_VERSION_ID="$value"
        ;;
    esac
  done </etc/os-release
}

linux_release_at_least() {
  local minimum="$1" major minor minimum_major minimum_minor
  case "$LINUX_VERSION_ID" in
    ''|*[!0-9.]*|*.*.*|.*|*.) return 1 ;;
  esac
  major="${LINUX_VERSION_ID%%.*}"
  minor="${LINUX_VERSION_ID#*.}"
  [ "$minor" != "$LINUX_VERSION_ID" ] || minor=0
  [ "${#major}" -le 3 ] && [ "${#minor}" -le 2 ] || return 1
  minimum_major="${minimum%%.*}"
  minimum_minor="${minimum#*.}"
  [ "$minimum_minor" != "$minimum" ] || minimum_minor=0
  [ "$major" -gt "$minimum_major" ] 2>/dev/null \
    || { [ "$major" -eq "$minimum_major" ] 2>/dev/null \
      && [ "$minor" -ge "$minimum_minor" ] 2>/dev/null; }
}

preflight_linux_platform() {
  local libc_version machine_arch user_id
  read_linux_os_release
  case "$LINUX_DISTRO_ID" in
    ubuntu) linux_release_at_least 22.04 || fail "unsupported Ubuntu release: ${LINUX_VERSION_ID:-unknown}; Ubuntu 22.04 or newer is required" ;;
    debian) linux_release_at_least 12 || fail "unsupported Debian release: ${LINUX_VERSION_ID:-unknown}; Debian 12 or newer is required" ;;
    fedora) linux_release_at_least 44 || fail "unsupported Fedora release: ${LINUX_VERSION_ID:-unknown}; Fedora 44 or newer is required" ;;
    *) fail "unsupported Linux distribution: ${LINUX_DISTRO_ID:-unknown}; supported families are Ubuntu, Debian, and Fedora" ;;
  esac
  [ -x /usr/bin/id ] \
    || fail "the trusted /usr/bin/id tool is unavailable; the desktop user identity cannot be verified"
  user_id="$(/usr/bin/id -u 2>/dev/null || true)"
  [ -n "$user_id" ] && [ "$user_id" != "0" ] \
    || fail "do not run the entire installer with sudo or as root; run it as the intended desktop user and approve only the displayed system dependency step"
  [ -x /usr/bin/getconf ] \
    || fail "the trusted /usr/bin/getconf tool is unavailable; glibc compatibility cannot be verified"
  libc_version="$(/usr/bin/getconf GNU_LIBC_VERSION 2>/dev/null || true)"
  case "$libc_version" in
    glibc\ *) ;;
    *) fail "unsupported Linux C library: ${libc_version:-unknown}; this prototype requires glibc" ;;
  esac
  machine_arch="$(/usr/bin/uname -m 2>/dev/null || true)"
  case "$machine_arch" in
    x86_64|amd64|aarch64|arm64) ;;
    *) fail "unsupported Linux architecture: ${machine_arch:-unknown}; expected x86_64 or aarch64" ;;
  esac
  LINUX_MACHINE_ARCH="$machine_arch"
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    fail "a Linux desktop session is required for masked activation; DISPLAY and WAYLAND_DISPLAY are both unset"
  fi
}

if [ "$PLATFORM_FAMILY" = "linux" ]; then
  preflight_linux_platform
fi

# curl | bash has a pipe on stdin. Read all interactive decisions from the
# controlling terminal so the pipe never becomes an accidental prompt source.
# The activation window is the only product decision made during this command.
if [ ! -r /dev/tty ] || [ ! -w /dev/tty ] || ! ( : </dev/tty ) 2>/dev/null; then
  fail "an interactive terminal is required for activation"
fi

tty_print() {
  printf '%s\n' "$*" >/dev/tty
}

clean_exec() {
  (
  # git -C does not override inherited repository/configuration redirection.
  # Use a subshell so failures cannot alter the caller's environment.
  local git_env_name
  if [ -n "${BASH_VERSION:-}" ]; then
    for git_env_name in "${!GIT_@}"; do
      unset "$git_env_name" || exit 1
    done
  elif [ -n "${ZSH_VERSION:-}" ]; then
    eval 'for git_env_name in ${(k)parameters}; do
      case "$git_env_name" in
        GIT_*) unset "$git_env_name" || exit 1 ;;
      esac
    done'
  else
    fail "run this installer with Bash or Zsh so inherited Git redirection can be cleared safely"
  fi
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
if [ "$PLATFORM_FAMILY" = "macos" ] \
    && [ -x "$WORKBUDDY_STANDARD_APP/Contents/MacOS/Electron" ] \
    && [ ! -L "$WORKBUDDY_STANDARD_APP" ] \
    && [ ! -L "$WORKBUDDY_STANDARD_APP/Contents/MacOS/Electron" ]; then
  workbuddy_variant="standard"
elif [ "$PLATFORM_FAMILY" = "macos" ] \
    && { [ -e "$WORKBUDDY_AI_APP" ] || [ -L "$WORKBUDDY_AI_APP" ]; }; then
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
  if [ "${absent_claude_route:-0}" -eq 1 ]; then
    set -- env CLAUDE_SKILLS_DIR="$tmp_root/absent-claude-skills" "$@"
  fi
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

bootstrap_git_prerequisite_macos() {
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

linux_native_popup_supported() {
  case "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" in
    ubuntu:24.04|ubuntu:26.04|fedora:44) return 0 ;;
    ubuntu:22.04|debian:12)
      case "$LINUX_MACHINE_ARCH" in
        x86_64|amd64) return 0 ;;
        *) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

linux_debian_arm64_gtk_supported() {
  [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "debian:12" ] || return 1
  case "$LINUX_MACHINE_ARCH" in
    aarch64|arm64) return 0 ;;
    *) return 1 ;;
  esac
}

linux_debian_package_installed() {
  local package="$1" status=""
  [ -x "$DPKG_QUERY_BIN" ] || return 1
  status="$(clean_exec "$DPKG_QUERY_BIN" -W -f='${Status}' "$package" 2>/dev/null || true)"
  [ "$status" = "install ok installed" ]
}

linux_debian_arm64_gtk_dependency_packages() {
  local packages="" package
  linux_debian_arm64_gtk_supported || return 0
  for package in \
      build-essential \
      pkg-config \
      libcairo2-dev \
      libgirepository1.0-dev \
      gir1.2-gtk-3.0 \
      gir1.2-webkit2-4.1; do
    linux_debian_package_installed "$package" || packages="$packages $package"
  done
  printf '%s' "${packages# }"
}

linux_debian_arm64_gtk_dependencies_ready() {
  [ -z "$(linux_debian_arm64_gtk_dependency_packages)" ]
}

linux_ca_bundle_available() {
  local ca_bundle
  for ca_bundle in \
      /etc/ssl/certs/ca-certificates.crt \
      /etc/pki/tls/certs/ca-bundle.crt; do
    [ -s "$ca_bundle" ] && return 0
  done
  if [ "$LINUX_DISTRO_ID" = "fedora" ] \
      && [ -x "$RPM_BIN" ] \
      && clean_exec "$RPM_BIN" -q --quiet ca-certificates \
        </dev/null >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

linux_shared_library_available() {
  local library="$1" ldconfig_bin="" line
  for ldconfig_bin in /usr/sbin/ldconfig /sbin/ldconfig; do
    [ -x "$ldconfig_bin" ] || continue
    while IFS= read -r line; do
      case "$line" in
        *"$library "*) return 0 ;;
      esac
    done < <(clean_exec "$ldconfig_bin" -p 2>/dev/null || true)
    return 1
  done
  return 1
}

linux_apt_dependency_packages() {
  local packages="" minizip_package="" gtk_packages=""

  if ! try_git "/usr/bin/git"; then
    packages="$packages git"
  fi
  [ -x /usr/bin/curl ] || packages="$packages curl"
  linux_ca_bundle_available || packages="$packages ca-certificates"

  if linux_native_popup_supported; then
    case "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" in
      ubuntu:24.04|ubuntu:26.04) minizip_package="libminizip1t64" ;;
      ubuntu:22.04|debian:12) minizip_package="libminizip1" ;;
    esac
    linux_shared_library_available "libminizip.so.1" \
      || packages="$packages $minizip_package"
    linux_shared_library_available "libxcb-cursor.so.0" \
      || packages="$packages libxcb-cursor0"
  fi
  gtk_packages="$(linux_debian_arm64_gtk_dependency_packages)"
  [ -z "$gtk_packages" ] || packages="$packages $gtk_packages"

  printf '%s' "${packages# }"
}

linux_dnf_dependency_packages() {
  local packages=""

  if ! try_git "/usr/bin/git"; then
    packages="$packages git"
  fi
  [ -x /usr/bin/curl ] || packages="$packages curl"
  linux_ca_bundle_available || packages="$packages ca-certificates"
  if linux_native_popup_supported; then
    [ -x /usr/bin/ar ] || packages="$packages binutils"
    [ -x /usr/bin/zstd ] || packages="$packages zstd"
    linux_shared_library_available "libxcb-cursor.so.0" \
      || packages="$packages xcb-util-cursor"
  fi

  printf '%s' "${packages# }"
}

select_linux_dnf_bin() {
  local candidate
  DNF_BIN=""
  for candidate in /usr/bin/dnf5 /usr/bin/dnf; do
    [ -x "$candidate" ] || continue
    DNF_BIN="$candidate"
    return 0
  done
  return 1
}

print_linux_apt_manual_command() {
  local packages="$1"
  tty_print "Run these commands with your system administrator's approval, then rerun this installer:"
  tty_print "  sudo apt-get update"
  tty_print "  sudo apt-get install --no-install-recommends $packages"
}

linux_dpkg_state_ready() {
  local audit_output=""

  [ -x "$DPKG_BIN" ] || {
    tty_print "The trusted dpkg executable is unavailable at $DPKG_BIN."
    return 1
  }
  if ! audit_output="$(clean_exec "$DPKG_BIN" --audit 2>&1)"; then
    tty_print "The Debian package database could not be audited safely."
    [ -z "$audit_output" ] || printf '%s\n' "$audit_output" >/dev/tty
    return 1
  fi
  if [ -n "$audit_output" ]; then
    tty_print "The Debian package database has unfinished work from an earlier package operation:"
    printf '%s\n' "$audit_output" >/dev/tty
    return 1
  fi
  return 0
}

print_linux_dpkg_repair_commands() {
  tty_print "No sudo or APT command was run by this installer."
  tty_print "Finish the existing package transaction with your system administrator's approval, then rerun this installer:"
  tty_print "  sudo dpkg --configure -a"
  tty_print "  sudo apt-get -f install"
}

filter_linux_apt_stderr() {
  local line="" packagekit_notice=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      'Error: GDBus.Error:org.freedesktop.systemd1.UnitMasked: Unit packagekit.service is masked.')
        packagekit_notice=1
        ;;
      *) printf '%s\n' "$line" >&2 ;;
    esac
  done
  if [ "$packagekit_notice" -eq 1 ] \
      && [ "$LINUX_PACKAGEKIT_NOTICE_SHOWN" -eq 0 ]; then
    tty_print "NOTE: PackageKit desktop cache refresh is disabled; APT package operations do not depend on this service."
    LINUX_PACKAGEKIT_NOTICE_SHOWN=1
  fi
}

run_linux_apt_command() {
  local apt_stderr="" apt_status=0
  apt_stderr="$(clean_exec /usr/bin/mktemp /tmp/dp-install-apt-stderr.XXXXXX)" \
    || { tty_print "A secure temporary APT diagnostics file could not be created."; return 1; }
  clean_exec "$@" 2>"$apt_stderr" || apt_status=$?
  filter_linux_apt_stderr <"$apt_stderr"
  clean_exec /bin/rm -f "$apt_stderr" || true
  return "$apt_status"
}

install_linux_apt_packages() {
  local packages="$1" prompt="Install these dependencies now?" package

  [ -n "$packages" ] || return 0
  tty_print "Deep Pattern requires the following $LINUX_DISTRO_ID $LINUX_VERSION_ID system package(s):"
  for package in $packages; do
    case "$package" in
      git|curl|ca-certificates|libminizip1|libminizip1t64|libxcb-cursor0 \
        |build-essential|pkg-config|libcairo2-dev|libgirepository1.0-dev \
        |gir1.2-gtk-3.0|gir1.2-webkit2-4.1) ;;
      *) fail "refusing an unrecognized Linux dependency package: $package" ;;
    esac
    tty_print "  $package"
  done
  tty_print "APT package metadata will be refreshed and sudo authorization is required."
  tty_print "No repository configuration will be changed. No full system or distribution upgrade command will be used."
  if ! linux_dpkg_state_ready; then
    print_linux_dpkg_repair_commands
    return 1
  fi
  if ! confirm_dependency_install "$prompt"; then
    tty_print "Linux dependency installation was declined; no package manager was invoked."
    print_linux_apt_manual_command "$packages"
    return 1
  fi

  [ -x "$APT_GET_BIN" ] || {
    tty_print "The trusted APT executable is unavailable at $APT_GET_BIN."
    print_linux_apt_manual_command "$packages"
    return 1
  }
  [ -x "$SUDO_BIN" ] || {
    tty_print "The trusted sudo executable is unavailable at $SUDO_BIN."
    print_linux_apt_manual_command "$packages"
    return 1
  }

  tty_print "Requesting sudo authorization for the configured APT package installation..."
  if ! clean_exec "$SUDO_BIN" -v </dev/tty; then
    tty_print "Sudo authorization was not granted; no APT command was run."
    print_linux_apt_manual_command "$packages"
    return 1
  fi
  tty_print "Refreshing the configured APT package metadata..."
  if ! run_linux_apt_command "$SUDO_BIN" "$APT_GET_BIN" update </dev/tty; then
    tty_print "APT metadata refresh failed; no dependency installation was attempted."
    print_linux_apt_manual_command "$packages"
    return 1
  fi

  set --
  for package in $packages; do
    set -- "$@" "$package"
  done
  tty_print "Installing the approved Linux dependencies..."
  if ! run_linux_apt_command "$SUDO_BIN" "$APT_GET_BIN" -o APT::Get::AllowUnauthenticated=false install --no-install-recommends -y "$@" </dev/tty; then
    tty_print "APT did not complete successfully and may have made partial package changes."
    print_linux_apt_manual_command "$packages"
    return 1
  fi
  tty_print "The approved Linux system dependencies were installed."
}

print_linux_dnf_manual_command() {
  local packages="$1" dnf_command="dnf"
  select_linux_dnf_bin && dnf_command="$DNF_BIN"
  tty_print "Run this command with your system administrator's approval, then rerun this installer:"
  tty_print "  sudo $dnf_command --refresh --setopt=install_weak_deps=False install $packages"
}

linux_rpm_state_ready() {
  [ -x "$RPM_BIN" ] || {
    tty_print "The trusted RPM executable is unavailable at $RPM_BIN."
    return 1
  }
  if ! clean_exec "$RPM_BIN" --verifydb </dev/null >/dev/null 2>&1; then
    tty_print "The RPM package database did not pass its read-only integrity check."
    tty_print "No sudo or DNF command was run by this installer."
    return 1
  fi
  return 0
}

install_linux_dnf_packages() {
  local packages="$1" prompt="Install these dependencies now?" package

  [ -n "$packages" ] || return 0
  tty_print "Deep Pattern requires the following Fedora 44 system package(s):"
  for package in $packages; do
    case "$package" in
      git|curl|ca-certificates|binutils|zstd|xcb-util-cursor) ;;
      *) fail "refusing an unrecognized Fedora dependency package: $package" ;;
    esac
    tty_print "  $package"
  done
  tty_print "DNF will use the system's configured repositories and sudo authorization is required."
  tty_print "No repository configuration will be changed. No full system or distribution upgrade command will be used."
  if ! linux_rpm_state_ready; then
    tty_print "Repair the RPM database with your system administrator, then rerun this installer."
    return 1
  fi
  if ! confirm_dependency_install "$prompt"; then
    tty_print "Linux dependency installation was declined; no package manager was invoked."
    print_linux_dnf_manual_command "$packages"
    return 1
  fi
  if ! select_linux_dnf_bin; then
    tty_print "A trusted DNF executable is unavailable at /usr/bin/dnf5 or /usr/bin/dnf."
    print_linux_dnf_manual_command "$packages"
    return 1
  fi
  [ -x "$SUDO_BIN" ] || {
    tty_print "The trusted sudo executable is unavailable at $SUDO_BIN."
    print_linux_dnf_manual_command "$packages"
    return 1
  }

  tty_print "Requesting sudo authorization for the configured DNF package installation..."
  if ! clean_exec "$SUDO_BIN" -v </dev/tty; then
    tty_print "Sudo authorization was not granted; no DNF command was run."
    print_linux_dnf_manual_command "$packages"
    return 1
  fi
  tty_print "Refreshing the configured DNF package metadata..."
  if ! clean_exec "$SUDO_BIN" "$DNF_BIN" makecache --refresh </dev/tty; then
    tty_print "DNF metadata refresh failed; no dependency installation was attempted."
    print_linux_dnf_manual_command "$packages"
    return 1
  fi

  set --
  for package in $packages; do
    set -- "$@" "$package"
  done
  tty_print "Installing the approved Linux dependencies..."
  if ! clean_exec "$SUDO_BIN" "$DNF_BIN" -y --setopt=install_weak_deps=False install "$@" </dev/tty; then
    tty_print "DNF did not complete successfully and may have made partial package changes."
    print_linux_dnf_manual_command "$packages"
    return 1
  fi
  tty_print "The approved Linux system dependencies were installed."
}

linux_core_dependencies_ready() {
  try_git "/usr/bin/git" \
    && [ -x /usr/bin/curl ] \
    && linux_ca_bundle_available
}

linux_git_prerequisite() {
  local packages="" package_manager=""

  if [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "ubuntu:22.04" ] \
      && ! try_git "/usr/bin/git"; then
    fail "Ubuntu 22.04's default repository is older than the required Git 2.36; install Git 2.36 or newer from an approved source, then rerun this installer; no package manager or repository was changed automatically"
  fi

  case "$LINUX_DISTRO_ID" in
    ubuntu|debian)
      packages="$(linux_apt_dependency_packages)"
      package_manager="APT"
      if [ -n "$packages" ]; then
        install_linux_apt_packages "$packages" || true
      fi
      ;;
    fedora)
      packages="$(linux_dnf_dependency_packages)"
      package_manager="DNF"
      if [ -n "$packages" ]; then
        install_linux_dnf_packages "$packages" || true
      fi
      ;;
  esac
  if [ -n "$packages" ] && ! linux_core_dependencies_ready; then
    linux_core_dependencies_ready \
      || fail "required Linux dependencies are unavailable; install the packages shown above, then rerun this installer"
  fi
  if [ -n "$packages" ]; then
    if [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "fedora:44" ] \
        && { [ ! -x /usr/bin/ar ] \
          || [ ! -x /usr/bin/zstd ] \
          || ! linux_shared_library_available "libxcb-cursor.so.0"; }; then
      tty_print "Core Linux prerequisites are ready; optional native visual-window dependencies remain unavailable."
    elif [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" != "fedora:44" ] \
        && linux_native_popup_supported \
        && { ! linux_shared_library_available "libminizip.so.1" \
          || ! linux_shared_library_available "libxcb-cursor.so.0"; }; then
      tty_print "Core Linux prerequisites are ready; optional native visual-window dependencies remain unavailable."
    elif linux_debian_arm64_gtk_supported \
        && ! linux_debian_arm64_gtk_dependencies_ready; then
      tty_print "Core Linux prerequisites are ready; optional Debian ARM64 GTK build dependencies remain unavailable."
    fi
  fi

  linux_core_dependencies_ready \
    || fail "$package_manager completed, but Git 2.36+, curl, or the system CA bundle is still unavailable"
  tty_print "Linux Git prerequisite ready: $GIT_VERSION ($GIT_BIN)"
}

bootstrap_git_prerequisite() {
  if [ "$PLATFORM_FAMILY" = "macos" ]; then
    bootstrap_git_prerequisite_macos
  else
    linux_git_prerequisite
  fi
}

if [ "$PLATFORM_FAMILY" = "macos" ]; then
  try_git "$(command -v git 2>/dev/null || true)" \
    || bootstrap_git_prerequisite_macos
else
  linux_git_prerequisite
fi

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
  if [ "$PLATFORM_FAMILY" = "macos" ] && [ -x /usr/sbin/sysctl ]; then
    translated="$(/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true)"
  fi
  if [ "$translated" = "1" ]; then
    machine_arch="arm64"
  fi

  if [ "$PLATFORM_FAMILY" = "macos" ]; then
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
      *) fail "unsupported Mac architecture for the private Python runtime: $machine_arch" ;;
    esac
    PRIVATE_RUNTIME_PLATFORM="apple-darwin"
    PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 25 MB"
  else
    case "$machine_arch" in
      arm64|aarch64)
        PRIVATE_RUNTIME_ARCH="aarch64"
        PRIVATE_RUNTIME_SHA256="76ed18125286d7dc96ce24023d1e319dbd55a89a767102411b1ea23846113f69"
        PRIVATE_RUNTIME_ASSET_SIZE="91086530"
        PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 91 MB"
        ;;
      x86_64|amd64)
        PRIVATE_RUNTIME_ARCH="x86_64"
        PRIVATE_RUNTIME_SHA256="0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461"
        PRIVATE_RUNTIME_ASSET_SIZE="119758082"
        PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 120 MB"
        ;;
      *) fail "unsupported Linux architecture for the private Python runtime: $machine_arch" ;;
    esac
    PRIVATE_RUNTIME_PLATFORM="unknown-linux-gnu"
  fi

  PRIVATE_RUNTIME_ID="cpython-$PRIVATE_PYTHON_VERSION+$PRIVATE_PYTHON_BUILD-$PRIVATE_RUNTIME_ARCH-$PRIVATE_RUNTIME_PLATFORM"
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
    tty_print "Downloading $PRIVATE_RUNTIME_DOWNLOAD_LABEL into $PRIVATE_RUNTIME_ROOT without changing system Python."
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

  if [ "$PLATFORM_FAMILY" = "macos" ]; then
    actual_size="$(/usr/bin/stat -f '%z' "$archive" 2>/dev/null || true)"
  else
    actual_size="$(/usr/bin/stat -c '%s' "$archive" 2>/dev/null || true)"
  fi
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
    tty_print "The verified private Python archive does not provide ssl, venv, tkinter, and pip on this platform."
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
    || try_python "/usr/local/bin/python3" \
    || try_python "/opt/anaconda3/bin/python" \
    || try_python "/opt/anaconda3/bin/python3" \
    || {
      if [ "$PLATFORM_FAMILY" = "macos" ]; then
        try_python "/opt/homebrew/bin/python3" \
          || bootstrap_python_with_homebrew
      else
        fail "the verified private Python runtime could not be used and no compatible Python 3.12+ with ssl, venv, tkinter, and pip is installed; repair the reported download prerequisite or provide DE_PYTHON explicitly"
      fi
    }
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
    if [ "$PLATFORM_FAMILY" = "macos" ]; then
      tty_print "A verified private Python runtime could not be used. An existing compatible Python or optional Homebrew fallback will be checked."
    else
      tty_print "A verified private Python runtime could not be used. An existing compatible Python will be checked; no package manager will be invoked automatically."
    fi
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
absent_claude_route=0
if ! line_list_contains "$source_detected_clients" "claude-code"; then
  # Older signed stable builds route Claude skills unconditionally. Keep that
  # legacy route inside the install transaction until the stable fix lands.
  absent_claude_route=1
fi

# These bundles are distinct products, not aliases for the supported Desktop
# adapters with similar names. Report them explicitly when the current product
# source does not recognize them so a partial install cannot look complete.
unsupported_installed_hosts=""
if [ "$PLATFORM_FAMILY" = "macos" ]; then
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
  comma_list_contains "${aqg_selected_clients:-}" "claude-code" || return 0
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
run_aqg_install_doctor() {
  if comma_list_contains "$aqg_selected_clients" "claude-code" \
      && comma_list_contains "$aqg_selected_clients" "codex"; then
    clean_exec env PATH="$PYTHON_DIR:$PATH" \
      "$PYTHON_BIN" "$AQG_ROOT/scripts/aqg_doctor.py"
    return $?
  fi
  # Keep the signed Doctor's verdict; only its presentation of absent hosts
  # needs adjustment until the upstream Doctor becomes presence-aware.
  clean_exec env PATH="$PYTHON_DIR:$PATH" "$PYTHON_BIN" -c '
import json
import subprocess
import sys

doctor, clients = sys.argv[1:]
proc = subprocess.run([sys.executable, doctor, "--json"], capture_output=True, text=True)
if proc.stderr:
    print(proc.stderr, end="", file=sys.stderr)
try:
    payload = json.loads(proc.stdout)
    results = payload["results"]
    if not isinstance(results, list):
        raise ValueError("missing Doctor results")
except (ValueError, KeyError, TypeError):
    print("AQG Doctor returned an unreadable report", file=sys.stderr)
    raise SystemExit(proc.returncode or 1)

detected = set(clients.split(","))
absent = {name for client, name in (("claude-code", "claude_skill_root"),
                                      ("codex", "codex_skill_root")) if client not in detected}
counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
for result in results:
    status = result["status"]
    name = result["name"]
    detail = result["detail"]
    if status not in ("PASS", "WARN", "FAIL"):
        print("AQG Doctor returned an unknown status", file=sys.stderr)
        raise SystemExit(1)
    if name in absent and status == "WARN" and detail.endswith(
        "does not exist (skills not installed for this agent)"
    ):
        status, detail = "SKIP", "agent not detected; skills not required"
    counts[status] += 1
    marker = {"PASS": "+", "WARN": "!", "FAIL": "x", "SKIP": "-"}[status]
    print(f"  [{marker}] {status:4s} {name}: {detail}")
    if status in ("WARN", "FAIL") and result.get("fix"):
        print("        fix: " + result["fix"])
print()
print("Summary: " + " ".join(f"{key}={value}" for key, value in counts.items()))
raise SystemExit(proc.returncode)
' "$AQG_ROOT/scripts/aqg_doctor.py" "$aqg_selected_clients"
}
if ! run_aqg_install_doctor; then
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
if [ "$PLATFORM_FAMILY" = "macos" ] && {
    [ -e "$CLAUDE_3P_CONFIG" ] || [ -L "$CLAUDE_3P_CONFIG" ];
}; then
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

tty_print "Detected all supported Decision Engine hosts present on $PLATFORM_DISPLAY_NAME for signed stable $managed_version:"
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

verify_managed_mcp_runtime() {
  local allow_unactivated="${1:-0}" client
  [ "$PLATFORM_FAMILY" = "linux" ] || return 0
  [ "$allow_unactivated" = "1" ] || return 0

  while IFS= read -r client; do
    [ -n "$client" ] || continue
    if ! run_managed_python - "$client" "$MANAGED_ROOT" <<'PY'
import json
import os
import subprocess
import sys

client, root = sys.argv[1:]
environment = os.environ.copy()
environment["DE_MCP_CLIENT_HOST"] = client
messages = (
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "dp-install-smoke", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
)
payload = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"
try:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "installer.launcher",
            "--managed-root",
            root,
            "--serve-only",
        ],
        input=payload,
        text=True,
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
except subprocess.TimeoutExpired:
    print(f"MCP launcher smoke timed out for {client}", file=sys.stderr)
    raise SystemExit(1)

if process.returncode != 0:
    detail = process.stderr.strip().splitlines()
    suffix = f": {detail[-1]}" if detail else ""
    print(
        f"MCP launcher smoke exited {process.returncode} for {client}{suffix}",
        file=sys.stderr,
    )
    raise SystemExit(1)

responses = {}
for line in process.stdout.splitlines():
    if not line.strip():
        continue
    try:
        response = json.loads(line)
    except json.JSONDecodeError:
        print(f"MCP launcher emitted non-JSON stdout for {client}", file=sys.stderr)
        raise SystemExit(1)
    if isinstance(response, dict) and response.get("id") in (1, 2):
        responses[response["id"]] = response

initialize = responses.get(1)
tools_list = responses.get(2)
if not isinstance(initialize, dict) or "error" in initialize:
    print(f"MCP initialize failed for {client}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(initialize.get("result"), dict):
    print(f"MCP initialize returned no result for {client}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(tools_list, dict) or "error" in tools_list:
    print(f"MCP tools/list failed for {client}", file=sys.stderr)
    raise SystemExit(1)
tools_result = tools_list.get("result")
if not isinstance(tools_result, dict) or not isinstance(tools_result.get("tools"), list):
    print(f"MCP tools/list returned an invalid result for {client}", file=sys.stderr)
    raise SystemExit(1)
PY
    then
      printf '%s: ERROR: managed MCP launcher verification failed for %s\n' \
        "$PROGRAM_NAME" "$client" >&2
      return 1
    fi
  done <<<"$detected_clients"
}

ensure_linux_codex_gui_environment() {
  local allow_unactivated="${1:-0}"
  [ "$PLATFORM_FAMILY" = "linux" ] || return 0
  line_list_contains "$detected_clients" "codex" || return 0

  run_managed_python -c \
    'import sys
from installer import mcp_config

server_name = mcp_config.DEFAULT_SERVER_NAME
allow_unactivated = sys.argv[1] == "1"
entry = mcp_config.render_codex_entry(
    server_name, allow_unactivated=allow_unactivated
)
entry["env_vars"] = [
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
]
lines = ["[mcp_servers.%s]" % mcp_config._toml_key(server_name)]
key_order = (
    "command",
    "args",
    "cwd",
    "env",
    "env_vars",
    "startup_timeout_sec",
    "tool_timeout_sec",
)
unknown_keys = set(entry).difference(key_order)
if unknown_keys:
    raise SystemExit("unsupported Codex MCP entry field(s): %s" % sorted(unknown_keys))
for key in key_order:
    if key in entry:
        lines.append("%s = %s" % (key, mcp_config._toml_value(entry[key])))
block = "\n".join(lines) + "\n"
desired = mcp_config._tomllib().loads(block)["mcp_servers"][server_name]
mcp_config._write_codex_client(
    "codex",
    mcp_config.agent_config_path("codex"),
    server_name,
    block,
    desired,
    False,
)
actual = mcp_config.read_entry("codex", server_name)
if not isinstance(actual, dict) or actual.get("env_vars") != entry["env_vars"]:
    raise SystemExit("Linux Codex GUI environment forwarding did not persist")' \
    "$allow_unactivated" \
    </dev/null >/dev/null \
    || {
      printf '%s: ERROR: Linux Codex MCP GUI-session forwarding could not be configured\n' \
        "$PROGRAM_NAME" >&2
      return 1
    }
  tty_print "Linux Codex MCP will inherit the active desktop display session after Codex restarts."
}

cmp_verified_file() {
  local path="$1" expected_size="$2" expected_sha="$3" actual_size actual_sha
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  actual_size="$(/usr/bin/stat -c '%s' "$path" 2>/dev/null || true)"
  [ "$actual_size" = "$expected_size" ] || return 1
  actual_sha="$(/usr/bin/openssl dgst -sha256 "$path" 2>/dev/null | /usr/bin/awk '{print $NF}')"
  [ "$actual_sha" = "$expected_sha" ]
}

install_fedora_minizip_compat() {
  local asset_url="" asset_size="" asset_sha="" library_size="" library_sha=""
  local library_member="" copyright_member="./usr/share/doc/libminizip1t64/copyright"
  local runtime_id="" runtime_dir="" runtime_marker="" runtime_library=""
  local qt_lib_dir="" target="" stage="" archive="" data_archive="" candidate=""

  [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "fedora:44" ] || return 0
  case "$LINUX_MACHINE_ARCH" in
    arm64|aarch64)
      asset_url="https://ports.ubuntu.com/ubuntu-ports/pool/universe/z/zlib/libminizip1t64_1.3.dfsg-3.1ubuntu2_arm64.deb"
      asset_size="22732"
      asset_sha="1d76d40ef8319bb09f64f4c993982c55b5da8d5d479ccaa928c87b3140eb2ca9"
      library_size="67680"
      library_sha="c91f42d8c87a8b11d72958e42a53da55a2b6048b175a4f99e9860be3fcc22c2c"
      library_member="./usr/lib/aarch64-linux-gnu/libminizip.so.1.0.0"
      runtime_id="linux-gui-minizip-1.3-noble-aarch64"
      ;;
    x86_64|amd64)
      asset_url="https://archive.ubuntu.com/ubuntu/pool/universe/z/zlib/libminizip1t64_1.3.dfsg-3.1ubuntu2_amd64.deb"
      asset_size="22210"
      asset_sha="97616c0c808c10fc2d4016250b9df2af35e5eb84a1120d70d2159fa66cbe45f2"
      library_size="51504"
      library_sha="3c794f65f282da69c0286957e5b564b51a3e13381e30564a5f5b4454f9ca385c"
      library_member="./usr/lib/x86_64-linux-gnu/libminizip.so.1.0.0"
      runtime_id="linux-gui-minizip-1.3-noble-x86_64"
      ;;
    *) return 1 ;;
  esac

  [ -x /usr/bin/curl ] && [ -x /usr/bin/openssl ] \
    && [ -x /usr/bin/ar ] && [ -x /usr/bin/tar ] && [ -x /usr/bin/zstd ] \
    || { tty_print "Fedora GUI compatibility setup requires curl, OpenSSL, binutils, tar, and zstd."; return 1; }

  qt_lib_dir="$(run_managed_python -c \
    'from pathlib import Path
import PyQt6
print(Path(PyQt6.__file__).resolve().parent / "Qt6" / "lib")' \
    </dev/null 2>/dev/null || true)"
  case "$qt_lib_dir" in
    "$MANAGED_PYTHON_ROOT"/*/site-packages/PyQt6/Qt6/lib) ;;
    *) tty_print "The pinned Qt private library directory could not be verified; visual setup was not changed."; return 1 ;;
  esac
  [ -d "$qt_lib_dir" ] && [ ! -L "$qt_lib_dir" ] \
    || { tty_print "The pinned Qt private library directory is not a regular directory; visual setup was not changed."; return 1; }

  runtime_dir="$PRIVATE_RUNTIME_ROOT/$runtime_id"
  runtime_marker="$runtime_dir/.deeppattern-linux-gui-runtime"
  runtime_library="$runtime_dir/lib/libminizip.so.1"
  target="$qt_lib_dir/libminizip.so.1"

  if [ -e "$runtime_dir" ] || [ -L "$runtime_dir" ]; then
    if [ -d "$runtime_dir" ] && [ ! -L "$runtime_dir" ] \
        && [ -f "$runtime_marker" ] && [ ! -L "$runtime_marker" ] \
        && /usr/bin/grep -Fxq "schema=1" "$runtime_marker" \
        && /usr/bin/grep -Fxq "runtime_id=$runtime_id" "$runtime_marker" \
        && /usr/bin/grep -Fxq "package_sha256=$asset_sha" "$runtime_marker" \
        && cmp_verified_file "$runtime_library" "$library_size" "$library_sha"; then
      tty_print "Reusing the verified Fedora 44 GUI compatibility runtime."
    else
      tty_print "$runtime_dir is not the expected verified GUI compatibility runtime; preserve it; visual setup was not changed."
      return 1
    fi
  else
    ensure_deeppattern_runtime_directories
    stage="$(clean_exec /usr/bin/mktemp -d "$PRIVATE_RUNTIME_ROOT/.linux-gui-stage.XXXXXX")" \
      || { tty_print "A private GUI compatibility staging directory could not be created."; return 1; }
    archive="$stage/libminizip.deb"
    data_archive="$stage/data.tar.zst"
    candidate="$stage/libminizip.so.1"
    tty_print "Downloading the hash-pinned Fedora 44 GUI compatibility library..."
    if ! clean_exec /usr/bin/curl --fail --location --show-error --progress-bar \
        --proto '=https' --tlsv1.2 --connect-timeout 20 --retry 2 \
        --output "$archive" "$asset_url" \
        || ! cmp_verified_file "$archive" "$asset_size" "$asset_sha" \
        || ! clean_exec /usr/bin/ar p "$archive" data.tar.zst >"$data_archive" \
        || ! clean_exec env PATH=/usr/bin:/bin /usr/bin/tar --zstd -xOf \
          "$data_archive" "$library_member" >"$candidate" \
        || ! cmp_verified_file "$candidate" "$library_size" "$library_sha"; then
      clean_exec /bin/rm -rf "$stage"
      tty_print "The Fedora GUI compatibility asset failed download, archive, size, or SHA-256 verification; nothing was installed."
      return 1
    fi
    clean_exec /bin/mkdir -m 700 "$stage/runtime" "$stage/runtime/lib" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The private GUI runtime could not be staged."; return 1; }
    clean_exec /bin/mv "$candidate" "$stage/runtime/lib/libminizip.so.1" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The verified GUI library could not be staged."; return 1; }
    clean_exec env PATH=/usr/bin:/bin /usr/bin/tar --zstd -xOf \
      "$data_archive" "$copyright_member" >"$stage/runtime/COPYRIGHT-libminizip" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The GUI compatibility license could not be preserved."; return 1; }
    /bin/chmod 755 "$stage/runtime/lib/libminizip.so.1" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The verified GUI library permissions could not be set."; return 1; }
    printf '%s\n' \
      "schema=1" \
      "runtime_id=$runtime_id" \
      "package_url=$asset_url" \
      "package_sha256=$asset_sha" \
      "library_sha256=$library_sha" \
      >"$stage/runtime/.deeppattern-linux-gui-runtime" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The GUI runtime ownership marker could not be written."; return 1; }
    /bin/chmod 600 "$stage/runtime/.deeppattern-linux-gui-runtime" \
      "$stage/runtime/COPYRIGHT-libminizip" \
      || { clean_exec /bin/rm -rf "$stage"; tty_print "The GUI runtime metadata could not be protected."; return 1; }
    if [ -e "$runtime_dir" ] || [ -L "$runtime_dir" ] \
        || ! clean_exec /bin/mv "$stage/runtime" "$runtime_dir"; then
      clean_exec /bin/rm -rf "$stage"
      tty_print "$runtime_dir appeared during setup; preserve it; visual setup was not changed."
      return 1
    fi
    clean_exec /bin/rm -rf "$stage"
    cmp_verified_file "$runtime_library" "$library_size" "$library_sha" \
      || { tty_print "The installed GUI compatibility runtime failed final verification."; return 1; }
    tty_print "Fedora 44 GUI compatibility runtime is ready at $runtime_dir."
  fi

  if [ -e "$target" ] || [ -L "$target" ]; then
    if cmp_verified_file "$target" "$library_size" "$library_sha"; then
      return 0
    fi
    tty_print "$target is not the expected verified compatibility library; preserve it; visual setup was not changed."
    return 1
  fi
  clean_exec /bin/ln "$runtime_library" "$target" \
    || { tty_print "The verified compatibility library could not be linked into the private Qt runtime."; return 1; }
  cmp_verified_file "$target" "$library_size" "$library_sha" \
    || { tty_print "The private Qt compatibility library failed final verification."; return 1; }
}

prepare_debian12_arm64_gtk_popup_backend() {
  local missing_packages=""

  linux_debian_arm64_gtk_supported || return 2
  tty_print "Preparing the Debian 12 ARM64 GTK/WebKit popup backend."

  missing_packages="$(linux_debian_arm64_gtk_dependency_packages)"
  if [ -n "$missing_packages" ]; then
    tty_print "Debian 12 ARM64 native visual windows still require system package(s): $missing_packages"
    tty_print "Rerun this installer and approve the displayed APT dependency step, or install them with your system administrator's approval:"
    tty_print "  sudo apt-get install --no-install-recommends $missing_packages"
    return 1
  fi

  if run_managed_python -c \
      'import importlib.metadata as metadata
expected = {"pycairo": "1.27.0", "PyGObject": "3.50.0", "pywebview": "6.2.1"}
raise SystemExit(0 if all(metadata.version(name) == version for name, version in expected.items()) else 1)' \
      </dev/null >/dev/null 2>&1 \
      && (
        export PYWEBVIEW_GUI=gtk
        run_managed_python -c \
          'from client.popup import backend; import sys; raise SystemExit(0 if backend.inspect_webview(sys.executable).ready else 1)' \
          </dev/null >/dev/null 2>&1
      ); then
    tty_print "Reusing the verified Debian 12 ARM64 GTK popup backend."
    return 0
  fi

  tty_print "Installing fixed GTK bindings for the private Python 3.13 runtime; native code will be compiled against Debian's official development packages..."
  if ! run_managed_python -m pip install \
      --no-input \
      --progress-bar on \
      'pycairo @ https://files.pythonhosted.org/packages/07/4a/42b26390181a7517718600fa7d98b951da20be982a50cd4afb3d46c2e603/pycairo-1.27.0.tar.gz#sha256=5cb21e7a00a2afcafea7f14390235be33497a2cce53a98a19389492a60628430' \
      'PyGObject @ https://files.pythonhosted.org/packages/2b/58/d34e67a79631177e3c08e7d02b5165147f590171f2cae6769502af5f7f7e/pygobject-3.50.0.tar.gz#sha256=4500ad3dbf331773d8dedf7212544c999a76fc96b63a91b3dcac1e5925a1d103' \
      'pywebview @ https://files.pythonhosted.org/packages/3d/25/9491695c22c4842c5b3903b4dc172e0eecf67a27c0af34a71512c9b76a0a/pywebview-6.2.1-py3-none-any.whl#sha256=9d07275f53894ab4d5e2e0e996227193e7187dec276d9b624dccbce029216b46'; then
    tty_print "Warning: the Debian ARM64 GTK Python bindings could not be built; core MCP remains available, but visual windows will not open."
    return 1
  fi

  if ! run_managed_python -c \
      'import importlib.metadata as metadata
expected = {"pycairo": "1.27.0", "PyGObject": "3.50.0", "pywebview": "6.2.1"}
raise SystemExit(0 if all(metadata.version(name) == version for name, version in expected.items()) else 1)' \
      </dev/null >/dev/null 2>&1; then
    tty_print "Warning: the installed Debian ARM64 GTK package versions failed final verification."
    return 1
  fi
  if ! (
    export PYWEBVIEW_GUI=gtk
    run_managed_python -c \
      'from client.popup import backend; import sys; result = backend.inspect_webview(sys.executable); print("de-linux-popup: pywebview=%s" % result.state.value); raise SystemExit(0 if result.ready else 1)' \
      </dev/null
  ); then
    tty_print "Warning: the Debian ARM64 GTK packages were installed but the WebKit desktop backend could not initialize; core MCP remains available."
    return 1
  fi
  tty_print "Debian 12 ARM64 GTK popup backend is ready."
}

prepare_linux_popup_backend() {
  local missing_packages="" minizip_package="" xcb_cursor_package=""

  [ "$PLATFORM_FAMILY" = "linux" ] || return 0

  if linux_debian_arm64_gtk_supported; then
    prepare_debian12_arm64_gtk_popup_backend
    return $?
  fi

  if ! linux_native_popup_supported; then
    if run_managed_python -c \
        'from client.popup import backend; import sys; raise SystemExit(0 if backend.inspect_webview(sys.executable).ready else 1)' \
        </dev/null >/dev/null 2>&1; then
      tty_print "Reusing the existing Linux native popup backend."
      return 0
    fi
    case "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" in
      ubuntu:22.04)
        tty_print "Ubuntu 22.04 core MCP support is installed, but automatic native visual-window provisioning is not supported."
        tty_print "PyQt6 6.11 ARM64 requires manylinux_2_39_aarch64, which Ubuntu 22.04 does not provide."
        ;;
      *) tty_print "No compatible native visual-window runtime is available for this Linux platform." ;;
    esac
    tty_print "Diagram, comic, and discussion-board windows require a supported native popup backend."
    return 2
  fi

  if run_managed_python -c \
      'import importlib.metadata as metadata
expected = {
    "pywebview": "6.2.1",
    "QtPy": "2.4.3",
    "PyQt6": "6.11.0",
    "PyQt6-Qt6": "6.11.2",
    "PyQt6-sip": "13.12.0",
    "PyQt6-WebEngine": "6.11.0",
    "PyQt6-WebEngine-Qt6": "6.11.2",
}
raise SystemExit(0 if all(metadata.version(name) == version for name, version in expected.items()) else 1)' \
      </dev/null >/dev/null 2>&1 \
      && run_managed_python -c \
        'from client.popup import backend; import sys; raise SystemExit(0 if backend.inspect_webview(sys.executable).ready else 1)' \
        </dev/null >/dev/null 2>&1; then
    if [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "fedora:44" ] \
        && ! install_fedora_minizip_compat; then
      tty_print "Warning: the active Fedora GUI backend does not match the verified compatibility runtime; visual setup was not changed."
      return 1
    fi
    tty_print "Reusing the verified Linux native popup backend."
    return 0
  fi

  case "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" in
    ubuntu:24.04|ubuntu:26.04)
      minizip_package="libminizip1t64"
      xcb_cursor_package="libxcb-cursor0"
      ;;
    ubuntu:22.04|debian:12)
      minizip_package="libminizip1"
      xcb_cursor_package="libxcb-cursor0"
      ;;
    fedora:44)
      [ -x /usr/bin/ar ] || missing_packages="$missing_packages binutils"
      [ -x /usr/bin/zstd ] || missing_packages="$missing_packages zstd"
      xcb_cursor_package="xcb-util-cursor"
      ;;
    *)
      tty_print "Warning: no native visual-window package mapping exists for $LINUX_DISTRO_ID $LINUX_VERSION_ID."
      return 1
      ;;
  esac
  if [ -n "$minizip_package" ] && ! run_managed_python -c \
      'import ctypes, sys; ctypes.CDLL(sys.argv[1])' \
      'libminizip.so.1' </dev/null >/dev/null 2>&1; then
    missing_packages="$missing_packages $minizip_package"
  fi
  if ! run_managed_python -c \
      'import ctypes, sys; ctypes.CDLL(sys.argv[1])' \
      'libxcb-cursor.so.0' </dev/null >/dev/null 2>&1; then
    missing_packages="$missing_packages $xcb_cursor_package"
  fi
  if [ -n "$missing_packages" ]; then
    tty_print "$LINUX_DISTRO_ID $LINUX_VERSION_ID native visual windows require missing system package(s):$missing_packages"
    tty_print "Install them with your system administrator's approval, then rerun:"
    if [ "$LINUX_DISTRO_ID" = "fedora" ]; then
      tty_print "  sudo dnf --refresh --setopt=install_weak_deps=False install$missing_packages"
    else
      tty_print "  sudo apt-get install --no-install-recommends$missing_packages"
    fi
    tty_print "The earlier system dependency step did not complete these optional packages; core MCP setup will continue."
    return 1
  fi

  tty_print "Deep Pattern visual windows require a Linux WebView backend."
  if ! run_managed_python -c \
      'import importlib.metadata as metadata; import proxy_tools; raise SystemExit(0 if metadata.version("proxy_tools") == "0.1.0" else 1)' \
      </dev/null >/dev/null 2>&1; then
    tty_print "Installing a hash-pinned pure-Python compatibility package; no compiler is required..."
    if ! run_managed_python -m pip install \
        --no-input \
        --no-deps \
        'proxy_tools @ https://files.pythonhosted.org/packages/f2/cf/77d3e19b7fabd03895caca7857ef51e4c409e0ca6b37ee6e9f7daa50b642/proxy_tools-0.1.0.tar.gz#sha256=ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010'; then
      tty_print "Warning: the hash-pinned pywebview compatibility package could not be installed; core MCP remains available, but visual windows will not open."
      return 1
    fi
  fi

  tty_print "Installing about 210 MB of pinned prebuilt GUI wheels without compiling native code or using a system package manager..."
  if ! run_managed_python -m pip install \
      --only-binary=:all: \
      --no-input \
      --progress-bar on \
      'pywebview==6.2.1' \
      'QtPy==2.4.3' \
      'PyQt6==6.11.0' \
      'PyQt6-Qt6==6.11.2' \
      'PyQt6-sip==13.12.0' \
      'PyQt6-WebEngine==6.11.0' \
      'PyQt6-WebEngine-Qt6==6.11.2'; then
    tty_print "Warning: the binary Linux WebView backend could not be installed; core MCP remains available, but diagram, comic, and discussion-board windows will not open."
    return 1
  fi

  if [ "$LINUX_DISTRO_ID:$LINUX_VERSION_ID" = "fedora:44" ] \
      && ! install_fedora_minizip_compat; then
    tty_print "Warning: the Fedora GUI compatibility runtime could not be installed; core MCP remains available, but visual windows will not open."
    return 1
  fi

  if ! run_managed_python -c \
      'from client.popup import backend; import sys; result = backend.inspect_webview(sys.executable); print("de-linux-popup: pywebview=%s" % result.state.value); raise SystemExit(0 if result.ready else 1)' \
      </dev/null; then
    tty_print "Warning: the binary Linux WebView packages were installed but the desktop backend could not initialize; core MCP remains available."
    return 1
  fi
  tty_print "Linux native popup backend is ready."
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
  verify_managed_mcp_wiring "$allow_unactivated" || return 1
  ensure_linux_codex_gui_environment "$allow_unactivated" || return 1
  verify_managed_mcp_wiring "$allow_unactivated" || return 1
  verify_managed_mcp_runtime "$allow_unactivated"
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

filter_linux_activation_stderr() {
  local line="" virtual_gpu_notice=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      'EGL: EGL_EXT_image_dma_buf_import extension is not supported.' \
        | 'Release of profile requested but WebEnginePage still not deleted. Expect troubles !' \
        | libva\ error:\ */dri/virtio_gpu_drv_video.so\ init\ failed)
        virtual_gpu_notice=1
        ;;
      *) printf '%s\n' "$line" >&2 ;;
    esac
  done
  if [ "$virtual_gpu_notice" -eq 1 ]; then
    tty_print "NOTE: Virtual GPU acceleration is unavailable; Qt used software rendering for the activation window."
  fi
}

run_selected_permanent_setup() {
  local client activation_stderr="" activation_status=0
  set --
  while IFS= read -r client; do
    [ -n "$client" ] || continue
    set -- "$@" --client "$client"
  done <<<"$detected_clients"
  if [ "$PLATFORM_FAMILY" = "linux" ] && linux_native_popup_supported; then
    activation_stderr="$tmp_root/permanent-setup.stderr"
    : >"$activation_stderr"
    (
      export PYWEBVIEW_GUI=qt
      run_managed_python -m installer.permanent_setup "$@"
    ) 2>"$activation_stderr" || activation_status=$?
    filter_linux_activation_stderr <"$activation_stderr"
    clean_exec /bin/rm -f "$activation_stderr" || true
    return "$activation_status"
  else
    run_managed_python -m installer.permanent_setup "$@"
  fi
}

ensure_managed_runtime_git_excludes \
  || fail "the managed runtime Git exclusion could not be installed safely; no activation attempt was made"
if [ "$PLATFORM_FAMILY" = "macos" ]; then
  ensure_macos_stopper_host \
    || fail "the managed core is complete, but the owned macOS Stopper host bridge could not be installed and verified; the existing activation state was preserved"
else
  tty_print "Linux uses the signed Decision Engine in-process Stopper fallback; no system service or root privilege was installed."
fi
validate_complete_managed_root \
  || fail "the managed stable install changed while preparing the Stopper host bridge"

linux_popup_status=0
linux_popup_incomplete=0
final_mcp_allow_unactivated=0
prepare_linux_popup_backend || linux_popup_status=$?
case "$linux_popup_status" in
  0|2) ;;
  *) linux_popup_incomplete=1; tty_print "Rerun this installer after correcting the reported WebView prerequisite." ;;
esac

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
      final_mcp_allow_unactivated=1
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
verify_managed_mcp_wiring "$final_mcp_allow_unactivated" \
  || fail "a detected host MCP entry changed after wiring; close the affected Agent, then rerun this installer to repair it"

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
    || [ "$linux_popup_incomplete" -ne 0 ] \
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
