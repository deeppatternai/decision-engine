"""Managed updater gate for the Decision Engine MCP shim.

All updater, rollback and state code is imported before a possible reset.  The
shim itself is imported only while its session lease is held.  When recovery or
an update changes HEAD, a fresh child interpreter inherits MCP stdio and serves
the selected checkout, avoiding a mixed old/new ``installer`` module graph
without requiring an Agent restart.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Mapping, Optional, TextIO

from . import (
    config,
    managed_install,
    release_acquisition,
    release_contract,
    startup_diagnostics,
    update_coordination,
    update_transaction,
    updater,
)
from .config import ShellError
from .release_acquisition import load_trusted_release_keys


STARTUP_UPDATE_BUDGET_SECONDS = 60.0
STARTUP_LEADER_GRACE_SECONDS = 5.0
CLIENT_HOST_ENV = "DE_MCP_CLIENT_HOST"


def _log(message: str) -> None:
    print("de-launcher: %s" % message, file=sys.stderr)


def _resolved_client_host(value: Optional[str]) -> Optional[str]:
    candidate = value if value is not None else os.environ.get(CLIENT_HOST_ENV)
    normalized = candidate.strip().lower() if isinstance(candidate, str) else ""
    return normalized or None


def _client_host_kwargs(
    shim: ModuleType, client_host: Optional[str]
) -> dict:
    """Pass the new keyword only to shims that declare they can accept it."""
    if not client_host:
        return {}
    try:
        parameters = inspect.signature(shim.serve).parameters.values()
    except (TypeError, ValueError):
        return {}
    if any(
        parameter.name == "client_host"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return {"client_host": client_host}
    return {}


def _startup_diagnostic_kwargs(
    shim: ModuleType, root: Path, client_host: Optional[str]
) -> dict:
    """Pass phase journaling only to shims that explicitly accept the callback."""

    try:
        parameters = inspect.signature(shim.serve).parameters.values()
    except (TypeError, ValueError):
        return {}
    if not any(
        parameter.name == "on_startup_event"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return {}

    def record(event: str, phase: str, **fields) -> None:
        startup_diagnostics.append_event(
            root,
            event,
            phase,
            client_host=_resolved_client_host(client_host),
            **fields,
        )

    return {"on_startup_event": record}


def _forwarder_kind(shim: ModuleType, forwarder) -> str:
    lite = getattr(shim, "LiteForwarder", None)
    offline = getattr(shim, "OfflineForwarder", None)
    if isinstance(lite, type) and isinstance(forwarder, lite):
        return "lite"
    if isinstance(offline, type) and isinstance(forwarder, offline):
        return "offline"
    forwarder_type = getattr(shim, "Forwarder", None)
    if isinstance(forwarder_type, type) and isinstance(forwarder, forwarder_type):
        return "hosted"
    # Test doubles and pre-diagnostic shims may not expose concrete types.
    return "unknown"


def _harden_config_with_diagnostics(
    root: Path, client_host: Optional[str]
) -> bool:
    startup_diagnostics.append_event(
        root,
        "config_hardening_started",
        "config",
        client_host=_resolved_client_host(client_host),
        outcome="started",
    )
    try:
        result = config.harden_existing_windows_config(Path(root) / "config.json")
    except (ShellError, OSError, ValueError) as exc:
        startup_diagnostics.append_event(
            root,
            "launcher_failed",
            "config",
            client_host=_resolved_client_host(client_host),
            outcome="error",
            reason_code=startup_diagnostics.classify_exception(exc),
            error_type=type(exc).__name__,
        )
        raise
    startup_diagnostics.append_event(
        root,
        "config_hardening_completed",
        "config",
        client_host=_resolved_client_host(client_host),
        outcome="success" if result else "skipped",
    )
    return result


def _repair_codex_skill_routes(root: Path) -> bool:
    """Best-effort migration for users who update without reinstalling.

    Skill links live outside the managed Git checkout, so landing a release
    cannot create the newly supported Codex routes by itself.  A refusal here
    must never make the already-verified MCP unavailable: installer ownership
    checks fail closed, Doctor reports the missing routes, and the shim still
    serves the known-good release.
    """

    try:
        # Lazy import keeps the normal launcher module graph small and avoids
        # loading installer-only code until the admitted managed path needs it.
        from . import install as install_module

        routed = set(install_module.repair_codex_skill_routes(root))
        from .codex_routing import REPLACEMENT_SKILLS

        return set(REPLACEMENT_SKILLS).issubset(routed)
    except Exception as exc:  # aqg: top-level boundary — optional repair must never gate MCP
        _log("could not repair Codex skill routes: %s" % exc)
        return False


def _retire_codex_global_routing(root: Path) -> None:
    """Best-effort one-way cleanup after a managed release is confirmed running."""

    try:
        from . import codex_routing

        result = codex_routing.retire_routing(require_skills_root=root)
        if result["action"] == "removed":
            _log("retired the legacy Codex global AGENTS.md block")
    except Exception as exc:  # aqg: top-level boundary — legacy cleanup must never gate MCP
        _log("could not retire the legacy Codex global AGENTS.md block: %s" % exc)


def _finalize_journal(root: Path) -> Optional[update_transaction.UpdateResult]:
    return update_transaction.finalize_present_journal(root, lock_timeout_seconds=0.0)


def _updates_enabled(root: Path) -> bool:
    try:
        update_transaction._require_protocol_ready(Path(root))
    except (ShellError, OSError, ValueError):
        return False
    return True


def _managed_control_present(root: Path) -> bool:
    """Distinguish a legacy copied install from an attempted managed install.

    An entirely absent control plane is the one compatibility case.  Partial
    control state is not treated as legacy; it must enter the fail-closed
    managed path so corruption cannot silently bypass validation.
    """

    root = Path(root)
    paths = (
        root / ".managed-install.json",
        root / updater.UPDATE_STATE_RELATIVE_PATH,
        root / update_transaction.PROTOCOL_READY_RELATIVE_PATH,
        root / update_coordination.UPDATE_JOURNAL_RELATIVE_PATH,
    )
    return any(path.exists() or update_coordination._is_link_like(path) for path in paths)


def _configured_official_remote_names(root: Path) -> tuple:
    """Official remotes actually configured on this checkout, in priority
    order (github before gitee), filtered to the ones present — never an
    unfiltered or reordered list."""
    remotes = updater._read_remotes(updater._GitReader(root))
    return tuple(
        name for name, _url in managed_install.OFFICIAL_REMOTE_URLS if name in remotes
    )


def _attempt_update(
    root: Path,
    trusted_keys: Mapping[str, object],
    *,
    deadline: float,
) -> update_transaction.UpdateResult:
    state = updater._read_update_state(root)
    try:
        acquired = release_acquisition.discover_release(
            state, trusted_keys, deadline=deadline
        )
    except release_acquisition.ReleaseTransportError:
        # Anonymous HTTPS mirrors are unreachable — most likely this
        # repository is still private. Knowingly accepted, private-beta-only
        # exception: fall back to whatever git credentials are already
        # ambient on this machine (see discover_release_via_git's docstring
        # for the full rationale and its boundary). Once the repository is
        # public the anonymous mirrors above succeed first and this branch
        # stops being reached at all.
        _log(
            "anonymous HTTPS mirrors unreachable — falling back to locally "
            "configured git credentials to check for an update"
        )
        acquired = release_acquisition.discover_release_via_git(
            root, _configured_official_remote_names(root), state, trusted_keys,
            deadline=deadline,
        )
    if not (
        acquired.manifest.commit == state.last_release_commit
        and release_contract.manifest_sha256(acquired.manifest)
        == state.last_manifest_sha256
    ):
        release_acquisition.fetch_release_objects(root, acquired, deadline=deadline)
    # Once mutation starts it must reach a durable commit or rollback boundary;
    # the acquisition deadline decides whether it is safe to start, not whether
    # an in-flight filesystem transaction may be killed halfway through.
    release_acquisition._remaining(deadline)
    return update_transaction.apply_present_update(
        root,
        acquired.manifest,
        acquired.signature,
        trusted_keys,
        source=acquired.source.name,
        lock_timeout_seconds=0.0,
    )


def _load_candidate_shim(root: Path) -> ModuleType:
    importlib.invalidate_caches()
    sys.modules.pop("installer.shim", None)
    spec = importlib.util.find_spec("installer.shim")
    origin = spec.origin if spec is not None else None
    if not isinstance(origin, str):
        raise ShellError("managed shim has no importable filesystem identity")
    try:
        resolved_origin = Path(origin).resolve(strict=True)
        resolved_origin.relative_to(Path(root).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ShellError("managed shim import target is outside the managed installation") from exc
    if update_coordination._is_link_like(Path(origin)):
        raise ShellError("managed shim import target must not be a link or reparse point")
    shim = importlib.import_module("installer.shim")
    module_file = getattr(shim, "__file__", None)
    if not isinstance(module_file, str):
        raise ShellError("loaded shim does not have a filesystem identity")
    try:
        Path(module_file).resolve(strict=True).relative_to(Path(root).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ShellError("loaded shim is outside the managed installation") from exc
    return shim


def _verify_managed_candidate(root: Path, commit: str, version: str) -> None:
    """Prove the checkout is the protected clean release before importing it."""

    update_transaction._require_protocol_ready(root)
    state = updater._read_update_state(root)
    if state.last_release_commit != commit or state.last_version != version:
        raise ShellError("managed checkout does not match the protected release state")
    if not update_transaction._tracked_tree_is_clean(root):
        raise ShellError("managed checkout has tracked changes; refusing to import the shim")


def _head_commit(root: Path) -> str:
    reader = updater._GitReader(Path(root))
    _code, output = reader.run("head")
    return updater._single_commit(output, "managed HEAD")


def _version(root: Path) -> str:
    try:
        value = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ShellError("managed VERSION is unreadable") from exc
    if not updater._VERSION_RE.fullmatch(value):
        raise ShellError("managed VERSION is invalid")
    return value


def _forwarder_from_config(shim: ModuleType):
    """Use Lite only when the loaded shim explicitly classifies activation state."""
    activation_error = getattr(shim, "ActivationRequiredError", None)
    if not isinstance(activation_error, type) or not issubclass(
        activation_error, ShellError
    ):
        return shim.Forwarder.from_config()
    try:
        return shim.Forwarder.from_config()
    except activation_error as exc:
        lite_forwarder = getattr(shim, "LiteForwarder", None)
        if lite_forwarder is None:
            raise
        selected = lite_forwarder()
        try:
            selected._startup_reason_code = getattr(
                exc, "reason_code", "activation_required"
            )
        except (AttributeError, TypeError):
            pass
        return selected


def _serve_shim(
    root: Path,
    *,
    shim: Optional[ModuleType] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    client_host: Optional[str] = None,
) -> int:
    # A completed updater may win between the first HEAD read and admission.
    # Retry after admission if that happened; once the lease exists, new
    # mutations are excluded until serve exits.
    for _attempt in range(2):
        startup_diagnostics.append_event(
            root,
            "shim_admission_started",
            "shim_admission",
            client_host=_resolved_client_host(client_host),
            attempt=_attempt + 1,
            outcome="started",
        )
        commit = _head_commit(root)
        version = _version(root)
        with update_coordination.shim_session_lease(root, commit):
            if _head_commit(root) != commit or _version(root) != version:
                continue
            _verify_managed_candidate(root, commit, version)
            startup_diagnostics.append_event(
                root,
                "shim_candidate_verified",
                "shim_admission",
                client_host=_resolved_client_host(client_host),
                attempt=_attempt + 1,
                outcome="success",
            )
            codex_skills_ready = _repair_codex_skill_routes(root)
            selected = shim if shim is not None else _load_candidate_shim(root)
            forwarder = _forwarder_from_config(selected)
            forwarder_kind = _forwarder_kind(selected, forwarder)
            startup_diagnostics.append_event(
                root,
                "forwarder_selected",
                "shim_admission",
                client_host=_resolved_client_host(client_host),
                attempt=_attempt + 1,
                forwarder=forwarder_kind,
                outcome="success",
                reason_code=getattr(forwarder, "_startup_reason_code", None),
            )

            def confirm_running() -> None:
                try:
                    update_transaction.mark_running_release(
                        root, commit, version, lock_timeout_seconds=0.0
                    )
                    # New registrations identify their host.  ``None`` retains the one-time
                    # migration path for older Codex entries that predate host tagging; an
                    # explicitly different host never scans or changes Codex global state.
                    if codex_skills_ready and _resolved_client_host(client_host) in {
                        None,
                        "codex",
                    }:
                        _retire_codex_global_routing(root)
                except (ShellError, OSError, ValueError) as exc:
                    # This is diagnostic state, not an availability gate.  A
                    # concurrent launcher/updater may briefly own the install
                    # lock; doctor will keep reporting the release as
                    # unconfirmed until a later session records it.
                    _log("could not confirm the running release: %s" % exc)

            serve_kwargs = {}
            serve_kwargs.update(_client_host_kwargs(selected, client_host))
            serve_kwargs.update(
                _startup_diagnostic_kwargs(selected, root, client_host)
            )
            result = selected.serve(
                forwarder,
                stdin=stdin,
                stdout=stdout,
                on_ready=confirm_running,
                **serve_kwargs,
            )
            startup_diagnostics.append_event(
                root,
                "launcher_exited",
                "launcher",
                client_host=_resolved_client_host(client_host),
                outcome="clean_exit",
            )
            return result
    raise ShellError("managed checkout changed repeatedly during shim admission")


def _handoff_to_fresh_launcher(
    root: Path, *, client_host: Optional[str] = None
) -> int:
    """Serve from a fresh interpreter while inheriting the MCP stdio handles."""

    command = [
        sys.executable,
        "-m",
        "installer.launcher",
        "--managed-root",
        str(Path(root)),
        "--serve-only",
    ]
    run_options = {
        "cwd": str(Path(root)),
        "check": False,
        "shell": False,
    }
    if client_host:
        environment = os.environ.copy()
        environment[CLIENT_HOST_ENV] = client_host
        run_options["env"] = environment
    completed = subprocess.run(
        command,
        **run_options,
    )
    return int(completed.returncode)


def launch(
    root: Optional[Path] = None,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    startup_budget_seconds: float = STARTUP_UPDATE_BUDGET_SECONDS,
    client_host: Optional[str] = None,
) -> int:
    """Recover, update within one budget, then serve the selected known-good shim."""

    managed_root = Path(root or config.managed_component_root("decision-engine"))
    managed_install._require_fixed_managed_root(managed_root)
    startup_diagnostics.append_event(
        managed_root,
        "launcher_started",
        "launcher",
        client_host=_resolved_client_host(client_host),
        outcome="started",
    )
    _harden_config_with_diagnostics(managed_root, client_host)
    if not _managed_control_present(managed_root):
        return _serve_legacy_shim(
            managed_root,
            stdin=stdin,
            stdout=stdout,
            client_host=client_host,
        )
    initial_commit = _head_commit(managed_root)
    wait_budget = max(0.0, float(startup_budget_seconds)) + STARTUP_LEADER_GRACE_SECONDS
    startup_diagnostics.append_event(
        managed_root,
        "startup_gate_entered",
        "startup_gate",
        client_host=_resolved_client_host(client_host),
        outcome="started",
    )
    try:
        with update_coordination.startup_update_gate(
            managed_root, timeout_seconds=wait_budget
        ) as startup_gate:
            startup_diagnostics.append_event(
                managed_root,
                "startup_gate_acquired",
                "startup_gate",
                client_host=_resolved_client_host(client_host),
                outcome="success",
            )
            # Finalization is local and idempotent. Every lock successor runs it
            # so a follower can take over a journal left by a crashed leader.
            recovery = _finalize_journal(managed_root)
            startup_diagnostics.append_event(
                managed_root,
                "recovery_completed",
                "recovery",
                client_host=_resolved_client_host(client_host),
                outcome="success" if recovery is not None else "skipped",
            )
            suppress_update = recovery is not None and recovery.status in {
                "repair_required",
                "retry_pending",
                "deferred_active_session",
                "skipped_locked",
            }
            # A launcher that waited observed another complete startup-update
            # attempt. After takeover/finalization it skips only redundant
            # network discovery, then rereads the checkout for admission.
            if not getattr(startup_gate, "waited", False) and not suppress_update:
                if _updates_enabled(managed_root):
                    try:
                        update_started = time.monotonic()
                        startup_diagnostics.append_event(
                            managed_root,
                            "update_started",
                            "update",
                            client_host=_resolved_client_host(client_host),
                            outcome="started",
                        )
                        deadline = time.monotonic() + max(
                            0.0, float(startup_budget_seconds)
                        )
                        state = updater._read_update_state(managed_root)
                        if _head_commit(managed_root) != state.last_release_commit:
                            raise ShellError(
                                "managed HEAD differs from the protected release state"
                            )
                        trusted_keys = load_trusted_release_keys(
                            managed_root,
                            deadline=deadline,
                            expected_commit=state.last_release_commit,
                        )
                        if trusted_keys:
                            _attempt_update(managed_root, trusted_keys, deadline=deadline)
                        startup_diagnostics.append_event(
                            managed_root,
                            "update_completed",
                            "update",
                            client_host=_resolved_client_host(client_host),
                            elapsed_ms=int((time.monotonic() - update_started) * 1000),
                            outcome="success" if trusted_keys else "skipped",
                        )
                    except (ShellError, OSError, ValueError) as exc:
                        # MCP stdout is protocol-only. The current known-good
                        # release remains available after a bounded refusal.
                        _log(str(exc))
                        startup_diagnostics.append_event(
                            managed_root,
                            "update_refused",
                            "update",
                            client_host=_resolved_client_host(client_host),
                            elapsed_ms=int((time.monotonic() - update_started) * 1000),
                            outcome="refused",
                            reason_code=startup_diagnostics.classify_exception(exc),
                            error_type=type(exc).__name__,
                        )
    except update_coordination.InstallTransactionBusy as exc:
        startup_diagnostics.append_event(
            managed_root,
            "launcher_failed",
            "startup_gate",
            client_host=_resolved_client_host(client_host),
            outcome="error",
            reason_code="startup_gate_timeout",
            error_type=type(exc).__name__,
        )
        raise ShellError("startup update coordination timed out") from exc
    if _head_commit(managed_root) != initial_commit:
        startup_diagnostics.append_event(
            managed_root,
            "handoff_started",
            "handoff",
            client_host=_resolved_client_host(client_host),
            outcome="started",
        )
        if client_host:
            return _handoff_to_fresh_launcher(
                managed_root, client_host=client_host
            )
        return _handoff_to_fresh_launcher(managed_root)
    return _serve_shim(
        managed_root,
        stdin=stdin,
        stdout=stdout,
        client_host=client_host,
    )


def _serve_dev_shim(
    root: Path,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    client_host: Optional[str] = None,
) -> int:
    """Explicit developer mode: no marker, network, updater, reset or managed lease."""

    shim = _load_candidate_shim(root)
    return shim.serve(
        _forwarder_from_config(shim),
        stdin=stdin,
        stdout=stdout,
        **_client_host_kwargs(shim, client_host),
    )


def _serve_legacy_shim(
    root: Path,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    client_host: Optional[str] = None,
) -> int:
    """Lease a launcher-started legacy shim so activation cannot race it."""

    try:
        commit = _head_commit(root)
    except ShellError:
        # Copied/archive installs may contain a runnable package without Git
        # metadata.  The all-zero sentinel is valid lease metadata and still
        # makes activation wait for this legacy process to exit.
        commit = "0" * 40
    with update_coordination.shim_session_lease(root, commit):
        if _managed_control_present(root):
            raise ShellError("managed activation began during legacy shim admission")
        shim = _load_candidate_shim(root)
        return shim.serve(
            _forwarder_from_config(shim),
            stdin=stdin,
            stdout=stdout,
            **_client_host_kwargs(shim, client_host),
        )


def _reconfigure_stdio_utf8() -> None:
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv: Optional[list] = None) -> int:
    _reconfigure_stdio_utf8()
    parser = argparse.ArgumentParser(
        prog="de-launcher",
        description="Bounded managed updater and Decision Engine MCP launcher",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--managed-root", type=Path)
    mode.add_argument("--dev-root", type=Path)
    parser.add_argument("--serve-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--client-host",
        default=None,
        help="MCP host family used for popup follow-up routing",
    )
    args = parser.parse_args(argv)
    client_host = _resolved_client_host(args.client_host)
    diagnostic_root = None
    if args.dev_root is None:
        diagnostic_root = Path(
            args.managed_root or config.managed_component_root("decision-engine")
        )
    try:
        if args.dev_root is not None:
            return _serve_dev_shim(args.dev_root, client_host=client_host)
        if args.serve_only:
            root = Path(args.managed_root or config.managed_component_root("decision-engine"))
            managed_install._require_fixed_managed_root(root)
            _harden_config_with_diagnostics(root, client_host)
            return _serve_shim(root, client_host=client_host)
        return launch(args.managed_root, client_host=client_host)
    except (ShellError, OSError, ValueError) as exc:
        if diagnostic_root is not None:
            startup_diagnostics.append_event(
                diagnostic_root,
                "launcher_failed",
                "launcher",
                client_host=client_host,
                outcome="error",
                reason_code=startup_diagnostics.classify_exception(exc),
                error_type=type(exc).__name__,
            )
        _log(str(exc))
        return 1
    except Exception as exc:
        if diagnostic_root is not None:
            startup_diagnostics.append_event(
                diagnostic_root,
                "launcher_failed",
                "launcher",
                client_host=client_host,
                outcome="error",
                reason_code="unexpected_error",
                error_type=type(exc).__name__,
            )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
