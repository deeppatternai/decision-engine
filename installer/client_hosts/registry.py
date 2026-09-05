"""Static in-repository client-host registry."""

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional

from installer.client_hosts.contract import AgentHostSpec, validate_host_specs
from installer.client_hosts.hosts.claude import HOST_SPECS as CLAUDE_HOST_SPECS
from installer.client_hosts.hosts.codex import HOST_SPECS as CODEX_HOST_SPECS
from installer.client_hosts.hosts.cursor import HOST_SPECS as CURSOR_HOST_SPECS
from installer.client_hosts.hosts.qoder import HOST_SPECS as QODER_HOST_SPECS
from installer.client_hosts.hosts.qoder_cn import HOST_SPECS as QODER_CN_HOST_SPECS
from installer.client_hosts.hosts.trae import HOST_SPECS as TRAE_HOST_SPECS
from installer.client_hosts.hosts.trae_cn import HOST_SPECS as TRAE_CN_HOST_SPECS
from installer.client_hosts.hosts.trae_work import HOST_SPECS as TRAE_WORK_HOST_SPECS
from installer.client_hosts.hosts.trae_work_cn import (
    HOST_SPECS as TRAE_WORK_CN_HOST_SPECS,
)
from installer.client_hosts.hosts.workbuddy import HOST_SPECS as WORKBUDDY_HOST_SPECS
from installer.config import ShellError


def _build_registry(groups: Iterable[Iterable[AgentHostSpec]]) -> Dict[str, AgentHostSpec]:
    registry: Dict[str, AgentHostSpec] = {}
    for group in groups:
        for spec in group:
            if not spec.id or spec.id in registry:
                raise ShellError("client-host registry contains a missing or duplicate id")
            registry[spec.id] = spec
    validate_host_specs(registry)
    return registry


CLIENT_SPECS: Mapping[str, AgentHostSpec] = MappingProxyType(
    _build_registry(
        (
            CLAUDE_HOST_SPECS,
            CODEX_HOST_SPECS,
            CURSOR_HOST_SPECS,
            QODER_HOST_SPECS,
            QODER_CN_HOST_SPECS,
            TRAE_HOST_SPECS,
            TRAE_WORK_HOST_SPECS,
            TRAE_CN_HOST_SPECS,
            TRAE_WORK_CN_HOST_SPECS,
            WORKBUDDY_HOST_SPECS,
        )
    )
)
CLIENTS = tuple(CLIENT_SPECS)


def interactive_setup_blocked_reason() -> Optional[str]:
    """Return the first host-owned reason the setup GUI must not open here."""

    for spec in CLIENT_SPECS.values():
        probe = spec.interactive_setup_guard_probe
        if probe is None:
            continue
        try:
            reason = probe()
        except Exception:  # aqg: top-level boundary — optional host detection fails open
            continue
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def webview_render_blocked_reason() -> Optional[str]:
    """Return the first host-owned reason the setup GUI must avoid WebView here.

    Distinct from ``interactive_setup_blocked_reason``: that one refuses ANY
    window (sessions where even Tk aborts the process); this one only says the
    WebView backend renders unreliably here while the plain-GDI Tk form still
    works, so callers should degrade to Tk instead of refusing the GUI.
    """

    for spec in CLIENT_SPECS.values():
        probe = spec.webview_render_guard_probe
        if probe is None:
            continue
        try:
            reason = probe()
        except Exception:  # aqg: top-level boundary — optional host detection fails open
            continue
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None
