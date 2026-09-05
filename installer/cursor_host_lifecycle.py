"""Cursor binding for the host-neutral lifecycle dispatcher."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from installer import (
    client_host_lifecycle,
    client_host_ownership,
    config,
    cursor_activation,
    mcp_config,
    release_contract,
)


def collect_owned_release_evidence(
    managed_root: Path,
) -> client_host_lifecycle.ReleaseEvidence | None:
    managed = client_host_lifecycle.collect_managed_release_evidence(managed_root)
    if managed is None:
        return None
    record = client_host_ownership.read_record_if_present(
        "cursor",
        managed_root=managed_root,
        config_path=mcp_config.agent_config_path("cursor"),
        server_name=mcp_config.DEFAULT_SERVER_NAME,
    )
    if record is None:
        return None
    return client_host_lifecycle.bind_owned_release(
        managed,
        record.skill_release_id,
    )


def _execute(
    operation: str,
    request: Mapping[str, Any],
) -> client_host_lifecycle.LifecycleResult:
    outcome: Mapping[str, Any] | None = None
    try:
        managed_root = request.get("managed_root")
        if not isinstance(managed_root, Path):
            raise ValueError("managed_root is required")
        if operation == "update":
            verified = request.get("verified_release")
            if verified is None:
                verified = cursor_activation._verified_current_release(
                    managed_root
                )
            if not isinstance(verified, release_contract.VerifiedRelease):
                raise ValueError("verified_release is invalid")
            outcome = cursor_activation.update_cursor_owned(
                managed_root=managed_root,
                verified_release=verified,
            )
        elif operation == "uninstall":
            outcome = cursor_activation.uninstall_cursor_owned(
                managed_root=managed_root
            )
        elif operation == "repair":
            outcome = cursor_activation.repair_cursor_activation(
                managed_root=managed_root,
                python=request.get("python"),
            )
        elif operation == "status":
            evidence = collect_owned_release_evidence(managed_root)
            state = (
                "unknown"
                if evidence is None
                else client_host_lifecycle.assess_release_evidence(evidence).state
            )
            return client_host_lifecycle.LifecycleResult(
                host_family="cursor",
                operation=operation,
                status="completed",
                details=MappingProxyType({"release_state": state}),
            )
        else:
            return client_host_lifecycle.LifecycleResult(
                host_family="cursor",
                operation=operation,
                status="unsupported_for_host",
            )
    except cursor_activation.CursorActivationError as exc:
        message = str(exc)
        prefix = message.partition(":")[0]
        status = (
            "busy"
            if "already in progress" in message
            else prefix
            if prefix
            in {
                "repair_required",
                "requires_newer_installer",
            }
            else "refused"
        )
        return client_host_lifecycle.LifecycleResult(
            host_family="cursor",
            operation=operation,
            status=status,
            reason=prefix or type(exc).__name__,
        )
    except client_host_ownership.OwnershipError:
        return client_host_lifecycle.LifecycleResult(
            host_family="cursor",
            operation=operation,
            status="repair_required",
            reason="ownership_invalid",
        )
    except config.ShellError:
        return client_host_lifecycle.LifecycleResult(
            host_family="cursor",
            operation=operation,
            status="repair_required",
            reason="lifecycle_state_invalid",
        )
    except (OSError, ValueError):
        return client_host_lifecycle.LifecycleResult(
            host_family="cursor",
            operation=operation,
            status="refused",
            reason="invalid_request",
        )
    return client_host_lifecycle.LifecycleResult(
        host_family="cursor",
        operation=operation,
        status="completed",
        details=MappingProxyType(
            {
                "cleanup_pending": True,
            }
            if outcome is not None and outcome.get("cleanup_pending") is True
            else {}
        ),
    )


def build_adapter() -> client_host_lifecycle.HostLifecycleAdapter:
    return client_host_lifecycle.HostLifecycleAdapter(
        adapter_id="cursor-owned-v1",
        host_family="cursor",
        supported_operations=frozenset(
            {"repair", "status", "update", "uninstall"}
        ),
        execute=_execute,
    )
