#!/usr/bin/env bash
#
# Decision Engine — umbrella installer (Decision Engine + Agent Quality Gates)
#
# One command to put both products on your machine. AQG is the free, local,
# no-account engineering-discipline toolkit; Decision Engine (DE) is the hosted
# engine that completes it. DE also runs standalone. Start from either side and
# bring the other along:
#
#   * Decision Engine (DE) — the hosted decision/audit/market-research/forecast
#     engine. The `de` install ships AQG alongside it, so `de` gives you both.
#     The client bundle installs from this repo; using the engine needs an
#     owner-issued endpoint + device activation key.
#   * Agent Quality Gates (AQG) — the free, local, no-account toolkit. Installs
#     standalone from its own public repo, or together with DE via `WITH_DE=1`.
#
# This script hardcodes no server address, token, or secret. Normal interactive
# installation defers activation to installer.permanent_setup, whose masked
# window collects owner-issued values without putting them in argv, shell
# history, or persistent environment. The client bundle it installs (thin
# routing skills + transport shim) carries no product intelligence; the engine
# — prompts, orchestration, rendering — lives only on the hosted server.
#
# Usage:
#   ./install.sh                 # or: ./install.sh de (DE, with AQG alongside)
#   ( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
#
#   WITH_AQG=0 ./install.sh de  # DE only — no engineering toolkit (DE runs standalone)
#   ./install.sh aqg          # AQG only (local, no account) from its public repo
#   WITH_DE=1 ./install.sh aqg  # AQG + DE; activate afterward through permanent_setup
#   ./install.sh all          # DE + AQG, always (ignores WITH_AQG — `all` means both)
#
set -euo pipefail

target="${1:-de}"

# Where AQG is cloned from for the standalone path. Overridable so the address
# is not pinned (the private development source and the public release repo can
# differ). Default is the public product repo.
AQG_REPO="${AQG_REPO:-https://github.com/deeppatternai/agent-quality-gates.git}"
AQG_DEST="${AQG_DEST:-$HOME/.deeppattern/agent-quality-gates}"

# Opt-in: also install Decision Engine when the target is `aqg` (symmetric
# co-install from the AQG side). Activation may be completed afterward.
WITH_DE="${WITH_DE:-0}"

# Opt-out: skip the AQG co-install on the `de` / `all` path — install Decision
# Engine by itself. DE runs standalone (decisions, audits, market research,
# forecasts, diagrams) with no need for the engineering-discipline toolkit; set
# this when you don't want AQG (e.g. you don't write code).
WITH_AQG="${WITH_AQG:-1}"

# Opt-out: skip auto-wiring the `decision-engine` MCP into the detected agent(s)'
# config. Default ON — connecting the shim is the step a manual install kept
# skipping (→ the agent has no DE tools → diagram/board "won't open"). The write
# is idempotent + backup-first + never clobbers other servers; set WITH_MCP=0 to
# print the entry for manual paste instead.
#
# NOTE: WITH_MCP=0 only applies to DE_DEV_MODE=1. On the normal managed path,
# credential-free MCP entries are published for every detected client before the
# activation decision; permanent setup repeats and repairs the same wiring after
# successful device activation.
WITH_MCP="${WITH_MCP:-1}"

# Opt-in: run against THIS checkout directly instead of the managed, auto-updating
# install. Wires the Agent MCP with `installer.launcher --dev-root <this checkout>`
# — no marker, no network, no updater, no lease at all (installer/launcher.py's own
# explicit developer-mode semantics). Use this if you're developing decision-engine
# itself, or if the managed bootstrap below refuses because this checkout already
# sits at the fixed install path (~/.deeppattern/decision-engine) without being a
# valid signed dual-remote checkout.
DE_DEV_MODE="${DE_DEV_MODE:-0}"

die() { echo "install: $*" >&2; exit 1; }

# Accept common boolean spellings for the opt flags and FAIL LOUD on a typo — a
# one-shot installer must not silently do the opposite of what was asked (e.g.
# WITH_AQG=false quietly still installing AQG). Only 0/1 flow past here.
case "${WITH_DE}" in
  1|[Tt]rue|[Yy]es|[Oo]n)  WITH_DE=1 ;;
  0|[Ff]alse|[Nn]o|[Oo]ff) WITH_DE=0 ;;
  *) die "WITH_DE must be 0 or 1 (also true/false, yes/no, on/off) — got '${WITH_DE}'" ;;
esac
case "${WITH_AQG}" in
  1|[Tt]rue|[Yy]es|[Oo]n)  WITH_AQG=1 ;;
  0|[Ff]alse|[Nn]o|[Oo]ff) WITH_AQG=0 ;;
  *) die "WITH_AQG must be 0 or 1 (also true/false, yes/no, on/off) — got '${WITH_AQG}'" ;;
esac
case "${WITH_MCP}" in
  1|[Tt]rue|[Yy]es|[Oo]n)  WITH_MCP=1 ;;
  0|[Ff]alse|[Nn]o|[Oo]ff) WITH_MCP=0 ;;
  *) die "WITH_MCP must be 0 or 1 (also true/false, yes/no, on/off) — got '${WITH_MCP}'" ;;
esac
case "${DE_DEV_MODE}" in
  1|[Tt]rue|[Yy]es|[Oo]n)  DE_DEV_MODE=1 ;;
  0|[Ff]alse|[Nn]o|[Oo]ff) DE_DEV_MODE=0 ;;
  *) die "DE_DEV_MODE must be 0 or 1 (also true/false, yes/no, on/off) — got '${DE_DEV_MODE}'" ;;
esac

# Capture owner values before ANY installer child (including the AQG-first
# WITH_DE=1 path) can inherit them. These shell variables are deliberately not
# exported. Only the dedicated managed permanent-setup or dev activation child
# receives both values; body install receives the non-secret endpoint only.
install_owner_endpoint="${DE_ENDPOINT:-}"
install_owner_secret="${DE_ACTIVATION_SECRET:-}"
unset DE_ENDPOINT DE_ACTIVATION_SECRET

# This check intentionally lives in shell, before importing any Decision Engine
# module. An older interpreter may be unable to parse future source, so the
# actionable error must not depend on importing that source first. DE_PYTHON is
# an executable path selected and verified by the installation Agent; it is
# never evaluated as shell text.
DE_MIN_PYTHON_MAJOR="3"
DE_MIN_PYTHON_MINOR="12"
PYTHON_BIN=""

install_python_candidate_ready() {
  local probe="$1"
  env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${probe}" -c \
      'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
      </dev/null >/dev/null 2>&1 \
    && env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${probe}" -c 'import ssl, venv' \
      </dev/null >/dev/null 2>&1 \
    && env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${probe}" -m pip --version \
      </dev/null >/dev/null 2>&1 \
    && env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${probe}" -c \
      'import os, sys, sysconfig; raise SystemExit(1 if sys.prefix == sys.base_prefix and os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 0)' \
      </dev/null >/dev/null 2>&1 \
    || return 1
  if [ "${target}" = "de" ] || [ "${target}" = "all" ] || [ "${WITH_DE}" = "1" ]; then
    env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${probe}" -c 'import tkinter' \
      </dev/null >/dev/null 2>&1 || return 1
  fi
}

resolve_install_python() {
  local candidate="${DE_PYTHON:-}"
  if [ -z "${candidate}" ]; then
    # `command -v` returns only the first PATH hit. macOS commonly keeps Apple's
    # 3.9 `/usr/bin/python3` ahead of Homebrew's current Python, so walk every
    # executable candidate and choose the first one that meets the version floor.
    local path_entry path_candidate path_name canonical_entry
    local fallback_candidate=""
    local old_ifs="${IFS}"
    local -a path_entries=()
    IFS=:
    read -r -a path_entries <<< "${PATH:-}"
    IFS="${old_ifs}"
    for path_entry in "${path_entries[@]}"; do
      # Automatic discovery only trusts absolute PATH directories. Empty and
      # relative entries mean the current working tree can supply an executable;
      # running one during an installer preflight would widen the trust boundary.
      case "${path_entry}" in
        /*) ;;
        *) continue ;;
      esac
      canonical_entry="$(cd -P -- "${path_entry}" 2>/dev/null && pwd -P)" || continue
      # Keep the glob in the pathname expression so versioned names such as
      # `python3.12` are expanded by the shell before they are tested. The
      # basename check excludes executable neighbors such as python3.12-config.
      for path_candidate in "${canonical_entry}/python3" "${canonical_entry}"/python3.* "${canonical_entry}/python"; do
        [ -x "${path_candidate}" ] || continue
        path_name="${path_candidate##*/}"
        case "${path_name}" in
          python|python3) ;;
          python3.*) [[ "${path_name}" =~ ^python3\.[0-9]+$ ]] || continue ;;
          *) continue ;;
        esac
        if ! env -u DE_ENDPOINT -u DE_ACTIVATION_SECRET "${path_candidate}" -c \
            'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
            </dev/null >/dev/null 2>&1; then
          continue
        fi
        [ -n "${fallback_candidate}" ] || fallback_candidate="${path_candidate}"
        if install_python_candidate_ready "${path_candidate}"; then
          candidate="${path_candidate}"
          break 2
        fi
      done
    done
    # Preserve the existing precise diagnostics when compatible interpreters
    # exist but none passes every gate (PEP 668, missing Tk, etc.). The first
    # version-compatible candidate flows through the unchanged checks below.
    [ -n "${candidate}" ] || candidate="${fallback_candidate}"
  fi
  [ -n "${candidate}" ] || die \
    "Python 3.12+ is required. Authorize your installation Agent" \
    "to install and verify a compatible Python, then retry."
  if ! "${candidate}" -c \
      'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
      </dev/null >/dev/null 2>&1; then
    die "Python 3.12+ is required. Authorize your installation Agent" \
        "to install and verify a compatible Python, then retry."
  fi
  if ! "${candidate}" -c 'import ssl, venv' </dev/null >/dev/null 2>&1; then
    die "the selected Python cannot import ssl and venv; authorize your installation Agent" \
        "to install a complete Python 3.12+ distribution, then retry."
  fi
  if ! "${candidate}" -m pip --version </dev/null >/dev/null 2>&1; then
    die "the selected Python has no usable pip, which is required to install AQG dependencies." \
        "Authorize your installation Agent to install a complete Python 3.12+ distribution, then retry."
  fi
  # `pip --version` only proves pip EXISTS. An externally-managed interpreter (PEP 668 —
  # Homebrew's python@3.13 is the common one) ships a pip that refuses every install, so it
  # clears every gate above and is then recorded as the interpreter for the whole install,
  # only to fail much later when AQG's dependencies or the popup backend cannot be added to
  # it. A venv built from it is the supported remedy; the marker lives in the BASE stdlib,
  # which a venv still reports as its own, so the venv must be excluded here or this gate
  # would reject the very fix it recommends. Exit 0 means USABLE, as in every gate above —
  # reporting trouble as success would reject any stub that returns 0 by default.
  if ! "${candidate}" -c \
      'import os, sys, sysconfig; raise SystemExit(1 if sys.prefix == sys.base_prefix and os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 0)' \
      </dev/null >/dev/null 2>&1; then
    die "the selected Python is externally managed (PEP 668): its pip exists but refuses every" \
        "install, so Decision Engine's dependencies cannot be added to it. Authorize your" \
        "installation Agent to create a venv from it and retry with that interpreter:" \
        "${candidate} -m venv \"\$HOME/.deeppattern/de-python\" && DE_PYTHON=\"\$HOME/.deeppattern/de-python/bin/python3\" ./install.sh"
  fi
  if [ "${target}" = "de" ] || [ "${target}" = "all" ] || [ "${WITH_DE}" = "1" ]; then
    if ! "${candidate}" -c 'import tkinter' </dev/null >/dev/null 2>&1; then
      die "the selected Python has no tkinter support, so the masked activation window cannot open." \
          "Authorize your installation Agent to install a Python 3.12+ build with Tk, then retry."
    fi
  fi
  PYTHON_BIN="${candidate}"
}

run_python() {
  "${PYTHON_BIN}" "$@"
}

require_https() {
  # The endpoint carries a device token downstream; never let it degrade to
  # plaintext http. Localhost is the only dev exception — matched EXACTLY (bare
  # host, :port, or /path) so a lookalike like http://localhost.evil.com does
  # NOT slip through a prefix glob.
  case "$1" in
    https://*) return 0 ;;
    http://localhost|http://localhost/*|http://localhost:*) return 0 ;;
    http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*) return 0 ;;
    http://*) die "refusing a plaintext http:// endpoint ($1) — use https://" ;;
    *) die "endpoint must start with https:// (got: $1)" ;;
  esac
}

# The body installer may stage the non-secret endpoint for developer mode, but
# the activation secret must never appear in argv. Managed installs consume both
# values later via `installer.permanent_setup --from-env` and only persist the
# server-issued device credentials after successful activation.
run_installer_install_de() {
  local bundle_root="$1"
  if [ -n "${DE_ENDPOINT}" ]; then
    run_python -m installer.install de --bundle-root "${bundle_root}" \
      --server-endpoint "${DE_ENDPOINT}"
  else
    run_python -m installer.install de --bundle-root "${bundle_root}"
  fi
}

# Credentials stay in the inherited environment and never enter the managed
# bootstrap's command line or staged config.
run_bootstrap_managed_install() {
  run_python -m installer.bootstrap_managed_install install
}

# The installable client body — laid out flat at the repo root (skills/, client/,
# desktop/, integrations/, pyproject.toml) alongside this installer shell.
DE_BODY_PATHS="skills client desktop integrations pyproject.toml"

install_de_body() {
  # DE client body only (no AQG). The body ships IN this repo (flat at the root),
  # so install straight from the local clone; there is no server bundle fetch.
  # Endpoint + activation key are owner-provided and land only in the per-device
  # config (never hardcoded). Both are OPTIONAL: the client installs fine
  # without them, it just stays unactivated (DE Lite audit is local advisory-only;
  # hosted audit, board generation, etc. wait for activation). Dev mode securely calls
  # `installer.activate --from-env` when both values are present; managed mode
  # delegates to permanent setup. Neither path stages the activation key.
  local owner_endpoint="${install_owner_endpoint}" owner_secret="${install_owner_secret}"
  # Managed installation validates the endpoint during the later, non-blocking
  # permanent activation step. Developer mode still stages the endpoint, so it
  # keeps the early transport check.
  if [ "${DE_DEV_MODE}" = "1" ] && [ -n "${owner_endpoint}" ]; then
    require_https "$owner_endpoint"
  fi

  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  [ -d "${repo_root}/skills" ] \
    || die "no skills/ body beside install.sh at ${repo_root} — clone the full repo, don't copy install.sh alone"

  if [ "${DE_DEV_MODE}" = "1" ]; then
    install_de_dev_mode "${repo_root}" "${owner_endpoint}"
  else
    install_de_managed "${repo_root}" "${owner_endpoint}" "${owner_secret}"
  fi
}

# DE_DEV_MODE=1: install code/skills/config exactly as before (this repo's own copy-or-in-place
# logic, unchanged), but wire the Agent MCP with `--dev-root <repo_root>` instead of the managed
# root — installer.launcher's explicit developer mode: no managed marker/release network, no
# updater, and no lease. Use this for developing decision-engine itself, or when the managed path below refuses
# because this checkout already sits at the fixed install path without being a signed managed
# checkout (see install_de_managed's die message).
install_de_dev_mode() {
  local repo_root="$1"
  local owner_endpoint="$2"
  echo "=============================================================="
  echo "  DE_DEV_MODE=1 — developer checkout mode."
  echo "  The Agent MCP will run straight from: ${repo_root}"
  echo "  Auto-update is disabled entirely for this install."
  echo "=============================================================="

  # The canonical install home — PARALLEL TO AQG (~/.deeppattern/agent-quality-gates).
  # When the clone IS this dir, install IN PLACE: `installer.install` sees bundle-root=parent so
  # its source == the target and it skips the rmtree-copy (which would otherwise DELETE this very
  # clone — .git + installer/ and all). `git pull` then updates the body directly, like AQG.
  local de_home bundle_root rc=0
  de_home="${DEEPPATTERN_HOME:-$HOME/.deeppattern}/decision-engine"

  # `-ef` = SAME FILE (device+inode), not string equality: immune to symlinks, trailing slashes,
  # /var vs /private/var, and case spelling. A string compare here was the data-loss vector — a
  # false-negative would route a real clone through the copy path and rmtree it (audit 50070df6
  # convergent 4/4). `-ef` is false when de_home doesn't exist yet, which correctly picks legacy.
  if [ "${repo_root}" -ef "${de_home}" ]; then
    echo "==> Installing Decision Engine in place at ${repo_root}"
    bundle_root="$(dirname "${de_home}")"   # parent → src == target → in-place (no copy)
    ( cd "${repo_root}" && DE_ENDPOINT="${owner_endpoint}" run_installer_install_de "${bundle_root}" ) || rc=$?
  else
    # Clone lives elsewhere: stage the flat body into a <bundle-root>/decision-engine/ layout and
    # copy it into the install home (legacy path; prefer cloning to ${de_home} for the clean layout).
    # The Python side additionally REFUSES to overwrite a dst that contains .git (defense in depth).
    local stage item
    stage="$(mktemp -d)"
    trap 'rm -rf "${stage}"; trap - RETURN' RETURN   # clean the temp body on this fn's return AND self-clear: a RETURN trap is not function-local, so without this it leaks to install_de() and fires again after `stage` (local) is out of scope → `stage: unbound variable` under `set -u` + nonzero exit
    mkdir -p "${stage}/decision-engine"
    for item in ${DE_BODY_PATHS}; do
      [ -e "${repo_root}/${item}" ] && cp -R "${repo_root}/${item}" "${stage}/decision-engine/"
    done
    echo "==> Installing Decision Engine from ${repo_root}"
    ( cd "${repo_root}" && DE_ENDPOINT="${owner_endpoint}" run_installer_install_de "${stage}" ) || rc=$?
  fi
  [ "${rc}" -eq 0 ] || die "decision-engine dev-mode install failed"
  unset DE_ENDPOINT DE_ACTIVATION_SECRET

  # Codex Desktop can run the MCP-unavailable bridge inside a sandbox that is
  # unable to launch an AppKit process. Register the owned user LaunchAgent at
  # install time so its WatchPath can wake the native Stopper outside that
  # sandbox when the bridge writes the private local-run registry.
  # Optional by design: a locked-down launchd (MDM, restricted profile) can refuse the
  # registration, and that must NOT throw away an install that is otherwise complete — the same
  # class of defect as the abort that made a successful activation exit 134. Warn and continue,
  # exactly as the MCP wiring below already does.
  ( cd "${repo_root}" && run_python -m installer.stopper_launch_agent install --de-root "${de_home}" ) \
    || echo "install: NOTE — could not register the Stopper host LaunchAgent; the audit stop" \
            "panel may not appear on hosts that run Decision Engine inside a sandbox. Everything" \
            "else is installed. Retry with '${PYTHON_BIN} -m installer.stopper_launch_agent" \
            "install --de-root ${de_home}'." >&2

  if [ -n "${owner_endpoint}" ] && [ -n "${install_owner_secret}" ]; then
    if (
      cd "${repo_root}" &&
      DE_ENDPOINT="${owner_endpoint}" DE_ACTIVATION_SECRET="${install_owner_secret}" \
        run_python -m installer.activate --from-env
    ); then
      :
    else
      rc=$?
      if [ "${rc}" -eq 3 ]; then
        echo "install: dev-mode device activation needs owner-guided recovery; do not retry automatically." >&2
      else
        echo "install: dev-mode device activation failed; the local install remains ready." >&2
      fi
    fi
  fi

  if [ "${WITH_MCP}" = "1" ]; then
    echo "==> Wiring the local decision-engine MCP into your agent(s) (--dev-root ${repo_root}):"
    ( cd "${repo_root}" && run_python -m installer.mcp_config --write --dev-root "${repo_root}" ) \
      || echo "install: NOTE — auto-wire found no agent or hit an error; run" \
              "'${PYTHON_BIN} -m installer.mcp_config --write --dev-root ${repo_root} --client <claude-code|claude-desktop|claude-desktop-3p|codebuddy|codex|cursor|qoder|qoder-cn|qoder-ide|qoder-cn-ide|trae|trae-work|trae-cn|trae-work-cn|workbuddy|workbuddy-ai>'" \
              "or see installer/README.md." >&2

    # Current Codex releases route through skills.  This is a one-way migration only: it removes
    # the former marker-delimited DE block after all replacement skills are present and never
    # creates or adds global prose.
    ( cd "${repo_root}" && run_python -m installer.codex_routing --require-skills-root "${repo_root}" ) \
      || echo "install: NOTE — could not retire the legacy Decision Engine block from Codex" \
              "AGENTS.md; run '${PYTHON_BIN} -m installer.codex_routing' after installation." >&2
  else
    echo "==> WITH_MCP=0 — skipping MCP auto-wire; paste the entry from" \
         "'${PYTHON_BIN} -m installer.mcp_config --dev-root ${repo_root}' into your agent config yourself."
  fi

  echo "==> Decision Engine doctor:"
  ( cd "${repo_root}" && run_python -m installer.doctor ) || true
}

# Publish the MCP transport for every host detected by the landed managed copy.
# This deliberately uses the fixed pre-activation mode: the launcher can serve
# the local Lite surface without device credentials, while the default public
# MCP writer remains activation-gated for all other callers.
wire_managed_mcp() {
  local managed_root="$1"
  local detected_clients client status failed=0

  if ! detected_clients="$(
    cd "${managed_root}" &&
      run_python -c 'from installer import mcp_config; print("\n".join(mcp_config.detect_clients()))'
  )"; then
    echo "install: ERROR — could not detect supported Agent hosts for MCP wiring" >&2
    return 1
  fi
  if [ -z "${detected_clients}" ]; then
    echo "install: no supported Agent host detected; MCP wiring will be available when a host is installed."
    return 0
  fi

  while IFS= read -r client; do
    [ -n "${client}" ] || continue
    if ! (
      cd "${managed_root}" &&
        run_python -m installer.mcp_config --write --allow-unactivated --client "${client}"
    ); then
      echo "install: ERROR — MCP wiring failed for detected host ${client}" >&2
      failed=1
      continue
    fi
    if ! status="$(
        cd "${managed_root}" &&
        run_python -c \
          'from installer import mcp_config; import sys; print(mcp_config.entry_status(sys.argv[1], allow_unactivated=True))' \
          "${client}"
    )"; then
      echo "install: ERROR — could not verify MCP wiring for detected host ${client}" >&2
      failed=1
    elif [ "${status}" != "ready" ]; then
      echo "install: ERROR — MCP wiring for detected host ${client} is ${status}, not ready" >&2
      failed=1
    fi
  done <<<"${detected_clients}"

  if [ "${failed}" -ne 0 ]; then
    return 1
  fi
  if ! (
    cd "${managed_root}" &&
      run_python -c \
        'from installer import config, install; result = install.repair_detected_skill_routes(config.managed_component_root("decision-engine")); raise SystemExit(1 if result.failed else 0)'
  ); then
    echo "install: ERROR — managed skill routing failed for one or more detected hosts" >&2
    return 1
  fi
}

# Normal path: clone/verify/land a managed checkout at the fixed install root and activate it —
# regardless of whether repo_root (wherever this install.sh happens to be running from) is the
# fixed root, some other clone, or a temp download. repo_root is used ONLY to locate the
# installer.bootstrap_managed_install code currently being run; it is never read as git state.
# Idempotent: re-running against an already-activated managed root is a clean no-op; re-running
# against one this bootstrap started but did not finish resumes it.
install_de_managed() {
  local repo_root="$1"
  local owner_endpoint="$2" owner_secret="$3"
  if [ "${WITH_MCP}" != "1" ]; then
    die "WITH_MCP=0 has no effect on the managed install path — credential-free MCP wiring is" \
        "required for the unactivated DE Lite path. Use DE_DEV_MODE=1 if you want to skip" \
        "automatic MCP wiring."
  fi

  echo "==> Bootstrapping the managed Decision Engine install (clone stable, verify signature, activate)"
  if ! (
    cd "${repo_root}" &&
    DE_ENDPOINT="${owner_endpoint}" DE_ACTIVATION_SECRET="${owner_secret}" \
      run_bootstrap_managed_install
  ); then
    die "decision-engine managed bootstrap failed — see the errors above." \
        "Developing against this checkout directly instead? Re-run with DE_DEV_MODE=1."
  fi
  unset DE_ENDPOINT DE_ACTIVATION_SECRET

  # Fixed by definition (installer.config.managed_component_root ignores DEEPPATTERN_HOME on
  # purpose — a checkout that can authorize managed Git repair must use one predictable path).
  local managed_root="$HOME/.deeppattern/decision-engine"

  # Optional by design — see the dev-mode call site above: a refused registration costs the
  # sandboxed Stopper wake-up, never the installation.
  ( cd "${managed_root}" && run_python -m installer.stopper_launch_agent install --de-root "${managed_root}" ) \
    || echo "install: NOTE — could not register the Stopper host LaunchAgent; the audit stop" \
            "panel may not appear on hosts that run Decision Engine inside a sandbox. Everything" \
            "else is installed. Retry with '${PYTHON_BIN} -m installer.stopper_launch_agent" \
            "install --de-root ${managed_root}'." >&2

  # MCP registration is transport wiring only and carries no endpoint or token.
  # Publish and verify every detected host before reporting installation success,
  # including when activation is still pending.
  wire_managed_mcp "${managed_root}" \
    || die "Decision Engine was installed, but one or more detected Agent hosts could not be wired."

  # One-way migration from releases that copied DE prose into Codex's global AGENTS.md.  Current
  # routing lives in the installed skills; this command only removes the old managed block.
  ( cd "${managed_root}" && run_python -m installer.codex_routing --require-skills-root "${managed_root}" ) \
    || echo "install: NOTE — could not retire the legacy Decision Engine block from Codex" \
            "AGENTS.md; run '${PYTHON_BIN} -m installer.codex_routing' from ${managed_root}." >&2

  # Post-install self-check (AQG-style doctor), run against the ACTUAL landed managed checkout
  # (not repo_root, which may be a different — e.g. newer local dev — copy of the same code).
  echo "==> Decision Engine doctor:"
  ( cd "${managed_root}" && run_python -m installer.doctor ) || true
}

run_aqg_client_phase() {
  local phase="$1"
  local flag="$2"
  local script="${AQG_DEST}/scripts/install_aqg_clients.py"
  local status
  local q_python q_script q_home q_aqg_dest

  if [ ! -f "${script}" ]; then
    q_aqg_dest=$(printf '%q' "${AQG_DEST}")
    die "AQG client adapter script not found at ${script}; update the AQG checkout with" \
        "git -C ${q_aqg_dest} pull --ff-only, then retry this installer."
  fi

  if run_python "${script}" --installed-supported "${flag}" \
      --home "${HOME}" --aqg-root "${AQG_DEST}"; then
    status=0
  else
    status=$?
  fi

  case "${status}" in
    0)
      return 0
      ;;
    3)
      echo "==> AQG client adapter ${phase}: no supported Agent host detected; successful no-op."
      return 0
      ;;
    *)
      q_python=$(printf '%q' "${PYTHON_BIN}")
      q_script=$(printf '%q' "${script}")
      q_home=$(printf '%q' "${HOME}")
      q_aqg_dest=$(printf '%q' "${AQG_DEST}")
      {
        echo "install: AQG client adapter ${phase} phase failed via install_aqg_clients.py (exit ${status})."
        echo "install: retry with: ${q_python} ${q_script} --installed-supported ${flag} --home ${q_home} --aqg-root ${q_aqg_dest}"
      } >&2
      return "${status}"
      ;;
  esac
}

install_de() {
  local aqg_ready="0"
  local status
  if [ "${WITH_AQG}" = "1" ]; then
    ensure_aqg_checkout_and_deps \
      || die "AQG could not be installed; Decision Engine was not installed." \
             "Review the AQG installation errors above, then retry this installer."

    if run_aqg_client_phase apply --apply; then
      :
    else
      status=$?
      return "${status}"
    fi

    if run_aqg_client_phase verify --verify; then
      :
    else
      status=$?
      return "${status}"
    fi

    echo "==> Verifying Agent Quality Gates:"
    run_python "${AQG_DEST}/scripts/aqg_doctor.py" --no-cli \
      || die "AQG Doctor reported unhealthy after client apply/verify; Decision Engine was not installed." \
             "Review and repair the AQG Doctor errors above, then retry this installer."
    aqg_ready="1"
  fi
  install_de_body
  # The managed bootstrap has already consumed the owner values. Drop them
  # from this installer process before launching AQG/git/Doctor children; this
  # does not change the parent shell or any User-level environment variable.
  unset DE_ENDPOINT DE_ACTIVATION_SECRET
  if ! decision_engine_is_activated; then
    if [ "${aqg_ready}" = "1" ]; then
      echo "AQG is installed and verified. Decision Engine is not activated."
      echo "Ask the Decision Engine owner for the endpoint and device activation key; once you have them," \
           "do not send the real values in chat — just tell me \"continue installing DE\"."
    else
      echo "Decision Engine core files are installed, but the device is not activated; DE Lite MCP is ready."
    fi
    return
  fi
  echo "==> Decision Engine activated; MCP wiring and automatic updates are ready."
}

decision_engine_is_activated() {
  local root="$HOME/.deeppattern/decision-engine"
  [ -d "${root}" ] || return 1
  (
    cd "${root}" &&
    run_python -c \
      'from installer import activate, config; raise SystemExit(0 if activate.is_permanently_activated(config.load_json(config.de_config_path())) else 1)'
  ) </dev/null >/dev/null 2>&1
}

aqg_checkout_is_usable() {
  [ -e "${AQG_DEST}/.git" ] \
    && [ -f "${AQG_DEST}/scripts/install.sh" ] \
    && [ -f "${AQG_DEST}/scripts/aqg_doctor.py" ] \
    && [ -f "${AQG_DEST}/requirements.txt" ]
}

aqg_is_ready() {
  aqg_checkout_is_usable \
    && run_python -c 'import yaml; assert tuple(int(x) for x in yaml.__version__.split(".")[:2]) >= (6, 0)' \
         </dev/null >/dev/null 2>&1 \
    && run_python "${AQG_DEST}/scripts/aqg_doctor.py" --no-cli \
         </dev/null >/dev/null 2>&1
}

ensure_aqg_dependencies() {
  echo "==> Installing AQG runtime dependencies with ${PYTHON_BIN}"
  if run_python -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' \
       </dev/null >/dev/null 2>&1; then
    run_python -m pip install -r "${AQG_DEST}/requirements.txt"
  else
    run_python -m pip install --user -r "${AQG_DEST}/requirements.txt"
  fi
}

ensure_aqg_checkout_and_deps() {
  # Prepares the AQG checkout and its dependencies for the DE install flow
  # WITHOUT running AQG Doctor as a pre-apply gate: an unhealthy/fresh host
  # surface must not block the client wrapper apply/verify steps. The single
  # fail-closed Doctor gate for this flow runs once, after apply/verify.
  if aqg_checkout_is_usable; then
    echo "==> AQG checkout present at ${AQG_DEST}; ensuring dependencies are installed."
  else
    install_aqg_body || return 1
  fi
  ensure_aqg_dependencies
}

ensure_aqg_ready() {
  if aqg_is_ready; then
    echo "==> AQG already installed and healthy at ${AQG_DEST}; reusing it without pull or reinstall."
    return 0
  fi
  if aqg_checkout_is_usable; then
    echo "==> Existing AQG checkout needs dependency or routing repair; preserving its revision."
    ensure_aqg_dependencies || return 1
    if run_python "${AQG_DEST}/scripts/aqg_doctor.py" --no-cli; then
      echo "==> AQG dependencies repaired and verified at ${AQG_DEST}."
      return 0
    fi
    echo "==> Re-running the existing AQG installer without pulling a new revision."
    run_aqg_installer || return 1
    run_python "${AQG_DEST}/scripts/aqg_doctor.py" --no-cli || return 1
    echo "==> AQG installed and verified at ${AQG_DEST}."
    return 0
  fi
  install_aqg_body || return 1
  ensure_aqg_dependencies || return 1
  echo "==> Verifying Agent Quality Gates:"
  run_python "${AQG_DEST}/scripts/aqg_doctor.py" --no-cli || return 1
  echo "==> AQG installed and verified at ${AQG_DEST}."
}

install_aqg_body() {
  local aqg_archive
  # Returns non-zero on failure (does NOT die), so the caller can report the
  # AQG-before-DE failure at the correct boundary.
  echo "==> Installing Agent Quality Gates (local, no account) from ${AQG_REPO}"
  if [ -L "${AQG_DEST}" ] || [ -f "${AQG_DEST}/.git" ]; then
    echo "install: incomplete managed AQG checkout at ${AQG_DEST}; preserve it and repair AQG before retrying" >&2
    return 1
  fi
  if [ -d "${AQG_DEST}/.git" ]; then
    echo "    Existing checkout at ${AQG_DEST} — updating."
    [ -z "$(git -C "${AQG_DEST}" status --porcelain=v1 --untracked-files=all)" ] \
      || { echo "install: AQG checkout at ${AQG_DEST} has local changes; preserve it and repair before retrying" >&2; return 1; }
    git -C "${AQG_DEST}" config --local core.autocrlf false \
      && git -C "${AQG_DEST}" config --local core.eol lf \
      || { echo "install: could not pin LF-safe Git configuration at ${AQG_DEST}" >&2; return 1; }
    aqg_archive="$(mktemp)" \
      || { echo "install: could not create a temporary AQG archive" >&2; return 1; }
    if ! git -C "${AQG_DEST}" archive --output="${aqg_archive}" HEAD \
        || ! tar -xf "${aqg_archive}" -C "${AQG_DEST}"; then
      rm -f "${aqg_archive}"
      echo "install: could not rematerialize LF-safe AQG files at ${AQG_DEST}" >&2
      return 1
    fi
    rm -f "${aqg_archive}"
    git -C "${AQG_DEST}" add --update \
      || { echo "install: could not refresh the LF-safe AQG index at ${AQG_DEST}" >&2; return 1; }
    if ! git -C "${AQG_DEST}" diff --cached --quiet HEAD --; then
      git -C "${AQG_DEST}" reset --quiet HEAD -- .
      echo "install: AQG files changed while line endings were normalized; preserve them and repair before retrying" >&2
      return 1
    fi
    git -C "${AQG_DEST}" pull --ff-only \
      || { echo "install: could not update AQG checkout at ${AQG_DEST}" >&2; return 1; }
  else
    mkdir -p "$(dirname "${AQG_DEST}")"
    git clone --config core.autocrlf=false --config core.eol=lf "${AQG_REPO}" "${AQG_DEST}" \
      || { echo "install: could not clone AQG from ${AQG_REPO} (public? git installed?)" >&2; return 1; }
  fi
  [ -f "${AQG_DEST}/scripts/install.sh" ] \
    || { echo "install: no scripts/install.sh in ${AQG_DEST} — is this the AQG repo?" >&2; return 1; }
  run_aqg_installer || return 1
  echo "==> AQG installed at ${AQG_DEST}."
}

run_aqg_installer() {
  # Clear WITH_DE for the child so AQG's own installer can't re-trigger a DE co-install.
  WITH_DE=0 bash "${AQG_DEST}/scripts/install.sh" || return 1
}

install_aqg() {
  ensure_aqg_ready || die "AQG install failed — see the errors above"
  if [ "${WITH_DE}" = "1" ]; then
    echo "==> WITH_DE=1 — also installing Decision Engine alongside AQG."
    # Re-enter the normal DE flow so the activation-deferred result uses the
    # same message and behavior. The second AQG check is a fast healthy reuse.
    WITH_AQG=1 install_de
  fi
}

resolve_install_python

case "${target}" in
  de)     install_de ;;
  all)    WITH_AQG=1 install_de ;;   # `all` always means both — WITH_AQG can't strip AQG out
  aqg)    install_aqg ;;
  *)      die "unknown target '${target}' (choose: de | aqg | all)" ;;
esac
