"""Decision Engine Doctor — a one-shot diagnostic for a local client install.

Mirrors AQG's doctor: gather the common "is my client ready?" checks in one
place, each with an actionable fix. It reports only PRESENCE / ABSENCE — it NEVER
prints the owner endpoint or the activation secret (the cleanroom leak-scan
forbids either leaving this machine). Stdlib only.

Exit codes:
    0 — all checks PASS (or only WARN)
    1 — at least one FAIL (a genuinely broken install)
    2 — usage error

Usage:
    python3 -m installer.doctor            # human-readable report
    python3 -m installer.doctor --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from installer import (
    client_host_lifecycle,
    config,
    cursor_version,
    install,
    managed_install,
    mcp_config,
    office,
    stopper_launch_agent,
)

# The shipped DE skills — kept in lockstep with install routing (installer.install).
DE_SKILLS = (
    "audit", "audit-adjudication", "audit-brainstorming", "audit-explore",
    "audit-forecast", "audit-market-research", "audit-writing-plans",
    "discussion-board", "graphic-explanation", "layer-check",
)


@dataclass
class CheckResult:
    status: str          # "PASS" | "WARN" | "FAIL"
    name: str
    detail: str
    fix: str = field(default="")


CURSOR_DOCTOR_STATES = (
    "not_selected",
    "configured_healthy",
    "configured_broken",
    "project_shadow_detected",
    "same_name_unowned",
    "skills_missing",
    "skills_user_modified",
    "version_skew_reload_required",
    "server_dependency_blocked",
    "local_ui_disabled",
    "unsupported_cursor_version",
    "cursor_version_unknown",
    "host_identity_conflict",
    "same_name_user_modified",
)

_CURSOR_MCP_STATUSES = frozenset(
    {
        "absent",
        "ready",
        "stale",
        "unreadable",
        "same_name_unowned",
        "same_name_user_modified",
    }
)
_CURSOR_SKILLS_STATUSES = frozenset(
    {"not_checked", "healthy", "missing", "user_modified", "unverifiable"}
)
_CURSOR_VERSION_STATUSES = frozenset(
    {
        "candidate_standard_version_floor_met",
        "unsupported_cursor_version",
        "cursor_version_unknown",
    }
)
_HOST_IDENTITY_STATUSES = frozenset(
    {"matched", "missing", "malformed", "unknown", "conflicting"}
)


@dataclass(frozen=True)
class CursorDoctorEvidence:
    """Normalized, redacted facts consumed by the pure Cursor status reducer.

    This type deliberately has no field for raw ``clientInfo``, paths, config
    values, tokens, or context. Runtime identity is optional because a one-shot
    Doctor process has no authorized persistence channel for shim observations.
    """

    selected: bool
    mcp_status: str = "absent"
    skills_status: str = "not_checked"
    project_shadow: bool = False
    diagnostic_failed: bool = False
    version_status: Optional[str] = None
    release_state: Optional[str] = None
    version_skew: bool = False
    server_dependency_blocked: bool = False
    local_ui_enabled: Optional[bool] = None
    identity_status: Optional[str] = None


def aggregate_cursor_doctor_states(
    evidence: CursorDoctorEvidence,
) -> Tuple[str, ...]:
    """Reduce normalized evidence to the master-plan's stable state order."""

    if not isinstance(evidence, CursorDoctorEvidence):
        raise ValueError("Cursor Doctor evidence is invalid")
    if type(evidence.selected) is not bool:
        raise ValueError("Cursor Doctor selection evidence is invalid")
    for value, label in (
        (evidence.project_shadow, "project shadow"),
        (evidence.diagnostic_failed, "probe failure"),
        (evidence.version_skew, "version skew"),
        (evidence.server_dependency_blocked, "server dependency"),
    ):
        if type(value) is not bool:
            raise ValueError("Cursor Doctor %s evidence is invalid" % label)
    if (
        evidence.local_ui_enabled is not None
        and type(evidence.local_ui_enabled) is not bool
    ):
        raise ValueError("Cursor Doctor local UI evidence is invalid")
    if evidence.mcp_status not in _CURSOR_MCP_STATUSES:
        raise ValueError("Cursor Doctor MCP status is invalid")
    if evidence.skills_status not in _CURSOR_SKILLS_STATUSES:
        raise ValueError("Cursor Doctor skills status is invalid")
    if (
        evidence.version_status is not None
        and evidence.version_status not in _CURSOR_VERSION_STATUSES
    ):
        raise ValueError("Cursor Doctor version status is invalid")
    if (
        evidence.release_state is not None
        and evidence.release_state
        not in {
            "current",
            "update_required",
            "reload_required",
            "unknown",
            "invalid",
        }
    ):
        raise ValueError("Cursor Doctor release state is invalid")
    if evidence.version_skew != (evidence.release_state == "reload_required"):
        raise ValueError("Cursor Doctor release skew evidence is inconsistent")
    if (
        evidence.identity_status is not None
        and evidence.identity_status not in _HOST_IDENTITY_STATUSES
    ):
        raise ValueError("Cursor Doctor identity status is invalid")

    if not evidence.selected:
        if evidence != CursorDoctorEvidence(selected=False):
            raise ValueError("unselected Cursor cannot carry diagnostic evidence")
        return ("not_selected",)

    states = set()
    configuration_healthy = (
        evidence.mcp_status == "ready"
        and evidence.skills_status == "healthy"
        and not evidence.project_shadow
        and not evidence.diagnostic_failed
        and evidence.version_status == "candidate_standard_version_floor_met"
    )
    states.add(
        "configured_healthy" if configuration_healthy else "configured_broken"
    )
    if evidence.project_shadow:
        states.add("project_shadow_detected")
    if evidence.mcp_status == "same_name_unowned":
        states.add("same_name_unowned")
    elif evidence.mcp_status == "same_name_user_modified":
        states.add("same_name_user_modified")
    if evidence.skills_status == "missing":
        states.add("skills_missing")
    elif evidence.skills_status == "user_modified":
        states.add("skills_user_modified")
    if evidence.version_skew:
        states.add("version_skew_reload_required")
    if evidence.server_dependency_blocked:
        states.add("server_dependency_blocked")
    if evidence.local_ui_enabled is False:
        states.add("local_ui_disabled")
    if evidence.version_status in {
        "unsupported_cursor_version",
        "cursor_version_unknown",
    }:
        states.add(evidence.version_status)
    if evidence.identity_status == "conflicting":
        states.add("host_identity_conflict")
    if evidence.identity_status in {
        "missing",
        "malformed",
        "unknown",
        "conflicting",
    }:
        states.add("local_ui_disabled")
    return tuple(state for state in CURSOR_DOCTOR_STATES if state in states)


def _legacy_cursor_owned_skills_status() -> str:
    """Read one bounded snapshot of the owned Cursor skill payload.

    A valid retained cache lets Doctor distinguish active modification from a
    missing skill. Without that cache, the existing signed-release verifier may
    still prove health; an unverifiable protected-state snapshot is reported as
    broken without accusing the user of modification.
    """

    from installer import (
        client_host_ownership,
        cursor_activation,
        cursor_skill_payload,
    )

    managed_root = mcp_config.registration_root()
    try:
        record = client_host_ownership.read_record_if_present(
            "cursor",
            managed_root=managed_root,
            config_path=mcp_config.agent_config_path("cursor"),
            server_name=mcp_config.DEFAULT_SERVER_NAME,
        )
    except (OSError, ValueError, config.ShellError):
        return "unverifiable"
    if record is None:
        return "not_checked"

    def ownership_unchanged() -> bool:
        try:
            current = client_host_ownership.read_record_if_present(
                "cursor",
                managed_root=managed_root,
                config_path=mcp_config.agent_config_path("cursor"),
                server_name=mcp_config.DEFAULT_SERVER_NAME,
            )
        except (OSError, ValueError, config.ShellError):
            return False
        return current == record

    destination = mcp_config.CLIENT_SPECS["cursor"].skills_global_path()
    try:
        managed_install._reject_link_components(destination)
        destination_info = os.lstat(destination)
    except FileNotFoundError:
        return "missing"
    except (OSError, managed_install.ManagedInstallError):
        return "unverifiable"
    if (
        not stat.S_ISDIR(destination_info.st_mode)
        or managed_install._is_link_like(destination)
    ):
        return "user_modified"
    for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
        root = destination / skill
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unverifiable"
        if (
            not stat.S_ISDIR(info.st_mode)
            or managed_install._is_link_like(root)
        ):
            return "user_modified"

    if (
        not isinstance(record.skill_release_id, str)
        or not cursor_skill_payload._RELEASE_ID_RE.fullmatch(
            record.skill_release_id
        )
    ):
        return "unverifiable"
    cache_root = (
        cursor_activation.cursor_activation_root()
        / "skill-payloads"
        / record.skill_release_id
    )
    try:
        managed_install._reject_link_components(cache_root)
    except managed_install.ManagedInstallError:
        return "unverifiable"
    try:
        cache_digest = cursor_skill_payload.verify_cached_payload(cache_root)
    except cursor_skill_payload.CursorSkillPayloadError:
        cache_digest = None
    if cache_digest == record.skill_manifest_sha256:
        try:
            for skill in cursor_skill_payload.CURSOR_M2_SKILLS:
                root = destination / skill
                cursor_skill_payload.verify_skill_copy(
                    cache_root,
                    root,
                    skill,
                    expected_manifest_sha256=record.skill_manifest_sha256,
                )
                if cursor_skill_payload._tree_has_nondefault_windows_stream(root):
                    raise cursor_skill_payload.CursorSkillPayloadError(
                        "owned active skill contains alternate streams"
                    )
        except cursor_skill_payload.CursorSkillPayloadUnverifiableError:
            return "unverifiable"
        except cursor_skill_payload.CursorSkillPayloadError:
            return "user_modified"
        return "healthy" if ownership_unchanged() else "unverifiable"

    try:
        cursor_activation.verify_owned_active_skills_for_doctor(
            record,
            managed_root=managed_root,
        )
    except cursor_activation.CursorActivationError:
        return "unverifiable"
    except (OSError, ValueError, config.ShellError):
        return "unverifiable"
    return "healthy" if ownership_unchanged() else "unverifiable"


def _cursor_owned_skills_status() -> str:
    """Check Cursor's shared links into the current managed-root skill payload."""

    try:
        if mcp_config.read_entry("cursor") is None:
            return "not_checked"
        body_root = mcp_config.registration_root()
        source = body_root / "skills"
        destination = mcp_config.CLIENT_SPECS["cursor"].skills_global_path()
        if not source.is_dir():
            return "unverifiable"
        for skill in sorted(path for path in source.iterdir() if path.is_dir()):
            if skill.name in mcp_config.CLIENT_SPECS["cursor"].excluded_skills:
                continue
            route = destination / skill.name
            if not os.path.lexists(route):
                return "missing"
            if not install._is_skill_route(route):
                return "user_modified"
            if not install._route_points_to(route, skill):
                return "user_modified"
        return "healthy"
    except (OSError, ValueError, config.ShellError):
        return "unverifiable"


def _cursor_local_ui_enabled() -> bool:
    """Cursor uses the same cross-platform local popup capability as peers."""

    return mcp_config.CLIENT_SPECS["cursor"].local_display_tools


def _cursor_managed_release_state() -> Optional[str]:
    # Cursor follows the common managed-root update state and needs no copied
    # payload generation or host-specific reload-skew lifecycle.
    return None


def collect_cursor_doctor_evidence(
    *,
    workspace: Optional[Path] = None,
    identity_status: Optional[str] = None,
) -> CursorDoctorEvidence:
    """Collect normalized read-only facts as a bounded best-effort snapshot."""

    diagnostic_failed = False
    try:
        mcp_status = mcp_config.entry_status("cursor")
    except (OSError, ValueError, config.ShellError):
        mcp_status = "unreadable"
        diagnostic_failed = True
    skills_status = None
    if mcp_status == "absent":
        try:
            skills_status = _cursor_owned_skills_status()
        except Exception:  # aqg: top-level boundary — ownership evidence stays redacted
            skills_status = "unverifiable"
        if skills_status == "not_checked":
            evidence = CursorDoctorEvidence(selected=False)
            return evidence
        mcp_status = "same_name_unowned"
        diagnostic_failed = skills_status == "unverifiable"
    if mcp_status not in _CURSOR_MCP_STATUSES:
        mcp_status = "unreadable"
        diagnostic_failed = True

    if skills_status is None:
        try:
            skills_status = _cursor_owned_skills_status()
        except Exception:  # aqg: top-level boundary — one read-only probe cannot expose raw failure
            skills_status = "unverifiable"
            diagnostic_failed = True

    try:
        version_evidence = cursor_version.collect_cursor_version_evidence()
        version_status = cursor_version.assess_cursor_version(
            version_evidence
        ).state
    except Exception:  # aqg: top-level boundary — raw package/path failures stay redacted
        version_status = "cursor_version_unknown"
        diagnostic_failed = True
    if version_status not in _CURSOR_VERSION_STATUSES:
        version_status = "cursor_version_unknown"
        diagnostic_failed = True

    try:
        release_state = _cursor_managed_release_state()
    except (OSError, ValueError, config.ShellError):
        release_state = "invalid"
        diagnostic_failed = True

    project_shadow = False
    if workspace is not None:
        from installer import cursor_skill_payload

        try:
            project_shadow = (
                mcp_config.cursor_workspace_shadow(workspace) is not None
                or mcp_config.cursor_workspace_skill_shadow(
                    workspace,
                    cursor_skill_payload.CURSOR_M2_SKILLS,
                )
                is not None
            )
        except (OSError, ValueError, config.ShellError):
            diagnostic_failed = True

    try:
        final_mcp_status = mcp_config.entry_status("cursor")
    except (OSError, ValueError, config.ShellError):
        final_mcp_status = "unreadable"
    if final_mcp_status == "absent":
        final_mcp_status = "same_name_unowned"
    if final_mcp_status not in _CURSOR_MCP_STATUSES:
        final_mcp_status = "unreadable"
    if final_mcp_status != mcp_status:
        diagnostic_failed = True
        mcp_status = final_mcp_status

    evidence = CursorDoctorEvidence(
        selected=True,
        mcp_status=mcp_status,
        skills_status=skills_status,
        project_shadow=project_shadow,
        diagnostic_failed=diagnostic_failed,
        version_status=version_status,
        release_state=release_state,
        version_skew=release_state == "reload_required",
        local_ui_enabled=_cursor_local_ui_enabled(),
        identity_status=identity_status,
    )
    return evidence


def cursor_doctor_states_for_machine(
    *,
    workspace: Optional[Path] = None,
    identity_status: Optional[str] = None,
) -> Tuple[str, ...]:
    """Return stable states while containing every optional probe failure."""

    try:
        evidence = collect_cursor_doctor_evidence(
            workspace=workspace,
            identity_status=identity_status,
        )
        return aggregate_cursor_doctor_states(evidence)
    except Exception:  # aqg: top-level boundary — JSON Doctor must stay redacted/available
        return ("configured_broken",)


def check_python() -> CheckResult:
    v = sys.version_info
    if v[:2] >= (3, 12):
        return CheckResult("PASS", "python", "%d.%d.%d" % (v.major, v.minor, v.micro))
    return CheckResult(
        "FAIL", "python", "%d.%d" % (v.major, v.minor),
        fix="Decision Engine needs Python 3.12+; install a compatible Python 3.x release.",
    )


def _skill_route_snapshot():
    # The DE skills are the core client payload, not an optional dependency — a
    # failed link is a genuinely broken install, so FAIL (exit 1), not WARN.
    destinations = mcp_config.active_skill_routes(for_doctor=True)
    expected = {
        client: tuple(skill for skill in DE_SKILLS if skill not in excluded)
        for client, (_directory, excluded) in destinations.items()
    }
    strict_routes = mcp_config.managed_skill_route_names()
    managed_skills = config.de_config_path().parent / "skills"
    missing = {
        client: [
            skill
            for skill in expected[client]
            if not (directory / skill).exists()
            or (
                client in strict_routes
                and not install._route_points_to(
                    directory / skill, managed_skills / skill
                )
            )
        ]
        for client, (directory, _excluded) in destinations.items()
    }
    missing = {client: skills for client, skills in missing.items() if skills}
    return destinations, expected, missing


def skill_route_failures() -> Dict[str, List[str]]:
    """Expose missing managed routes by host without parsing display strings."""
    _destinations, _expected, missing = _skill_route_snapshot()
    return missing


def check_skills() -> CheckResult:
    destinations, expected, missing = _skill_route_snapshot()
    if not missing:
        detail = ", ".join(
            "%s=%s" % (client, directory)
            for client, (directory, _excluded) in destinations.items()
        )
        counts = sorted({len(skills) for skills in expected.values()})
        count_detail = (
            str(counts[0])
            if len(counts) == 1
            else ", ".join(
                "%s=%d" % (client, len(skills))
                for client, skills in expected.items()
            )
        )
        return CheckResult(
            "PASS", "skills", "%s skills linked in %s" % (count_detail, detail)
        )
    detail = "; ".join(
        "%s: %s" % (client, ", ".join(skills))
        for client, skills in missing.items()
    )
    if set(missing) == {"codex"}:
        fix = (
            "Fully restart Codex once so the managed launcher repairs its skill links, "
            "then restart Codex again so it reloads the skills. For a legacy install, "
            "re-run ./install.sh from your clone instead."
        )
    elif set(missing) == {"cursor"}:
        fix = (
            "Run python3 -m installer.permanent_setup to repair Cursor skills, "
            "then fully restart Cursor so it reloads them."
        )
    else:
        fix = "Re-run ./install.sh from your clone to (re)link the DE skills."
    return CheckResult(
        "FAIL", "skills", "missing — %s" % detail, fix=fix,
    )


def check_setup_dialog() -> CheckResult:
    """Check the masked credential dialog dependency on every desktop platform."""
    try:
        import tkinter  # noqa: F401 — presence probe only
    except Exception:  # aqg: top-level boundary — from-env remains available without tkinter
        return CheckResult(
            "WARN",
            "setup-gui",
            "tkinter missing — the masked setup window cannot open",
            fix=(
                "Install a Python build with tkinter. If the activation check below does not "
                "report owner-guided recovery, use only an already-secure process environment "
                "(never argv or shell history) before: "
                "python3 -m installer.permanent_setup --from-env"
            ),
        )
    return CheckResult("PASS", "setup-gui", "tkinter masked setup window available")


def check_popup_backend() -> CheckResult:
    """Classify pywebview readiness without mutating the environment."""
    try:
        from client.popup import backend as popup_backend
        # avoid_gui_registration: Doctor only diagnoses — it must never make a child
        # register as a macOS GUI app, because where that is refused the child ABORTS
        # (SIGABRT) and the user gets a "Python quit unexpectedly" dialog mid-install.
        inspection = popup_backend.inspect_webview(
            sys.executable, timeout_s=15, avoid_gui_registration=True
        )
        spec = popup_backend.pywebview_pip_spec()
    except Exception:  # aqg: top-level boundary — import OR probe failure is a WARN for an optional dep
        return CheckResult(
            "WARN", "popup", "popup backend not verifiable here (run from your clone root)",
            fix="Boards open a native window; pywebview installs on first use.",
        )
    if inspection.ready:
        return CheckResult("PASS", "popup", "pywebview backend ready")
    if inspection.state is popup_backend.WebviewState.MISSING:
        return CheckResult(
            "WARN", "popup", "pywebview package missing",
            fix=(
                "The installer and first popup both retry automatically; manual same-Python "
                "repair: \"%s\" -m pip install \"%s\"" % (sys.executable, spec)
            ),
        )
    if inspection.state is popup_backend.WebviewState.NO_GUI_SESSION:
        # Not a defect: this session (agent sandbox / remote shell) cannot register a
        # window at all, so readiness is unknowable here and probing it would abort the
        # child process instead of answering. Say so rather than blaming the package.
        return CheckResult(
            "WARN", "popup", "no desktop GUI session here — popup readiness not probed",
            fix=(
                "Re-run Doctor from a normal desktop Terminal: \"%s\" -m installer.doctor"
                % sys.executable
            ),
        )
    if inspection.state is popup_backend.WebviewState.BACKEND_UNAVAILABLE:
        return CheckResult(
            "WARN", "popup", "pywebview installed but its native GUI backend is unavailable",
            fix=(
                "Run Doctor in a desktop session and verify the OS webview backend "
                "(WebView2 on Windows, WebKit on macOS, WebKit2GTK on Linux)."
            ),
        )
    return CheckResult(
        "WARN", "popup", "pywebview probe failed before readiness could be determined",
        fix="Re-run Doctor from a normal desktop terminal using: \"%s\" -m installer.doctor" % sys.executable,
    )


def check_mcp_registered() -> CheckResult:
    """Report whether detected clients have a current Decision Engine launcher entry.

    Presence alone is insufficient: old installs can retain an ``installer.shim``
    entry that bypasses the launcher. Diagnostics expose only client names and
    status categories, never commands, paths, environment values, or sibling
    configuration. WARN remains intentional because wiring is repairable.

    A merely-present host without an entry stays an informational note — the user may simply
    not want Decision Engine there. A host that Decision Engine was already ONBOARDED into
    (``installer.host_onboarding``: its DE skill route is installed) does NOT:
    its skills tell the agent to call ``mcp__decision-engine__*`` tools that its own config
    never registered, so that host is reported even when a sibling host is ready."""
    try:
        from installer import host_onboarding, mcp_config

        statuses = {}
        for client in mcp_config.clients_with_doctor_capability(
            "mcp-entry",
            detected_only=True,
        ):
            try:
                statuses[client] = mcp_config.entry_status(client)
            except Exception:  # aqg: top-level boundary - one bad client config stays a WARN
                statuses[client] = "unreadable"
        absent = [client for client, status in statuses.items() if status == "absent"]
        onboarding = host_onboarding.onboarding_report(absent)
    except Exception:  # aqg: top-level boundary — any probe failure is a WARN for this optional wiring
        return CheckResult("WARN", "mcp", "could not verify MCP registration",
                           fix="Wire it: python3 -m installer.mcp_config --write")
    ready = [client for client, status in statuses.items() if status == "ready"]
    onboarded = tuple(onboarding)
    unwired = [client for client in absent if client not in onboarding]
    repair = [
        "%s=%s" % (client, status)
        for client, status in statuses.items()
        if status not in ("ready", "absent")
    ]
    # Onboarded-but-unregistered reads like the repair states, not like the "not wired" note.
    # Group by what was actually found, so the detail never claims content a host does not
    # have (Claude Code and Cursor have no routing file of their own).
    by_phrase = {}
    for client, evidence in onboarding.items():
        by_phrase.setdefault(host_onboarding.evidence_phrase(evidence), []).append(client)
    onboarded_detail = [
        "%s %s but MCP not wired" % (", ".join(clients), phrase)
        for phrase, clients in by_phrase.items()
    ]
    if repair or onboarded:
        detail = "; ".join(
            (["ready: %s" % ", ".join(ready)] if ready else [])
            + (["repair: %s" % ", ".join(repair)] if repair else [])
            + onboarded_detail
            + (["not wired: %s" % ", ".join(unwired)] if unwired else [])
        )
        # --client takes ONE host, so name it only when exactly one host needs onboarding
        # repair; bare --write covers every detected host anyway.
        targeted = " --client %s" % onboarded[0] if len(onboarded) == 1 else ""
        return CheckResult(
            "WARN",
            "mcp",
            detail,
            fix="Repair wiring (idempotent, backs up first): python3 -m installer.mcp_config"
                " --write%s   then RESTART %s."
                % (targeted, "Codex/agent" if onboarded else "the agent"),
        )
    if ready:
        detail = "launcher ready in: %s" % ", ".join(ready)
        if unwired:
            detail += "; not wired: %s" % ", ".join(unwired)
        return CheckResult("PASS", "mcp", detail)
    return CheckResult(
        "WARN", "mcp", "decision-engine MCP not wired into any agent — hosted tools won't appear",
        fix="Wire it (idempotent, backs up first): python3 -m installer.mcp_config --write   "
            "then RESTART the agent.",
    )


def check_cursor_workspace(workspace: Path) -> CheckResult:
    """Report an explicit workspace's project-level Cursor MCP shadow."""

    mcp_shadow = None
    skill_shadow = None
    mcp_failed = False
    skill_failed = False
    try:
        from installer import mcp_config

        mcp_shadow = mcp_config.cursor_workspace_shadow(workspace)
    except Exception:  # aqg: top-level boundary — retain independent skill signal
        mcp_failed = True
    try:
        skill_shadow = mcp_config.cursor_workspace_skill_shadow(
            workspace,
            DE_SKILLS,
        )
    except Exception:  # aqg: top-level boundary — retain independent MCP signal
        skill_failed = True
    if (
        mcp_shadow is None
        and skill_shadow is None
        and not mcp_failed
        and not skill_failed
    ):
        return CheckResult(
            "PASS",
            "cursor-workspace",
            "no project-level Decision Engine MCP or known skill shadow",
        )
    details = []
    if mcp_failed:
        details.append("mcp_diagnosis=unreadable_or_invalid")
    if mcp_shadow is not None:
        mcp_detail = (
            "project_shadow_detected; precedence=unverified; global=%s; project=%s"
            % (mcp_shadow.global_source, mcp_shadow.project_source)
        )
        if mcp_shadow.same_name_conflict:
            mcp_detail += "; conflict=same_name_unowned"
        details.append(mcp_detail)
    if skill_failed:
        details.append("skill_diagnosis=unreadable_or_invalid")
    if skill_shadow is not None:
        details.append(
            "project_skill_shadow_detected; cursor_skills=%s; agents_skills=%s"
            % (
                ",".join(skill_shadow.cursor_skills) or "absent",
                ",".join(skill_shadow.agents_skills) or "absent",
            )
        )
    return CheckResult(
        "WARN",
        "cursor-workspace",
        "; ".join(details),
        fix=(
            "Review the user-global and project Decision Engine MCP/skill entries "
            "plus Cursor trust/reload state; "
            "no project file was changed."
        ),
    )


def check_cursor_version() -> CheckResult:
    """Report the offline Cursor version floor without executing Cursor."""

    try:
        evidence = cursor_version.collect_cursor_version_evidence()
        assessment = cursor_version.assess_cursor_version(evidence)
    except Exception:  # aqg: top-level boundary — version evidence stays redacted
        assessment = cursor_version.CursorVersionAssessment(
            state="cursor_version_unknown",
            detail=(
                "cursor_version_unknown; scope=standard_windows_roots; "
                "exhaustive=false; evidence=collector_error; "
                "path_preferred_standard_install=unknown; installed=unknown; "
                "installations=0; minimum=%s; compatibility=unverified"
                % cursor_version.CURSOR_CANDIDATE_MINIMUM_TEXT
            ),
        )
    if assessment.state == "candidate_standard_version_floor_met":
        return CheckResult("PASS", "cursor-version", assessment.detail)
    if assessment.state == "unsupported_cursor_version":
        fix = (
            "Update or remove every older Cursor installation, then rerun "
            "this read-only check; the 2.4 floor remains a candidate, not "
            "a compatibility claim."
        )
    else:
        fix = (
            "Install Cursor in a standard per-user or system location and "
            "ensure PATH selects that installation, then rerun this read-only check."
        )
    return CheckResult("WARN", "cursor-version", assessment.detail, fix=fix)


def check_dev_mode() -> CheckResult:
    """Surface, visibly, whether any detected client is wired to run this install in EXPLICIT
    developer-checkout mode (``--dev-root``) — installer.launcher's own semantics: no marker, no
    network, no updater, no lease at all. Not an error (a developer checkout is a legitimate,
    deliberate choice), but silent auto-update-is-off state is exactly what a doctor should flag
    rather than let a user discover it later by wondering why updates never land."""
    try:
        from installer import mcp_config

        dev_clients = []
        for client in mcp_config.detect_clients():
            try:
                entry = mcp_config.read_entry(client)
            except Exception:  # aqg: top-level boundary — one bad client config is skipped here;
                continue      # check_mcp_registered already reports it as unreadable
            if entry and mcp_config.dev_root_from_args(entry.get("args")):
                dev_clients.append(client)
    except Exception:  # aqg: top-level boundary — probe failure must not block other checks
        return CheckResult("WARN", "dev-mode", "could not verify dev-mode wiring")
    if not dev_clients:
        return CheckResult("PASS", "dev-mode", "not in developer mode")
    return CheckResult(
        "WARN",
        "dev-mode",
        "DEVELOPER MODE ACTIVE for: %s — auto-update is disabled entirely" % ", ".join(dev_clients),
        fix="Intentional for local development. To switch to the managed, auto-updating install: "
            "DE_DEV_MODE=0 ./install.sh (or unset DE_DEV_MODE) from a fresh clone.",
    )


def _probe_interpreter_compatible(command: str) -> bool:
    """Does ``command`` still exist, run, and satisfy Python 3.12+? Independent of check_python
    (THIS machine's current interpreter) — this is whatever got RECORDED in the MCP wiring, which
    can go stale if that interpreter is later removed or replaced."""
    if not command or not os.path.isfile(command) or not os.access(command, os.X_OK):
        return False
    try:
        result, _truncated, _output = _run_bounded_process(
            [command, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)"],
            env={k: v for k, v in os.environ.items() if k.upper() in {
                "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "HOME", "USERPROFILE",
            }},
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def check_python_wiring() -> CheckResult:
    """The Python interpreter path RECORDED in each registered MCP entry must still exist and be
    compatible, or the Agent can't even start the launcher. Independent repair path (task 10):
    ``installer.bootstrap_managed_install repair-wiring`` rediscovers the interpreter currently
    running it and rewrites the entry — for a dev-root entry, re-running ``mcp_config --write
    --dev-root <path>`` does the same job without touching the managed-install machinery."""
    try:
        from installer import mcp_config

        stale = {}
        for client in mcp_config.detect_clients():
            try:
                entry = mcp_config.read_entry(client)
            except Exception:  # aqg: top-level boundary — check_mcp_registered reports this client
                continue
            if not entry:
                continue
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                continue
            if not _probe_interpreter_compatible(command):
                stale[client] = mcp_config.dev_root_from_args(entry.get("args"))
    except Exception:  # aqg: top-level boundary — probe failure must not block other checks
        return CheckResult("WARN", "python-wiring", "could not verify recorded interpreter(s)")
    if not stale:
        return CheckResult("PASS", "python-wiring", "recorded interpreter(s) still runnable")
    managed = [c for c, dev_root in stale.items() if not dev_root]
    dev = [c for c, dev_root in stale.items() if dev_root]
    fixes = []
    if managed:
        fixes.append("managed: python3 -m installer.bootstrap_managed_install repair-wiring")
    if dev:
        fixes.append("dev-root: python3 -m installer.mcp_config --write --dev-root <path>")
    return CheckResult(
        "WARN",
        "python-wiring",
        "recorded interpreter missing or incompatible for: %s" % ", ".join(sorted(stale)),
        fix="; ".join(fixes) + "   then RESTART the agent.",
    )


def check_codex_legacy_routing() -> CheckResult:
    """Report only a legacy DE block that still needs one-way removal."""
    try:
        from installer import codex_routing

        path = codex_routing.agents_md_path()
        if not path.exists() and not path.parent.exists():
            return CheckResult("PASS", "codex", "not in use (no ~/.codex)")
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        begins, ends = text.count(codex_routing._BEGIN), text.count(codex_routing._END)
        if begins == 0 and ends == 0:
            if codex_routing.has_unmarked_legacy_copy(text):
                return CheckResult(
                    "WARN",
                    "codex",
                    "likely unmarked legacy Decision Engine routing remains in global AGENTS.md",
                    fix=(
                        "Review and remove the old Decision Engine sections by hand; also inspect "
                        "project AGENTS.md files where the retired fragment may have been pasted."
                    ),
                )
            return CheckResult("PASS", "codex", "legacy routing absent; skills are authoritative")
        if (
            begins == 1
            and ends == 1
            and text.find(codex_routing._BEGIN) < text.find(codex_routing._END)
        ):
            return CheckResult(
                "WARN",
                "codex",
                "legacy Decision Engine routing remains in global AGENTS.md",
                fix="Remove it safely: python3 -m installer.codex_routing   then RESTART Codex.",
            )
        return CheckResult(
            "WARN",
            "codex",
            "global AGENTS.md has malformed legacy Decision Engine markers",
            fix="Remove the decision-engine marker region by hand; preserve all other content.",
        )
    except Exception:  # aqg: top-level boundary — optional legacy probe never gates Doctor
        return CheckResult(
            "WARN",
            "codex",
            "could not verify removal of legacy Codex routing",
            fix="Inspect global AGENTS.md for the Decision Engine managed markers.",
        )


def check_libreoffice() -> CheckResult:
    """Optional — only for Office → board conversion; WARN when absent."""
    found = office.find_libreoffice()
    if found:
        return CheckResult("PASS", "libreoffice", found)
    return CheckResult(
        "WARN", "libreoffice", "not installed (optional)",
        fix="Only for Office files. Install on demand: python3 -m installer.office  (or: %s)" % office.install_hint(),
    )


def check_activation() -> CheckResult:
    """Report only WHETHER the device is activated — never the endpoint/secret value.

    The 'activated' signal is the binding written by installer.activate: an
    ``access_token`` + ``device_id`` in the per-device config. The whole body is
    guarded so a config that parses to a non-dict (a list) can't raise past this
    check and be misreported as FAIL.
    """
    path = config.de_config_path()
    if not path.exists():
        return CheckResult(
            "WARN", "activation", "device not activated yet",
            fix="Ask the agent to open permanent setup: python3 -m installer.permanent_setup",
        )
    try:
        data = config.load_json(path)
        activated = isinstance(data, dict) and bool(data.get("access_token")) and bool(data.get("device_id"))
    except Exception:  # aqg: top-level boundary — unreadable / oddly-shaped config is a WARN, not a crash
        return CheckResult("WARN", "activation", "config present but unreadable",
                           fix="Preserve the config and use managed repair/support; permanent setup will not overwrite it.")
    if activated:
        return CheckResult("PASS", "activation", "device activated")
    recovery_marker = path.parent / config.ACTIVATION_RECOVERY_RELATIVE_PATH
    if recovery_marker.exists():
        return CheckResult(
            "WARN",
            "activation",
            "owner-guided recovery required; automatic activation retry is blocked",
            fix=(
                "Preserve the installation and recovery marker; do not rerun activation. "
                "Contact the Decision Engine owner/support for recovery."
            ),
        )
    return CheckResult("WARN", "activation", "not activated yet",
                       fix="Ask the agent to open permanent setup: python3 -m installer.permanent_setup")


def check_worktree() -> CheckResult:
    """Show whether files that Git can see would block a managed update.

    Only porcelain path/status records are reported; file contents (including the
    secret-bearing config) are never included in diagnostics. Tracked changes in
    a managed checkout are FAIL because the MCP launcher refuses that state;
    developer checkout changes and untracked-only managed state remain WARN.
    """
    root = config.component_root("decision-engine")
    config_root = config.de_config_path().parent
    if os.path.normcase(os.path.abspath(str(root))) != os.path.normcase(os.path.abspath(str(config_root))):
        return CheckResult(
            "WARN", "worktree", "config root and managed install root differ",
            fix="Use the canonical managed config path, then re-run: python3 -m installer.doctor",
        )
    git = shutil.which("git")
    if not git:
        return CheckResult(
            "WARN", "worktree", "Git is not installed; managed update state cannot be checked",
            fix="Install Git, then re-run: python3 -m installer.doctor",
        )
    # Minimal environment: do not let inherited GIT_* variables redirect the
    # repository/config/index or substitute helpers. Git's own executable path
    # still lets it find its bundled helpers.
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {
            "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "HOME", "USERPROFILE", "LANG", "LC_ALL",
        }
    }
    env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_PAGER": "",
        "PAGER": "",
    })

    def run_git(args):
        return _run_bounded_process([git, *args], env=env, timeout=10)

    managed_checkout = managed_install.marker_path(root).exists()
    tracked_result = None
    tracked_truncated = False
    tracked_stdout = ""
    try:
        identity, _identity_truncated, top_level = run_git([
            "-C", str(root), "rev-parse", "--show-toplevel",
        ])
        if identity.returncode != 0 or os.path.normcase(os.path.abspath(top_level.strip())) \
                != os.path.normcase(os.path.abspath(str(root))):
            return CheckResult(
                "WARN", "worktree", "install root is not itself a readable Git checkout",
                fix="Run the managed-install repair/migration flow.",
            )
        unsafe, unsafe_truncated, _unsafe_config = run_git([
            "-C", str(root), "config", "--local", "--no-includes", "--name-only",
            "--get-regexp", r"^(filter\..*\.(clean|smudge|process)|include(if)?\..*)$",
        ])
        if unsafe_truncated or unsafe.returncode == 0:
            return CheckResult(
                "WARN", "worktree", "checkout Git config contains execution-capable settings",
                fix="Remove local filter/include settings or run the managed-install repair flow.",
            )
        if unsafe.returncode != 1:
            return CheckResult(
                "WARN", "worktree", "could not safely inspect checkout Git configuration",
                fix="Run the managed-install repair/migration flow.",
            )
        result, truncated, stdout = run_git([
            "-c", "core.quotepath=true", "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false", "-c", "submodule.recurse=false",
            "-C", str(root), "status", "--porcelain=v1",
            "--untracked-files=normal", "--ignore-submodules=all",
        ])
        if managed_checkout and truncated:
            tracked_result, tracked_truncated, tracked_stdout = run_git([
                "-c", "core.quotepath=true", "-c", "core.fsmonitor=false",
                "-c", "core.untrackedCache=false", "-c", "submodule.recurse=false",
                "-C", str(root), "status", "--porcelain=v1",
                "--untracked-files=no", "--ignore-submodules=all",
            ])
    except FileNotFoundError:
        return CheckResult(
            "WARN", "worktree", "Git is not installed; managed update state cannot be checked",
            fix="Install Git, then re-run: python3 -m installer.doctor",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "WARN", "worktree", "Git worktree inspection timed out after 10 seconds",
            fix="Check the install disk and Git health, then re-run: python3 -m installer.doctor",
        )
    except (OSError, subprocess.SubprocessError):
        return CheckResult("WARN", "worktree", "could not inspect managed checkout")
    if result.returncode != 0 and not truncated:
        return CheckResult(
            "WARN", "worktree", "install root is not a readable Git checkout",
            fix="Run the managed-install repair/migration flow.",
        )
    if tracked_result is not None and tracked_result.returncode != 0 and not tracked_stdout:
        return CheckResult(
            "WARN", "worktree", "could not inspect managed tracked checkout state",
            fix="Run the managed-install repair/migration flow.",
        )

    records = [line for line in stdout.splitlines() if line]
    if not records:
        return CheckResult("PASS", "worktree", "managed checkout is Git-clean")

    tracked_records = (
        [line for line in tracked_stdout.splitlines() if line]
        if tracked_result is not None
        else [record for record in records if record[:2] != "??"]
    )
    managed_tracked = managed_checkout and bool(tracked_records)
    reported_records = (
        tracked_records
        if managed_tracked else records
    )
    reported_truncated = (
        tracked_truncated if managed_tracked and tracked_result is not None else truncated
    )

    # Git's quoted porcelain format already escapes unusual names. Strip any
    # remaining terminal controls defensively and bound the diagnostic size.
    visible = []
    for record in reported_records[:5]:
        visible.append("".join(ch if " " <= ch < "\x7f" else "?" for ch in record)[:160])
    if reported_truncated:
        suffix = " (output truncated; many more changes)"
    else:
        suffix = " (+%d more)" % (len(reported_records) - len(visible)) \
            if len(reported_records) > len(visible) else ""
    if managed_tracked:
        return CheckResult(
            "FAIL",
            "worktree",
            "managed checkout has tracked changes: %s%s" % (", ".join(visible), suffix),
            fix=(
                "The MCP launcher cannot start from a dirty managed checkout; run the "
                "managed-install recovery/update flow, then restart the agent."
            ),
        )
    return CheckResult(
        "WARN",
        "worktree",
        "Git-visible changes: %s%s" % (", ".join(visible), suffix),
        fix="Preserve intentional files, then run the managed-install repair flow; runtime files should be ignored.",
    )


def check_managed_update() -> CheckResult:
    """Report the actual installed/target/running release without exposing config values."""

    from installer import (
        release_acquisition,
        update_coordination,
        update_transaction,
        updater,
    )

    root = config.managed_component_root("decision-engine")
    marker = root / ".managed-install.json"
    state_path = root / updater.UPDATE_STATE_RELATIVE_PATH
    protocol = root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
    journal = root / update_coordination.UPDATE_JOURNAL_RELATIVE_PATH
    core_present = [
        path.exists() or update_coordination._is_link_like(path)
        for path in (marker, state_path, protocol)
    ]
    journal_present = journal.exists() or update_coordination._is_link_like(journal)
    if not any(core_present) and not journal_present:
        return CheckResult(
            "WARN",
            "update",
            "legacy install; managed updater is not activated",
            fix="Continue using the legacy install until the signed bootstrap is published.",
        )
    if not all(core_present):
        return CheckResult(
            "FAIL",
            "update",
            "managed updater control plane is only partially installed",
            fix="Preserve the install and recovery files, then contact support; automated repair is not published yet.",
        )
    try:
        state = updater._read_update_state(root)
        update_transaction._require_protocol_ready(root)
        trusted = release_acquisition.load_trusted_release_keys(root)
    except (config.ShellError, OSError, ValueError) as exc:
        return CheckResult(
            "FAIL",
            "update",
            "managed update state is invalid (%s)" % type(exc).__name__,
            fix="Preserve the install and recovery files, then contact support; automated repair is not published yet.",
        )
    target = state.target_commit[:12] if state.target_commit else "none"
    running = state.running_commit[:12] if state.running_commit else "unconfirmed"
    source = state.source or "none"
    installed_commit = (
        state.last_release_commit[:12] if state.last_release_commit else "unknown"
    )
    detail = (
        "channel=%s source=%s installed=%s@%s target=%s running=%s result=%s"
        % (
            state.channel,
            source,
            state.last_version,
            installed_commit,
            target,
            running,
            state.last_result or "none",
        )
    )
    if state.last_result == "repair_required":
        return CheckResult(
            "FAIL",
            "update",
            detail,
            fix="Preserve the recovery directory and contact support; automated repair is not published yet.",
        )
    if state.last_result == "retry_pending":
        return CheckResult(
            "WARN",
            "update",
            detail,
            fix="The next agent restart will retry the verified managed update automatically.",
        )
    if not trusted:
        return CheckResult(
            "WARN",
            "update",
            detail + "; no production release key is provisioned",
            fix="Install an official release that contains the production public key.",
        )
    if state.running_commit != state.last_release_commit:
        return CheckResult(
            "WARN",
            "update",
            detail,
            fix="Restart the agent once; if it remains unconfirmed, run update repair.",
        )
    return CheckResult("PASS", "update", detail)


def check_stop_panel() -> CheckResult:
    """The audit stop panel — the window that shows a running audit's tier + live timer and lets
    the user hit Stop. macOS ships a native binary; every other platform (Windows / Linux) uses a
    tkinter window, which needs Python's tkinter. Without it audits still run — there is just no
    panel to watch or cancel from — so absence is WARN, never FAIL. On macOS tkinter is irrelevant
    (the native binary is used), so this always PASSes there."""
    import platform

    if platform.system() == "Darwin":
        return CheckResult("PASS", "stop-panel", "native macOS panel")
    try:
        import tkinter  # noqa: F401 — presence probe only
    except Exception:  # aqg: top-level boundary — a missing optional GUI dep is a WARN, never FAIL
        return CheckResult(
            "WARN", "stop-panel", "tkinter missing — audits run, but with no stop/progress window",
            fix="Install Python's tkinter to get the audit stop panel (on Windows the python.org "
                "installer bundles it; some minimal / conda Pythons omit it).",
        )
    return CheckResult("PASS", "stop-panel", "tkinter stop panel available")


def check_stopper_host_bridge() -> CheckResult:
    """Verify the macOS user LaunchAgent used by the MCP-unavailable bridge."""
    import platform

    if platform.system() != "Darwin":
        return CheckResult(
            "PASS", "stopper-host", "not required on this platform"
        )
    de_root = config.component_root("decision-engine")
    state = stopper_launch_agent.status(de_root=de_root)
    status = str(state.get("status", "invalid"))
    if status == "ready":
        return CheckResult(
            "PASS", "stopper-host", "owned macOS host bridge loaded"
        )
    return CheckResult(
        "FAIL",
        "stopper-host",
        "macOS host bridge %s" % status,
        fix=(
            "python3 -m installer.stopper_launch_agent install --de-root %s"
            % de_root
        ),
    )


CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_python, check_skills, check_mcp_registered, check_dev_mode, check_python_wiring,
    check_codex_legacy_routing, check_setup_dialog, check_popup_backend, check_stop_panel,
    check_stopper_host_bridge, check_libreoffice,
    check_activation, check_worktree, check_managed_update,
)

GIT_OUTPUT_LIMIT = 64 * 1024


def _run_bounded_process(argv, *, env, timeout, output_limit=GIT_OUTPUT_LIMIT):
    """Run a diagnostic child without allowing stdout to grow past a fixed cap."""
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    captured = bytearray()
    reader_error = []

    def drain_stdout():
        try:
            while len(captured) <= output_limit:
                remaining = output_limit + 1 - len(captured)
                chunk = process.stdout.read(min(8192, remaining))
                if not chunk:
                    break
                captured.extend(chunk)
                if len(captured) > output_limit:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    break
        except OSError as exc:
            reader_error.append(exc)

    reader = threading.Thread(target=drain_stdout, name="doctor-git-output", daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=2)
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
    if reader.is_alive():
        raise OSError("diagnostic output reader did not stop")
    if reader_error:
        raise reader_error[0]
    truncated = len(captured) > output_limit
    output = bytes(captured[:output_limit]).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(argv, returncode), truncated, output


def run_all(workspace: Optional[Path] = None) -> List[CheckResult]:
    results: List[CheckResult] = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # aqg: top-level boundary — one check must never crash the doctor
            results.append(CheckResult("FAIL", fn.__name__, "check crashed: %s" % type(exc).__name__))
    if workspace is not None:
        try:
            results.append(check_cursor_workspace(workspace))
        except Exception as exc:  # aqg: top-level boundary -- isolate optional check
            results.append(
                CheckResult(
                    "FAIL",
                    "cursor-workspace",
                    "check crashed: %s" % type(exc).__name__,
                )
            )
    return results


def _render(results: List[CheckResult]) -> str:
    icon = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}
    lines = []
    for r in results:
        lines.append("  %s %-12s %s" % (icon.get(r.status, "?"), r.name, r.detail))
        if r.fix and r.status != "PASS":
            lines.append("      → %s" % r.fix)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="de doctor", description="Decision Engine client diagnostic.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    cursor_scope = parser.add_mutually_exclusive_group()
    cursor_scope.add_argument(
        "--workspace",
        type=Path,
        help="read-only Cursor project-shadow diagnosis for one workspace",
    )
    cursor_scope.add_argument(
        "--cursor-version",
        action="store_true",
        help="run only the redacted Cursor installed/selected version diagnosis",
    )
    args = parser.parse_args(argv)

    if args.cursor_version:
        results = [check_cursor_version()]
        cursor_states = None
    else:
        results = run_all(workspace=args.workspace)
        cursor_states = (
            cursor_doctor_states_for_machine(workspace=args.workspace)
            if args.json
            else None
        )
    has_fail = any(r.status == "FAIL" for r in results)

    if args.json:
        payload = {
            "results": [asdict(r) for r in results],
            "ok": not has_fail,
        }
        if cursor_states is not None:
            payload["cursor_states"] = list(cursor_states)
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(_render(results))
        print()
        print("Decision Engine doctor: %s" % ("FAIL — fix the ✗ above" if has_fail else "OK"))
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
