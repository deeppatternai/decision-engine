"""Host-neutral lifecycle dispatch and release-state contracts.

The dispatcher owns vocabulary and validation.  Host adapters own only their
format-specific mutation mechanics and must use paths derived from their
``AgentHostSpec`` rather than accepting mutable paths from ownership records.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, FrozenSet, Mapping, Optional


if __name__ == "__main__":
    # `python -m` starts this file as `__main__`. Publish that same module
    # object under its package name before an adapter imports the contract, so
    # both sides share the exact dataclass identities without a second import.
    sys.modules["installer.client_host_lifecycle"] = sys.modules[__name__]


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^([1-9][0-9]{0,18})-([0-9a-f]{40})$")
_OPERATIONS = frozenset({"repair", "update", "uninstall", "status"})
_RESULT_STATUSES = frozenset(
    {
        "completed",
        "unchanged",
        "busy",
        "refused",
        "repair_required",
        "requires_newer_installer",
        "unsupported_for_host",
    }
)
_RELEASE_STATES = frozenset(
    {"current", "update_required", "reload_required", "unknown", "invalid"}
)


@dataclass(frozen=True)
class LifecycleResult:
    host_family: str
    operation: str
    status: str
    reason: Optional[str] = None
    details: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class HostLifecycleAdapter:
    adapter_id: str
    host_family: str
    supported_operations: FrozenSet[str]
    execute: Callable[[str, Mapping[str, Any]], LifecycleResult]


@dataclass(frozen=True)
class ReleaseEvidence:
    target_commit: Optional[str]
    installed_commit: Optional[str]
    running_commit: Optional[str]


@dataclass(frozen=True)
class ReleaseAssessment:
    state: str


_BUILTIN_LOADERS = MappingProxyType(
    {
        "cursor-owned-v1": (
            "installer.cursor_host_lifecycle",
            "build_adapter",
        ),
    }
)


def _validate_adapter(adapter: HostLifecycleAdapter) -> None:
    if not isinstance(adapter, HostLifecycleAdapter):
        raise ValueError("lifecycle adapter is invalid")
    if (
        not adapter.adapter_id
        or not adapter.host_family
        or not adapter.supported_operations
        or not adapter.supported_operations.issubset(_OPERATIONS)
        or not callable(adapter.execute)
    ):
        raise ValueError("lifecycle adapter contract is invalid")


def _validate_result(
    result: LifecycleResult,
    *,
    host_family: str,
    operation: str,
) -> LifecycleResult:
    if (
        not isinstance(result, LifecycleResult)
        or result.host_family != host_family
        or result.operation != operation
        or result.status not in _RESULT_STATUSES
        or (result.reason is not None and not isinstance(result.reason, str))
        or not isinstance(result.details, Mapping)
    ):
        raise ValueError("lifecycle adapter returned an invalid result")
    return result


def dispatch_operation(
    host_family: str,
    operation: str,
    *,
    request: Mapping[str, Any],
    host_adapter_id: Optional[str],
    adapters: Mapping[str, HostLifecycleAdapter],
) -> LifecycleResult:
    """Dispatch one operation without host-name branches."""

    if not isinstance(host_family, str) or not host_family:
        raise ValueError("lifecycle host family is invalid")
    if operation not in _OPERATIONS:
        raise ValueError("lifecycle operation is invalid")
    if not isinstance(request, Mapping):
        raise ValueError("lifecycle request is invalid")
    if host_adapter_id is None:
        return LifecycleResult(
            host_family=host_family,
            operation=operation,
            status="unsupported_for_host",
        )
    adapter = adapters.get(host_adapter_id)
    if adapter is None:
        raise ValueError("registered lifecycle adapter is unavailable")
    _validate_adapter(adapter)
    if adapter.adapter_id != host_adapter_id:
        raise ValueError("lifecycle adapter id does not match registration")
    if adapter.host_family != host_family:
        raise ValueError("lifecycle adapter host family does not match registration")
    if operation not in adapter.supported_operations:
        return LifecycleResult(
            host_family=host_family,
            operation=operation,
            status="unsupported_for_host",
        )
    result = adapter.execute(operation, MappingProxyType(dict(request)))
    return _validate_result(
        result,
        host_family=host_family,
        operation=operation,
    )


def _load_builtin_adapter(adapter_id: str) -> HostLifecycleAdapter:
    location = _BUILTIN_LOADERS.get(adapter_id)
    if location is None:
        raise ValueError("registered lifecycle adapter is unavailable")
    module_name, factory_name = location
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError("registered lifecycle adapter is unavailable")
    adapter = factory()
    _validate_adapter(adapter)
    return adapter


def dispatch_registered_operation(
    host_id: str,
    operation: str,
    *,
    request: Mapping[str, Any],
) -> LifecycleResult:
    from installer import mcp_config

    spec = mcp_config.CLIENT_SPECS.get(host_id)
    if spec is None:
        raise ValueError("lifecycle host is not registered")
    adapter_id = spec.lifecycle_adapter
    adapters = (
        {}
        if adapter_id is None
        else {adapter_id: _load_builtin_adapter(adapter_id)}
    )
    return dispatch_operation(
        spec.host_family,
        operation,
        request=request,
        host_adapter_id=adapter_id,
        adapters=adapters,
    )


def require_monotonic_release(
    installed_release_id: str,
    target_release_id: str,
) -> tuple[int, int]:
    installed = _RELEASE_ID_RE.fullmatch(installed_release_id or "")
    target = _RELEASE_ID_RE.fullmatch(target_release_id or "")
    if installed is None or target is None:
        raise ValueError("owned release id is invalid")
    installed_sequence = int(installed.group(1))
    target_sequence = int(target.group(1))
    if target_sequence <= installed_sequence:
        raise ValueError("target owned release must be newer")
    return installed_sequence, target_sequence


def require_authorized_child(path: Path, root: Path) -> Path:
    """Return an absolute child path with no existing link-like component."""

    authorized = Path(os.path.abspath(os.path.normpath(str(Path(root)))))
    candidate = Path(os.path.abspath(os.path.normpath(str(Path(path)))))
    try:
        common = Path(os.path.commonpath((str(authorized), str(candidate))))
    except ValueError as exc:
        raise ValueError("path is outside the authorized root") from exc
    if (
        os.path.normcase(str(common)) != os.path.normcase(str(authorized))
        or os.path.normcase(str(candidate)) == os.path.normcase(str(authorized))
    ):
        raise ValueError("path is outside the authorized root")
    from installer import managed_install

    try:
        managed_install._reject_link_components(authorized)
        managed_install._reject_link_components(candidate)
    except managed_install.ManagedInstallError as exc:
        raise ValueError("path contains an unauthorized link component") from exc
    return candidate


def assess_release_evidence(evidence: ReleaseEvidence) -> ReleaseAssessment:
    if not isinstance(evidence, ReleaseEvidence):
        raise ValueError("release evidence is invalid")
    values = (
        evidence.target_commit,
        evidence.installed_commit,
        evidence.running_commit,
    )
    if any(value is not None and not isinstance(value, str) for value in values):
        return ReleaseAssessment("invalid")
    if any(value is not None and not _COMMIT_RE.fullmatch(value) for value in values):
        return ReleaseAssessment("invalid")
    if evidence.target_commit is None or evidence.installed_commit is None:
        return ReleaseAssessment("unknown")
    if evidence.target_commit != evidence.installed_commit:
        return ReleaseAssessment("update_required")
    if evidence.running_commit is None:
        return ReleaseAssessment("unknown")
    if evidence.running_commit != evidence.installed_commit:
        return ReleaseAssessment("reload_required")
    return ReleaseAssessment("current")


def bind_owned_release(
    managed_evidence: ReleaseEvidence,
    installed_release_id: str,
) -> ReleaseEvidence:
    """Replace global installed evidence with one owned host generation."""

    if not isinstance(managed_evidence, ReleaseEvidence):
        raise ValueError("managed release evidence is invalid")
    match = _RELEASE_ID_RE.fullmatch(installed_release_id or "")
    installed_commit = match.group(2) if match is not None else "invalid"
    return ReleaseEvidence(
        target_commit=managed_evidence.target_commit,
        installed_commit=installed_commit,
        running_commit=managed_evidence.running_commit,
    )


def collect_managed_release_evidence(root: Path) -> Optional[ReleaseEvidence]:
    """Read one protected update-state snapshot; never expose paths or errors."""

    from installer import updater

    try:
        state = updater._read_update_state(Path(root))
    except (OSError, ValueError, updater.UpdateInspectionError):
        return None
    return ReleaseEvidence(
        target_commit=state.last_release_commit,
        installed_commit=state.last_release_commit,
        running_commit=state.running_commit,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Explicit lifecycle entrypoint; emits normalized, path-free JSON."""

    from installer import config, mcp_config

    parser = argparse.ArgumentParser(prog="de-client-host-lifecycle")
    parser.add_argument("operation", choices=sorted(_OPERATIONS))
    parser.add_argument("--host", required=True, choices=mcp_config.CLIENTS)
    parser.add_argument("--managed-root", type=Path)
    args = parser.parse_args(argv)
    root = args.managed_root or config.managed_component_root("decision-engine")
    try:
        result = dispatch_registered_operation(
            args.host,
            args.operation,
            request={"managed_root": Path(root)},
        )
    except (config.ShellError, OSError, RuntimeError, TypeError, ValueError) as exc:
        result = LifecycleResult(
            host_family=mcp_config.CLIENT_SPECS[args.host].host_family,
            operation=args.operation,
            status="refused",
            reason=type(exc).__name__,
        )
    print(
        json.dumps(
            {
                "host_family": result.host_family,
                "operation": result.operation,
                "status": result.status,
                "reason": result.reason,
                "details": dict(result.details),
            },
            sort_keys=True,
        )
    )
    return 0 if result.status in {"completed", "unchanged"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
