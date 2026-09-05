"""One-time activation for an already-prepared signed managed checkout.

This is intentionally not a downloader that overwrites an arbitrary legacy
directory.  A bootstrap must first create the fixed root at the signed stable
tag.  Activation then proves that exact state, writes protected identity/state,
publishes the launcher protocol marker, but never rewires an Agent client.
``installer.permanent_setup`` exclusively owns host wiring after permanent
device activation succeeds.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import (
    config,
    managed_install,
    mcp_config,
    release_acquisition,
    release_contract,
    update_coordination,
    update_transaction,
    updater,
)
from .config import ShellError


class ManagedActivationError(ShellError):
    """A safe-to-display activation refusal."""


@dataclass(frozen=True)
class ActivationResult:
    root: Path
    version: str
    commit: str
    source: str
    clients: Tuple[str, ...]
    failed_clients: Tuple[Tuple[str, str], ...] = ()


def _require_prepared_remotes(root: Path) -> None:
    reader = updater._GitReader(root)
    remotes = updater._read_remotes(reader)
    try:
        managed_install._validate_official_remotes(remotes)
    except managed_install.ManagedInstallError as exc:
        raise ManagedActivationError(
            "prepared checkout must contain only the exact GitHub and Gitee remotes"
        ) from exc


def _verify_prepared_checkout(
    root: Path,
    manifest: release_contract.ReleaseManifest,
    signature: release_contract.ReleaseSignature,
    trusted_keys,
) -> release_contract.VerifiedRelease:
    """Re-prove signed tag, HEAD and VERSION on every activation attempt."""

    verified = release_contract.verify_release_signature(
        manifest,
        signature,
        trusted_keys,
        expected_repository_id=managed_install.REPOSITORY_ID,
        expected_channel=managed_install.CHANNEL,
    )
    if not release_contract.python_is_compatible(
        verified.manifest, tuple(sys.version_info[:3])
    ):
        raise ManagedActivationError("prepared release requires a newer Python runtime")
    reader = updater._GitReader(root)
    _code, tag_output = reader.run("target", tag=verified.manifest.tag)
    if updater._single_commit(tag_output, "prepared release tag") != verified.manifest.commit:
        raise ManagedActivationError("prepared tag does not match the signed release")
    _code, head_output = reader.run("head")
    if updater._single_commit(head_output, "prepared HEAD") != verified.manifest.commit:
        raise ManagedActivationError("prepared HEAD does not match the signed release")
    try:
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ManagedActivationError("prepared VERSION is unreadable") from exc
    if version != verified.manifest.version:
        raise ManagedActivationError("prepared VERSION does not match the signed release")
    return verified


def activate_prepared_install(
    root: Optional[Path] = None,
    *,
    clients: Optional[Sequence[str]] = None,
    startup_budget_seconds: float = 10.0,
) -> ActivationResult:
    """Activate launcher updates without directing any Agent client to the root."""

    canonical = managed_install.canonical_managed_root(
        Path(root or config.managed_component_root("decision-engine"))
    )
    managed_install._require_fixed_managed_root(canonical)
    targets = tuple(clients if clients is not None else mcp_config.detect_clients())
    if not targets or any(client not in mcp_config.CLIENTS for client in targets):
        raise ManagedActivationError(
            "at least one supported Agent client must be selected before activation"
        )
    if len(set(targets)) != len(targets):
        raise ManagedActivationError("activation client list contains duplicates")
    protocol = canonical / update_transaction.PROTOCOL_READY_RELATIVE_PATH
    if protocol.exists() or update_coordination._is_link_like(protocol):
        raise ManagedActivationError(
            "managed updates are already activated; use the MCP config command to change clients"
        )
    # Reject a locally misconfigured checkout before crossing the network boundary.
    _require_prepared_remotes(canonical)
    trusted_keys = release_acquisition.load_trusted_release_keys(canonical)
    if not trusted_keys:
        raise ManagedActivationError(
            "production release public keys are not provisioned; activation remains disabled"
        )
    deadline = time.monotonic() + max(0.0, float(startup_budget_seconds))
    try:
        acquired = release_acquisition.discover_initial_release(
            trusted_keys, deadline=deadline
        )
    except release_acquisition.ReleaseTransportError:
        # Same scoped exception as bootstrap_managed_install._bootstrap_fresh_clone:
        # fall back to this already-cloned checkout's own authenticated git
        # remotes when the anonymous HTTPS mirrors are unreachable (private
        # repository). Uses discover_initial_release_via_git (no anti-rollback
        # prior — correct here, since activation runs before any protected
        # state exists) — the separate, ongoing per-MCP-start launcher path
        # uses discover_release_via_git instead (with anti-rollback checked
        # against protected state); see that function's docstring for the
        # private-beta-only rationale shared by both.
        print(
            "de-managed-activate: anonymous HTTPS mirrors unreachable — falling "
            "back to this checkout's own authenticated git access",
            file=sys.stderr,
        )
        remotes = updater._read_remotes(updater._GitReader(canonical))
        remote_names = tuple(
            name for name, _url in managed_install.OFFICIAL_REMOTE_URLS if name in remotes
        )
        acquired = release_acquisition.discover_initial_release_via_git(
            canonical, remote_names, trusted_keys
        )
    with update_coordination.install_transaction(
        canonical, timeout_seconds=0.0
    ) as transaction:
        _require_prepared_remotes(canonical)
        verified = _verify_prepared_checkout(
            canonical, acquired.manifest, acquired.signature, trusted_keys
        )
        live = update_coordination.live_shim_sessions(
            canonical, transaction=transaction
        )
        if live:
            raise ManagedActivationError(
                "running legacy shim sessions must exit before activation"
            )
        managed_install.write_managed_identity(canonical)
        try:
            state = updater._read_update_state(canonical)
        except updater.UpdateInspectionError:
            state = update_transaction.initialize_release_state(
                canonical,
                acquired.manifest,
                acquired.signature,
                trusted_keys,
                source=acquired.source.name,
                transaction=transaction,
            )
        else:
            if (
                state.last_release_sequence != acquired.manifest.release_sequence
                or state.last_release_commit != acquired.manifest.commit
                or state.last_manifest_sha256
                != release_contract.manifest_sha256(acquired.manifest)
                or state.last_version != acquired.manifest.version
            ):
                raise ManagedActivationError(
                    "existing protected state does not match the signed stable release"
                )
        # Publish only checkout-local readiness. External Agent configuration
        # remains untouched until permanent device activation succeeds.
        update_transaction._write_protocol_ready_locked(canonical, transaction)
    return ActivationResult(
        canonical,
        state.last_version,
        state.last_release_commit,
        acquired.source.name,
        targets,
        (),
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="de-managed-activate",
        description="Activate a prepared signed stable Decision Engine checkout",
    )
    parser.add_argument("--managed-root", type=Path)
    parser.add_argument("--client", action="append", choices=mcp_config.CLIENTS)
    parser.add_argument("--startup-budget-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        result = activate_prepared_install(
            args.managed_root,
            clients=args.client,
            startup_budget_seconds=args.startup_budget_s,
        )
    except (ShellError, OSError, ValueError) as exc:
        print("de-managed-activate: %s" % exc, file=sys.stderr)
        return 1
    print(
        "de-managed-activate: core ready %s@%s via %s; MCP wiring is deferred "
        "until permanent setup for %s"
        % (
            result.version,
            result.commit[:12],
            result.source,
            ", ".join(result.clients),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
