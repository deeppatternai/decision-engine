"""Per-transport client-host capability state.

This module is deliberately OS- and popup-free. Host-specific probes return a
``PreflightResult`` and a call-time ``VolatileResult``; the shim owns one gate
per stdio transport. Keeping the transition logic pure makes every fail-closed
branch testable without pretending CI executed Windows APIs.
"""

from __future__ import annotations

import ntpath
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Optional, Tuple, Union


class CapabilityStage(str, Enum):
    PENDING = "pending"
    PREFLIGHT_DENIED = "preflight-denied"
    READY_FOR_INITIALIZE = "ready-for-initialize"
    DENIED = "denied"
    ELIGIBLE = "eligible"
    REVOKED = "revoked"
    CLOSED = "closed"


@dataclass(frozen=True)
class PreflightResult:
    eligible: bool
    reason: str
    lease: Optional[Any] = None

    @classmethod
    def ready(cls, lease: Any) -> "PreflightResult":
        if lease is None or not callable(getattr(lease, "close", None)):
            raise ValueError("eligible preflight requires a closeable lease")
        return cls(True, "preflight-ready", lease)

    @classmethod
    def denied(cls, reason: str) -> "PreflightResult":
        if not isinstance(reason, str) or not reason:
            raise ValueError("denied preflight requires a stable reason")
        return cls(False, reason)


@dataclass(frozen=True)
class VolatileResult:
    allowed: bool
    reason: str
    revoke: bool = False

    @classmethod
    def available(cls) -> "VolatileResult":
        return cls(True, "granted")

    @classmethod
    def unavailable(cls, reason: str = "display-context-unavailable") -> "VolatileResult":
        return cls(False, reason)

    @classmethod
    def revoked(cls, reason: str) -> "VolatileResult":
        return cls(False, reason, True)


@dataclass(frozen=True)
class DisplayCheckResult:
    allowed: bool
    reason: str
    revoked: bool = False


@dataclass(frozen=True)
class CursorArgvRule:
    value: str
    match: str
    scope: str


@dataclass(frozen=True)
class CursorWindowsFixture:
    fixture_id: str
    capture_id: str
    enabled: bool
    expires_on: date
    client_name: str
    client_version: str
    roots_list_changed: bool
    parent_image_basename: str
    required_argv_rules: Tuple[CursorArgvRule, ...]
    forbidden_argv_rules: Tuple[CursorArgvRule, ...]
    max_ancestor_hops: int
    terminal_hop: int
    architecture: str
    terminal_image_basename: str
    terminal_known_roots: Tuple[str, ...]
    terminal_relative_paths: Tuple[str, ...]
    terminal_product: str
    terminal_company: str
    terminal_file_version: str
    window_class_names: Tuple[str, ...]
    release_sha256: str
    release_signer_subject: str
    release_capture_id: str


@dataclass(frozen=True)
class CursorProcessEvidence:
    pid: int
    image_basename: str
    argv: Tuple[str, ...]
    session_id: int
    creation_time: int
    alive: bool = True
    final_path: str = ""
    fixed_volume: bool = False
    reparse_target: bool = False
    product: str = ""
    company: str = ""
    file_version: str = ""


@dataclass(frozen=True)
class CursorWindowsEvidence:
    platform: str
    remote_environment: bool
    shim_session_id: int
    active_console_session_id: int
    current_process_creation_time: int
    ancestors: Tuple[CursorProcessEvidence, ...]
    known_roots: Mapping[str, str]
    terminal_lease: Optional[Any]
    runtime_architecture: str = "x64"


_FIXTURE_FIELDS = {
    "fixture_id",
    "capture_id",
    "enabled",
    "expires_on",
    "client_name",
    "client_version",
    "roots_list_changed",
    "parent_image_basename",
    "required_argv_rules",
    "forbidden_argv_rules",
    "max_ancestor_hops",
    "terminal_hop",
    "architecture",
    "terminal_image_basename",
    "terminal_known_roots",
    "terminal_relative_paths",
    "terminal_product",
    "terminal_company",
    "terminal_file_version",
    "window_class_names",
    "release_sha256",
    "release_signer_subject",
    "release_capture_id",
}
_ASCII_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEX_256 = re.compile(r"[A-Fa-f0-9]{64}\Z")
_KNOWN_ROOTS = frozenset({"LocalAppData", "ProgramFiles", "ProgramFilesX64"})
_CURSOR_PREFLIGHT_SLOTS = threading.BoundedSemaphore(value=4)
_PROBE_FAILED = object()


def _nonempty_string(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid Cursor fixture field: %s" % name)
    return value


def _string_tuple(data: Mapping[str, Any], name: str) -> Tuple[str, ...]:
    value = data.get(name)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid Cursor fixture field: %s" % name)
    return tuple(value)


def _load_argv_rules(value: Any, *, forbidden: bool) -> Tuple[CursorArgvRule, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Cursor fixture argv rules are required")
    rules = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"value", "match", "scope"}:
            raise ValueError("invalid Cursor fixture argv rule")
        rule_value = raw["value"]
        match = raw["match"]
        scope = raw["scope"]
        if (
            not isinstance(rule_value, str)
            or not rule_value
            or match not in ("exact", "casefold")
            or scope not in ("direct-parent", "any-hop", "terminal")
            or (forbidden and scope != "any-hop")
        ):
            raise ValueError("invalid Cursor fixture argv rule")
        rules.append(CursorArgvRule(rule_value, match, scope))
    return tuple(rules)


def load_cursor_windows_fixture(data: Mapping[str, Any]) -> CursorWindowsFixture:
    if not isinstance(data, Mapping) or set(data) != _FIXTURE_FIELDS:
        raise ValueError("Cursor fixture fields do not match the closed schema")
    fixture_id = _nonempty_string(data, "fixture_id")
    capture_id = _nonempty_string(data, "capture_id")
    release_capture_id = _nonempty_string(data, "release_capture_id")
    if (
        _ASCII_ID.fullmatch(fixture_id) is None
        or _ASCII_ID.fullmatch(capture_id) is None
        or release_capture_id != capture_id
    ):
        raise ValueError("Cursor fixture capture identity is invalid")
    if type(data.get("enabled")) is not bool:
        raise ValueError("Cursor fixture enabled must be boolean")
    if type(data.get("roots_list_changed")) is not bool:
        raise ValueError("Cursor fixture roots capability must be boolean")
    try:
        expires_on = date.fromisoformat(_nonempty_string(data, "expires_on"))
    except ValueError:
        raise ValueError("Cursor fixture expiry is invalid") from None
    max_hops = data.get("max_ancestor_hops")
    terminal_hop = data.get("terminal_hop")
    if (
        type(max_hops) is not int
        or not 1 <= max_hops <= 8
        or type(terminal_hop) is not int
        or not 1 <= terminal_hop <= max_hops
    ):
        raise ValueError("Cursor fixture hop bounds are invalid")
    architecture = _nonempty_string(data, "architecture")
    if architecture not in ("x64", "arm64"):
        raise ValueError("Cursor fixture architecture is invalid")
    known_roots = _string_tuple(data, "terminal_known_roots")
    if not set(known_roots) <= _KNOWN_ROOTS:
        raise ValueError("Cursor fixture known roots are invalid")
    relative_paths = _string_tuple(data, "terminal_relative_paths")
    for raw_path in relative_paths:
        path = PurePosixPath(raw_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("Cursor fixture relative path is invalid")
    window_classes = _string_tuple(data, "window_class_names")
    release_sha256 = _nonempty_string(data, "release_sha256")
    if _HEX_256.fullmatch(release_sha256) is None:
        raise ValueError("Cursor fixture release hash is invalid")
    terminal_basename = _nonempty_string(data, "terminal_image_basename")
    if terminal_basename.casefold() != "cursor.exe":
        raise ValueError("Cursor fixture terminal image is invalid")
    return CursorWindowsFixture(
        fixture_id=fixture_id,
        capture_id=capture_id,
        enabled=data["enabled"],
        expires_on=expires_on,
        client_name=_nonempty_string(data, "client_name"),
        client_version=_nonempty_string(data, "client_version"),
        roots_list_changed=data["roots_list_changed"],
        parent_image_basename=_nonempty_string(data, "parent_image_basename"),
        required_argv_rules=_load_argv_rules(
            data.get("required_argv_rules"), forbidden=False
        ),
        forbidden_argv_rules=_load_argv_rules(
            data.get("forbidden_argv_rules"), forbidden=True
        ),
        max_ancestor_hops=max_hops,
        terminal_hop=terminal_hop,
        architecture=architecture,
        terminal_image_basename=terminal_basename,
        terminal_known_roots=known_roots,
        terminal_relative_paths=relative_paths,
        terminal_product=_nonempty_string(data, "terminal_product"),
        terminal_company=_nonempty_string(data, "terminal_company"),
        terminal_file_version=_nonempty_string(data, "terminal_file_version"),
        window_class_names=window_classes,
        release_sha256=release_sha256.lower(),
        release_signer_subject=_nonempty_string(data, "release_signer_subject"),
        release_capture_id=release_capture_id,
    )


PRODUCTION_CURSOR_WINDOWS_FIXTURE = load_cursor_windows_fixture(
    {
        "fixture_id": "cursor-windows-local-3538-v1",
        "capture_id": "cursor-3538-agent-20260730-paired",
        "enabled": False,
        "expires_on": "2026-08-06",
        "client_name": "cursor-vscode",
        "client_version": "1.0.0",
        "roots_list_changed": False,
        "parent_image_basename": "Cursor.exe",
        "required_argv_rules": [
            {"value": "--type=utility", "match": "exact", "scope": "direct-parent"}
        ],
        "forbidden_argv_rules": [
            {"value": "--print", "match": "exact", "scope": "any-hop"}
        ],
        "max_ancestor_hops": 2,
        "terminal_hop": 2,
        "architecture": "x64",
        "terminal_image_basename": "Cursor.exe",
        "terminal_known_roots": ["ProgramFiles"],
        "terminal_relative_paths": ["cursor/Cursor.exe"],
        "terminal_product": "Cursor",
        "terminal_company": "Anysphere",
        "terminal_file_version": "3.5.38",
        "window_class_names": ["Chrome_WidgetWin_1"],
        "release_sha256": (
            "703c886461176d1f9a60b79af396814f"
            "da4b48ef24d27ba06a0bf1c593874e54"
        ),
        "release_signer_subject": (
            'CN="Anysphere, Inc.", O="Anysphere, Inc.", '
            "L=San Francisco, S=California, C=US"
        ),
        "release_capture_id": "cursor-3538-agent-20260730-paired",
    }
)


def _argv_match(rule: CursorArgvRule, argv: Tuple[str, ...]) -> bool:
    if rule.match == "exact":
        return rule.value in argv
    expected = rule.value.casefold()
    return any(item.casefold() == expected for item in argv)


def _deny_evidence(evidence: CursorWindowsEvidence, reason: str) -> PreflightResult:
    TransportCapabilityGate._close_lease(evidence.terminal_lease)
    return PreflightResult.denied(reason)


def evaluate_cursor_windows_preflight(
    fixture: CursorWindowsFixture,
    evidence: CursorWindowsEvidence,
    *,
    today: Optional[date] = None,
) -> PreflightResult:
    """Pure, precedence-ordered evaluator for immutable Windows probe evidence."""
    if evidence.platform != "win32":
        return _deny_evidence(evidence, "unsupported-platform")
    if evidence.remote_environment:
        return _deny_evidence(evidence, "remote-environment")
    if (
        evidence.shim_session_id in (0, 0xFFFFFFFF)
        or evidence.active_console_session_id in (0, 0xFFFFFFFF)
        or evidence.shim_session_id != evidence.active_console_session_id
    ):
        return _deny_evidence(evidence, "inactive-session")
    if not fixture.enabled:
        return _deny_evidence(evidence, "fixture-disabled")
    if (today or date.today()) > fixture.expires_on:
        return _deny_evidence(evidence, "fixture-expired")
    if evidence.runtime_architecture != fixture.architecture:
        return _deny_evidence(evidence, "binary-location-mismatch")
    ancestors = evidence.ancestors
    if not ancestors or len(ancestors) > fixture.max_ancestor_hops:
        return _deny_evidence(evidence, "ancestry-invalid")
    if len(ancestors) < fixture.terminal_hop:
        return _deny_evidence(evidence, "parent-unavailable")
    seen_pids = set()
    child_creation = evidence.current_process_creation_time
    for process in ancestors:
        if (
            type(process.pid) is not int
            or process.pid <= 0
            or process.pid in seen_pids
            or not process.alive
            or process.session_id != evidence.shim_session_id
            or process.creation_time >= child_creation
        ):
            return _deny_evidence(evidence, "parent-lifetime-invalid")
        seen_pids.add(process.pid)
        child_creation = process.creation_time
    for rule in fixture.forbidden_argv_rules:
        if any(_argv_match(rule, process.argv) for process in ancestors):
            return _deny_evidence(evidence, "cli-mode")
    if ancestors[0].image_basename.casefold() != fixture.parent_image_basename.casefold():
        return _deny_evidence(evidence, "process-role-mismatch")
    terminal = ancestors[fixture.terminal_hop - 1]
    for rule in fixture.required_argv_rules:
        if rule.scope == "direct-parent":
            candidates = (ancestors[0],)
        elif rule.scope == "terminal":
            candidates = (terminal,)
        else:
            candidates = ancestors
        if not any(_argv_match(rule, process.argv) for process in candidates):
            return _deny_evidence(evidence, "process-role-mismatch")
    if terminal.image_basename.casefold() != fixture.terminal_image_basename.casefold():
        return _deny_evidence(evidence, "ancestry-invalid")
    if not terminal.fixed_volume or terminal.reparse_target or not terminal.final_path:
        return _deny_evidence(evidence, "binary-location-mismatch")
    allowed_paths = set()
    for root_name in fixture.terminal_known_roots:
        root = evidence.known_roots.get(root_name)
        if not isinstance(root, str) or not root:
            continue
        for relative in fixture.terminal_relative_paths:
            allowed_paths.add(
                ntpath.normcase(ntpath.normpath(ntpath.join(root, relative)))
            )
    for process in ancestors[: fixture.terminal_hop]:
        if (
            process.image_basename.casefold()
            != fixture.terminal_image_basename.casefold()
        ):
            continue
        if (
            not process.fixed_volume
            or process.reparse_target
            or not process.final_path
            or ntpath.normcase(ntpath.normpath(process.final_path))
            not in allowed_paths
        ):
            return _deny_evidence(evidence, "binary-location-mismatch")
        if (
            process.product != fixture.terminal_product
            or process.company != fixture.terminal_company
            or process.file_version != fixture.terminal_file_version
        ):
            return _deny_evidence(evidence, "binary-version-mismatch")
    if evidence.terminal_lease is None or not callable(
        getattr(evidence.terminal_lease, "close", None)
    ):
        return PreflightResult.denied("parent-unavailable")
    return PreflightResult.ready(evidence.terminal_lease)


def cursor_initialize_matches_fixture(
    params: Mapping[str, Any],
    fixture: CursorWindowsFixture = PRODUCTION_CURSOR_WINDOWS_FIXTURE,
) -> bool:
    if not fixture.enabled or not isinstance(params, Mapping):
        return False
    client_info = params.get("clientInfo")
    capabilities = params.get("capabilities")
    if not isinstance(client_info, Mapping) or not isinstance(capabilities, Mapping):
        return False
    roots = capabilities.get("roots")
    return (
        isinstance(roots, Mapping)
        and client_info.get("name") == fixture.client_name
        and client_info.get("version") == fixture.client_version
        and roots.get("listChanged") is fixture.roots_list_changed
    )


def run_cursor_windows_preflight(
    *,
    fixture: CursorWindowsFixture = PRODUCTION_CURSOR_WINDOWS_FIXTURE,
    probes: Any = None,
    platform: Optional[str] = None,
    timeout_s: float = 0.75,
) -> PreflightResult:
    """Bounded native-probe boundary; the checked-in disabled fixture short-circuits."""
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return PreflightResult.denied("unsupported-platform")
    if not fixture.enabled:
        return PreflightResult.denied("fixture-disabled")
    if probes is None or not callable(getattr(probes, "collect", None)):
        return PreflightResult.denied("os-probe-failed")
    deadline = time.monotonic() + max(0.0, min(float(timeout_s), 0.75))
    if not _CURSOR_PREFLIGHT_SLOTS.acquire(blocking=False):
        return PreflightResult.denied("preflight-saturated")

    done = threading.Event()
    publication_lock = threading.Lock()
    publication: dict[str, Any] = {"abandoned": False}

    def collect() -> None:
        observed: Any = _PROBE_FAILED
        try:
            observed = probes.collect(fixture, deadline)
        except BaseException:  # aqg: top-level boundary -- OS probe failure denies capability
            # aqg: OS-probe worker boundary -- all probe failures map to one closed code.
            observed = _PROBE_FAILED
        late_evidence = None
        try:
            with publication_lock:
                if publication["abandoned"]:
                    if isinstance(observed, CursorWindowsEvidence):
                        late_evidence = observed
                else:
                    publication["value"] = observed
        finally:
            if late_evidence is not None:
                _deny_evidence(late_evidence, "preflight-timeout")
            done.set()
            _CURSOR_PREFLIGHT_SLOTS.release()

    try:
        threading.Thread(
            target=collect,
            name="de-cursor-preflight",
            daemon=True,
        ).start()
    except BaseException:  # aqg: top-level boundary -- thread-start failure denies capability
        # aqg: thread-start boundary -- a scheduling failure must release capacity.
        _CURSOR_PREFLIGHT_SLOTS.release()
        return PreflightResult.denied("os-probe-failed")

    remaining = max(0.0, deadline - time.monotonic())
    if not done.wait(remaining):
        with publication_lock:
            observed = publication.get("value", _PROBE_FAILED)
            if "value" not in publication:
                publication["abandoned"] = True
                return PreflightResult.denied("preflight-timeout")
    else:
        with publication_lock:
            observed = publication.get("value", _PROBE_FAILED)

    if not isinstance(observed, CursorWindowsEvidence):
        return PreflightResult.denied("os-probe-failed")
    if time.monotonic() > deadline:
        return _deny_evidence(observed, "preflight-timeout")
    return evaluate_cursor_windows_preflight(fixture, observed)


class TransportCapabilityGate:
    """Locked capability state for one stdio transport."""

    def __init__(
        self,
        *,
        stage: CapabilityStage,
        display: bool,
        followup: bool,
        stopper: bool,
        reason: str,
        conditional: bool,
    ) -> None:
        self._lock = threading.Lock()
        self._stage = stage
        self._display = display
        self._followup = followup
        self._stopper = stopper
        self._reason = reason
        self._conditional = conditional
        self._lease: Optional[Any] = None

    @classmethod
    def static(
        cls, *, display: bool, followup: bool, stopper: bool
    ) -> "TransportCapabilityGate":
        return cls(
            stage=CapabilityStage.ELIGIBLE if display else CapabilityStage.DENIED,
            display=display,
            followup=followup,
            stopper=stopper,
            reason="static",
            conditional=False,
        )

    @classmethod
    def conditional(
        cls, *, followup: bool, stopper: bool
    ) -> "TransportCapabilityGate":
        return cls(
            stage=CapabilityStage.PENDING,
            display=False,
            followup=followup,
            stopper=stopper,
            reason="pending",
            conditional=True,
        )

    @property
    def stage(self) -> CapabilityStage:
        with self._lock:
            return self._stage

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def display_enabled(self) -> bool:
        with self._lock:
            return self._display and self._stage == CapabilityStage.ELIGIBLE

    @property
    def followup_enabled(self) -> bool:
        return self._followup

    @property
    def stopper_enabled(self) -> bool:
        return self._stopper

    def complete_preflight(self, result: PreflightResult) -> bool:
        late_lease = None
        with self._lock:
            if not self._conditional or self._stage != CapabilityStage.PENDING:
                late_lease = result.lease
                accepted = False
            elif result.eligible:
                self._lease = result.lease
                self._stage = CapabilityStage.READY_FOR_INITIALIZE
                self._reason = result.reason
                accepted = True
            else:
                self._stage = CapabilityStage.PREFLIGHT_DENIED
                self._reason = result.reason
                accepted = True
        self._close_lease(late_lease)
        return accepted

    def finalize_initialize(self, *, matches: bool) -> bool:
        lease = None
        with self._lock:
            if self._stage != CapabilityStage.READY_FOR_INITIALIZE:
                return False
            if matches:
                self._stage = CapabilityStage.ELIGIBLE
                self._display = True
                self._reason = "granted"
            else:
                self._stage = CapabilityStage.DENIED
                self._reason = "initialize-mismatch"
                lease, self._lease = self._lease, None
        self._close_lease(lease)
        return True

    def fail_initialize(self) -> bool:
        lease = None
        with self._lock:
            if self._stage != CapabilityStage.READY_FOR_INITIALIZE:
                return False
            self._stage = CapabilityStage.DENIED
            self._reason = "initialize-failed"
            lease, self._lease = self._lease, None
        self._close_lease(lease)
        return True

    def check_display(
        self,
        check: Optional[
            Union[VolatileResult, Callable[[], VolatileResult]]
        ] = None,
    ) -> DisplayCheckResult:
        with self._lock:
            if self._stage != CapabilityStage.ELIGIBLE or not self._display:
                return DisplayCheckResult(False, "display-capability-disabled")
            conditional = self._conditional
            retained_lease = self._lease
        if not conditional:
            return DisplayCheckResult(True, "granted")

        if check is None:
            native_check = getattr(retained_lease, "check_display", None)
            if not callable(native_check):
                return DisplayCheckResult(
                    False, "display-context-unavailable"
                )
            try:
                observed = native_check()
            except BaseException:  # aqg: top-level boundary -- native volatile failure denies
                observed = VolatileResult.unavailable()
        else:
            observed = check() if callable(check) else check
        if not isinstance(observed, VolatileResult):
            return DisplayCheckResult(False, "display-context-unavailable")
        if observed.allowed:
            return DisplayCheckResult(True, observed.reason)
        if not observed.revoke:
            return DisplayCheckResult(False, observed.reason)

        lease = None
        with self._lock:
            if self._stage != CapabilityStage.ELIGIBLE:
                return DisplayCheckResult(False, "display-capability-disabled")
            self._stage = CapabilityStage.REVOKED
            self._display = False
            self._reason = observed.reason
            lease, self._lease = self._lease, None
        self._close_lease(lease)
        return DisplayCheckResult(False, observed.reason, True)

    def close(self) -> None:
        lease = None
        with self._lock:
            if self._stage == CapabilityStage.CLOSED:
                return
            self._stage = CapabilityStage.CLOSED
            self._display = False
            lease, self._lease = self._lease, None
        self._close_lease(lease)

    @staticmethod
    def _close_lease(lease: Optional[Any]) -> None:
        if lease is None:
            return
        try:
            lease.close()
        except Exception:  # aqg: top-level boundary -- cleanup cannot reopen capability
            # aqg: top-level boundary -- cleanup failure cannot reopen capability.
            pass
