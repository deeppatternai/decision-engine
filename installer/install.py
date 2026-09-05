"""Unified installer for the Decision Engine public shell.

    install de   [--bundle-root DIR] [--server-endpoint URL] [--api-key KEY]
    install aqg  [--bundle-root DIR]
    install all  [--bundle-root DIR] [--server-endpoint URL] [--api-key KEY]
    install --repair-client CLIENT

What it does (release design §9):

- Lays the *real bodies* down side-by-side under ``~/.deeppattern/`` —
  ``decision-engine/`` and ``aqg/`` — copied from an unpacked shell bundle.
- Routes each component's skills into registered agent skill directories via
  symlink (directory Junction on Windows).
- **DE bundles AQG**: ``install de`` installs both decision-engine and aqg.
- Writes ``~/.deeppattern/decision-engine/config.json`` (API key + device
  fingerprint + server endpoint). AQG is local + no-auth, so ``install aqg``
  writes no config and does no device binding.
- Reserves an ``eaf`` slot for the future engine (installed only if its body is
  present in the bundle).

Idempotent: re-running replaces routed symlinks and refreshes the body without
duplicating or erroring. Device *activation* (exchanging the API key for a
real device token) is a separate online step — see ``shim.py`` / client docs;
this installer only stages the config so that step has a home to write to.

No server IP or secret is baked in: ``--server-endpoint`` is owner-provided and
lands only in the local config file.
"""

from __future__ import annotations

import argparse
import contextlib
import ntpath
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional

from . import client_host_ownership, cursor_activation, mcp_config
from .config import (
    ShellError,
    claude_skills_dir,
    codex_skills_dir,
    codex_skills_in_use,
    component_root,
    de_config_path,
    deeppattern_home,
    device_fingerprint,
    device_name_default,
    atomic_write_json,
    load_json,
    managed_component_root,
    normalize_endpoint,
)
from client.version import CLIENT_VERSION  # SOT leaf module (no runtime pull); the version the device reports

try:
    import fcntl  # POSIX only; Windows uses msvcrt inside install_lock.
except ImportError:  # pragma: no cover - exercised by Windows CI / mocked tests
    fcntl = None  # type: ignore


@contextlib.contextmanager
def install_lock(*, blocking: bool = True) -> Iterator[None]:
    """Serialize concurrent installer runs so two `install` invocations can't
    interleave a body rmtree with the other's config snapshot/write (which would
    lose the activation token — last-writer-wins)."""
    home = deeppattern_home()
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / ".install.lock"
    with open(lock_path, "a+b") as handle:
        if fcntl is None:
            try:
                import msvcrt
            except ImportError as exc:
                raise ShellError("this platform has no supported install lock") from exc
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as exc:
                raise ShellError("another Decision Engine install is active") from exc
            try:
                yield
            finally:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            return
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise ShellError("another Decision Engine install is active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

# Component -> body subdirectory expected inside the bundle root:
#   <bundle>/decision-engine/{skills/,...}
#   <bundle>/eaf/{skills/,...}          (optional, future)
#
# AQG is deliberately NOT a component here. install.sh owns it end to end: it clones the AQG repo
# to ~/.deeppattern/agent-quality-gates and runs AQG's OWN scripts/install.sh, which lays down the
# body and routes its skills. This module used to also list "aqg", which looked for a body at
# ~/.deeppattern/aqg — a directory install.sh never creates — so every `de` install skipped it
# ("not in bundle") and it laid down nothing, ever. Two owners for one component is what let that
# sit unnoticed; there is now one.
KNOWN_COMPONENTS = ("decision-engine", "eaf")

# Which real bodies each install target lays down. "all" additionally reserves eaf (installed only
# if its body is present in the bundle — it has none yet, so it no-ops by design; that is NOT the
# case the REQUIRED check below guards).
TARGETS: Dict[str, List[str]] = {
    "de": ["decision-engine"],
    "all": ["decision-engine", "eaf"],
}

# A target that cannot lay these down is a broken install, not a no-op: without the body there is
# no skill to route and no config to write, and run_install would otherwise return a clean summary
# having installed nothing. install.sh checks for skills/ before calling, but this module is a
# public entry point (`python3 -m installer.install`) and must not fail open on its own.
REQUIRED_COMPONENTS = {"decision-engine"}

# Only decision-engine is server-backed (needs endpoint + device binding).
SERVER_BACKED = {"decision-engine"}


class SkillRouteConflict(ShellError):
    """A user-managed route occupies a skill name owned by this bundle."""


def _replace_tree(src: Path, dst: Path) -> None:
    """Copy ``src`` body to ``dst`` idempotently (replace any prior body)."""
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


# --- Skill routing links -----------------------------------------------------
# A skill is "routed" by linking its directory from the managed checkout into the
# agent's skills dir, so a managed update to the checkout is reflected instantly
# (the link keeps pointing at the live source — no re-copy needed). On POSIX that
# link is a symlink. On Windows, creating a symlink requires Developer Mode or
# Administrator (the CreateSymbolicLink privilege), which fails for a normal user
# with `OSError: [WinError 1314]` and is hostile to demand of every user — so on
# Windows we use a **directory junction** (`mklink /J`) instead. A junction gives
# the same "points at the live checkout" behaviour with no elevated privilege.
# Junctions are directory-only and same-volume — both hold here (skills are
# directories; source and dest both live under the user profile).


def _path_module():
    """Return Windows path semantics when the Windows branch is under test."""
    return ntpath if os.name == "nt" else os.path


def _normalize_route_path(path: str) -> str:
    """Normalize a route target for ownership comparisons."""
    if os.name == "nt" and (path.startswith("\\\\?\\") or path.startswith("\\??\\")):
        path = path[4:]
    path_module = _path_module()
    return path_module.normcase(path_module.normpath(path_module.abspath(path)))


def _skill_route_target(path: Path) -> Optional[str]:
    """Return a normalized symlink/junction target, or ``None`` for other entries.

    On Windows, a generic reparse-point attribute is not enough: volume mounts,
    cloud placeholders, and provider-defined entries are reparse points too.
    ``os.readlink`` succeeds for directory junctions and symlinks and fails
    closed for unrelated reparse tags.
    """
    if os.name != "nt" and not path.is_symlink():
        return None
    try:
        target = os.readlink(path)
    except OSError:
        return None
    path_module = _path_module()
    if not path_module.isabs(target):
        target = path_module.join(str(path.parent), target)
    return _normalize_route_path(target)


def _is_skill_route(path: Path) -> bool:
    """True only for a readable symlink or Windows directory junction."""
    return _skill_route_target(path) is not None


def _route_points_to(path: Path, target: Path) -> bool:
    """Whether ``path`` points exactly at the expected installer-owned target."""
    actual = _skill_route_target(path)
    return actual is not None and actual == _normalize_route_path(str(target))


def _create_windows_junction(link: Path, target: Path) -> None:
    """Create a junction through CPython's native Win32 wrapper.

    ``_winapi.CreateJunction`` has shipped with supported CPython releases since
    before the current Python floor. It keeps paths out of both cmd.exe and PowerShell, so legal
    characters such as ``&`` and ``[]`` have no shell or wildcard semantics.
    """
    try:
        import _winapi
    except ImportError as exc:
        raise ShellError(
            "Windows skill routing requires CPython's native _winapi.CreateJunction"
        ) from exc
    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError as exc:
        raise ShellError(
            "failed to create skill junction %s -> %s: %s" % (link, target, exc)
        ) from exc


def _create_skill_route(link: Path, target: Path) -> None:
    """Create a route at ``link`` pointing at directory ``target`` — a symlink on
    POSIX, a directory junction on Windows (no Developer Mode / admin needed)."""
    if os.name == "nt":
        _create_windows_junction(link, target)
    else:
        link.symlink_to(target)


def _remove_skill_route(path: Path) -> None:
    """Remove a route without touching what it points at. On Windows both a
    directory symlink and a junction are unlinked as a directory entry with
    ``rmdir`` (which removes only the reparse point, never recursing into the
    target); on POSIX a symlink is ``unlink``-ed."""
    if os.name == "nt":
        os.rmdir(path)
    else:
        path.unlink()


def _preflight_skill_routes(
    component: str,
    skills_src: Path,
    installed_skills_src: Path,
    dest_root: Path,
    excluded_skills: FrozenSet[str] = frozenset(),
) -> None:
    """Refuse predictable destination conflicts before replacing the body.

    ``skills_src`` may be the incoming bundle while every final route points at
    ``installed_skills_src``.  Running this for all clients before
    ``_replace_tree`` keeps a Codex-only conflict from partially installing the
    body or changing Claude routes.  `_route_skills` repeats the ownership
    checks when it writes, closing the check/use window.
    """

    if not skills_src.is_dir() or not dest_root.exists():
        return
    for skill_dir in sorted(path for path in skills_src.iterdir() if path.is_dir()):
        if skill_dir.name in excluded_skills:
            continue
        target = dest_root / skill_dir.name
        expected_target = installed_skills_src / skill_dir.name
        if _is_skill_route(target):
            if not _route_points_to(target, expected_target):
                raise SkillRouteConflict(
                    "refusing to replace existing skill route %s because it points "
                    "outside this install" % target
                )
        elif target.exists():
            raise SkillRouteConflict(
                "refusing to overwrite existing non-symlink skill %s "
                "(remove it manually to let %s route it)" % (target, component)
            )


def _route_skills(
    component: str,
    body_root: Path,
    *,
    dest_root: Optional[Path] = None,
    excluded_skills: FrozenSet[str] = frozenset(),
) -> List[str]:
    """Route each skill dir under ``<body>/skills/`` into the agent skills dir
    (symlink on POSIX, directory junction on Windows — see the note above).

    Returns the list of routed skill names. Idempotent: a correct route is left
    untouched; a stale owned route is pruned. A pre-existing *real* directory
    or foreign route is left untouched and reported, never clobbered.
    """
    skills_src = body_root / "skills"
    if not skills_src.is_dir():
        return []
    dest_root = dest_root or claude_skills_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    routed: List[str] = []
    for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
        if skill_dir.name in excluded_skills:
            continue
        target = dest_root / skill_dir.name
        expected_target = skills_src / skill_dir.name
        if _is_skill_route(target):
            if not _route_points_to(target, expected_target):
                raise SkillRouteConflict(
                    "refusing to replace existing skill route %s because it points "
                    "outside this install" % target
                )
            # A correct route already follows managed updates automatically.
            # Leave it in place so every MCP startup can run the repair path
            # without churning symlinks/Junctions or racing another client.
            routed.append(skill_dir.name)
            continue
        elif target.exists():
            # A real, non-route entry we did not create — do not clobber.
            raise SkillRouteConflict(
                "refusing to overwrite existing non-symlink skill %s "
                "(remove it manually to let %s route it)" % (target, component)
            )
        _create_skill_route(target, expected_target)
        routed.append(skill_dir.name)
    _prune_stale_routes(dest_root, skills_src, set(routed))
    return routed


def repair_codex_skill_routes(body_root: Path) -> List[str]:
    """Idempotently add/repair Codex routes for an existing installation.

    Managed updates replace the checkout in place, so historical users do not
    rerun ``install_component`` when a release first adds Codex skill support.
    The launcher calls this narrow repair after candidate admission.  The
    cross-platform install lock serializes it with a user-initiated reinstall;
    route ownership checks remain fail-closed and never overwrite a
    real/foreign skill entry.
    """

    # MCP startup must never wait behind a user-initiated installer. A busy
    # lock becomes a best-effort refusal that launcher logs without blocking
    # the known-good shim; the next startup retries.
    if not codex_skills_in_use():
        return []
    with install_lock(blocking=False):
        return _route_skills(
            "decision-engine", Path(body_root), dest_root=codex_skills_dir()
        )


@dataclass(frozen=True)
class SkillRouteRepairResult:
    routed: Dict[str, List[str]]
    failed: Dict[str, str]


def retire_legacy_cursor_owned_copy(managed_root: Path) -> Optional[str]:
    """Retire the former Cursor owned-copy transaction before shared-link routing.

    The old uninstaller verifies the ownership record, MCP projection, cached
    payload, and every active skill before removing anything. Any missing or
    changed proof fails closed and leaves the user's Cursor tree untouched.
    """

    try:
        if client_host_ownership.record_file_sha256_if_present("cursor") is None:
            return None
        # Reuse the retained read-only legacy verifier before invoking the
        # transactional uninstaller. The import is intentionally lazy because
        # Doctor imports this shared router for normal link diagnosis.
        from installer import doctor

        if doctor._legacy_cursor_owned_skills_status() != "healthy":
            return "legacy owned-copy could not be verified and was preserved"
        cursor_activation.uninstall_cursor_owned(managed_root=Path(managed_root))
    except (ShellError, OSError, ValueError):
        return "legacy owned-copy could not be verified and was preserved"
    return None


def _skill_route_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, SkillRouteConflict):
        return "existing user-managed skill entry was preserved"
    if isinstance(exc, ShellError):
        return "skill routing failed"
    return "skill routing filesystem operation failed"


def repair_detected_skill_routes(
    body_root: Path,
    *,
    excluded_clients: FrozenSet[str] = frozenset(),
) -> SkillRouteRepairResult:
    """Repair registered skill routes during explicit setup only.

    The managed launcher intentionally continues to call only
    ``repair_codex_skill_routes`` for its historical migration. This generic
    repair is reserved for user-initiated permanent setup/install, so merely
    starting another host never onboards Cursor during an automatic update.
    """

    routes = mcp_config.active_skill_routes(setup_only=True)
    if not routes:
        return SkillRouteRepairResult({}, {})
    with install_lock(blocking=False):
        body_root = Path(body_root)
        routed: Dict[str, List[str]] = {}
        failed: Dict[str, str] = {}
        for client, (destination, excluded) in routes.items():
            if client in excluded_clients:
                continue
            try:
                _preflight_skill_routes(
                    "decision-engine",
                    body_root / "skills",
                    body_root / "skills",
                    destination,
                    excluded,
                )
                routed[client] = _route_skills(
                    "decision-engine",
                    body_root,
                    dest_root=destination,
                    excluded_skills=excluded,
                )
            except (ShellError, OSError) as exc:
                failed[client] = _skill_route_failure_reason(exc)
        return SkillRouteRepairResult(routed, failed)


def repair_client_integration(client: str, body_root: Path) -> Dict[str, object]:
    """Repair one host through the common MCP + managed-link adapters."""
    spec = mcp_config.CLIENT_SPECS.get(client)
    if spec is None:
        raise ShellError("unknown client %r" % client)
    body_root = Path(body_root)
    if client == "cursor":
        migration_failure = retire_legacy_cursor_owned_copy(body_root)
        if migration_failure is not None:
            raise ShellError(migration_failure)
    if spec.skill_delivery_mode == "none":
        mcp_result = mcp_config.write_entry(client, cwd=body_root)
        return {"client": client, "mcp": mcp_result, "routed_skills": []}
    if spec.skills_global_path is None:
        raise ShellError("client skill destination is unavailable")
    destination = spec.skills_global_path()
    with install_lock(blocking=False):
        # Refuse predictable user-owned skill conflicts before publishing the
        # MCP entry for this explicit single-host repair.
        _preflight_skill_routes(
            "decision-engine",
            body_root / "skills",
            body_root / "skills",
            destination,
            spec.excluded_skills,
        )
        mcp_result = mcp_config.write_entry(client, cwd=body_root)
        try:
            routed = _route_skills(
                "decision-engine",
                body_root,
                dest_root=destination,
                excluded_skills=spec.excluded_skills,
            )
        except (ShellError, OSError) as exc:
            return {
                "client": client,
                "mcp": mcp_result,
                "routed_skills": [],
                "failure": _skill_route_failure_reason(exc),
            }
    return {"client": client, "mcp": mcp_result, "routed_skills": routed}


def _prune_stale_routes(dest_root: Path, skills_src: Path, current: set) -> None:
    """Remove our own symlinks for skills that were dropped from the bundle.

    A prior install may have routed skills that no longer exist in this
    component's body; those symlinks would dangle (or point at a stale path).
    We only touch symlinks that point *into this component's* skills dir, so a
    user's own links and other components' routes are never disturbed.
    """
    path_module = _path_module()
    src_root = _normalize_route_path(str(skills_src))
    for entry in dest_root.iterdir():
        if entry.name in current:
            continue
        target = _skill_route_target(entry)
        if target is None:
            continue
        try:
            owned_root = path_module.commonpath([target, src_root])
        except ValueError:
            continue
        if owned_root == src_root and target != src_root:
            _remove_skill_route(entry)


def install_component(component: str, bundle_root: Path) -> Dict[str, object]:
    """Install one component body + route its skills. Returns a summary dict.

    IN-PLACE (parallel to AQG): when the source IS the install dir — i.e. the repo was cloned
    straight to ``~/.deeppattern/<component>`` — DO NOT copy. ``_replace_tree`` would rmtree the
    dst first, and here dst == src, so it would delete the live clone (``.git`` + ``installer/``
    and all) before copying nothing back. Instead just route its skills in place; ``git pull``
    then updates the body directly, exactly like AQG's checkout."""
    src = bundle_root / component
    if not src.is_dir():
        return {"component": component, "installed": False, "reason": "not in bundle"}
    dst = component_root(component)
    in_place = src.resolve() == dst.resolve()
    route_destinations = mcp_config.active_skill_routes()
    route_failures: Dict[str, str] = {}
    route_candidates = {}
    if "cursor" in route_destinations:
        migration_failure = retire_legacy_cursor_owned_copy(dst)
        if migration_failure is not None:
            route_failures["cursor"] = migration_failure
    for client, (destination, excluded) in route_destinations.items():
        if client in route_failures:
            continue
        try:
            _preflight_skill_routes(
                component,
                src / "skills",
                dst / "skills",
                destination,
                excluded,
            )
        except (ShellError, OSError) as exc:
            route_failures[client] = _skill_route_failure_reason(exc)
        else:
            route_candidates[client] = (destination, excluded)
    if not in_place:
        # Defense-in-depth backstop (audit 50070df6 convergent grok/gemini): `_replace_tree` rmtrees
        # `dst`. A legitimate install target is a plain body — it must NOT contain `.git`. If it does,
        # `dst` is a live git clone (someone's checkout, possibly with uncommitted work), so REFUSE
        # rather than delete it — independent of whatever branch install.sh chose.
        if (dst / ".git").exists():
            raise ShellError(
                "refusing to overwrite the git working tree at %s (it has a .git dir) — clone the "
                "client straight to it and install in place, or remove it first" % dst
            )
        _replace_tree(src, dst)
    routed_by_client: Dict[str, List[str]] = {}
    for client, (destination, excluded) in route_candidates.items():
        try:
            routed_by_client[client] = _route_skills(
                component,
                dst,
                dest_root=destination,
                excluded_skills=excluded,
            )
        except (ShellError, OSError) as exc:
            # Close the preflight/write race without rolling back unrelated hosts.
            route_failures[client] = _skill_route_failure_reason(exc)
    routed_skills = sorted(
        {
            skill
            for client_skills in routed_by_client.values()
            for skill in client_skills
        }
    )
    return {
        "component": component,
        "installed": True,
        "body": str(dst),
        "in_place": in_place,
        # Preserve the original summary field as the unique skill list while
        # exposing per-client routes for diagnostics and future clients.
        "routed_skills": routed_skills,
        "routed_skill_clients": routed_by_client,
        "routing_failures": route_failures,
        "integration_ready": bool(routed_by_client) or not route_destinations,
    }


def write_de_config(
    *,
    server_endpoint: Optional[str],
    api_key: Optional[str],
    device_name: Optional[str],
    prior: Optional[Dict[str, object]] = None,
) -> Path:
    """Stage the Decision Engine device config (§9).

    Merges onto ``prior`` (the config snapshotted before the body was replaced),
    so re-running the installer after activation does not wipe device_id /
    access_token — even though ``config.json`` lives *inside* the
    ``~/.deeppattern/decision-engine/`` body that a reinstall rewrites. Never
    writes a hardcoded IP or secret — endpoint is owner-provided, token is
    filled later by activation.
    """
    path = de_config_path()
    config: Dict[str, object] = dict(prior) if prior else load_json(path)
    if server_endpoint:
        config["server_endpoint"] = normalize_endpoint(server_endpoint)
    if api_key is not None:
        config["api_key"] = api_key
    config.setdefault("device_id", "")
    config.setdefault("access_token", "")
    config["device_name"] = device_name or config.get("device_name") or device_name_default()
    config["device_fingerprint"] = device_fingerprint()
    # Informational record of the client that last configured this device (force-set, not setdefault, so
    # a reinstall re-stamps it). NOTE: this config field does NOT drive the version reported at
    # activation — activate.py reports the running CLIENT_VERSION directly (audit 988bd8e1: the version is
    # a code property, never a user-editable config value), so a stale config can't leave the server's
    # version-gated GE byte-isolation dormant.
    config["client_version"] = CLIENT_VERSION
    atomic_write_json(path, config)
    return path


def run_install(
    target: str,
    *,
    bundle_root: Path,
    server_endpoint: Optional[str],
    api_key: Optional[str],
    device_name: Optional[str],
) -> Dict[str, object]:
    if target not in TARGETS:
        raise ShellError("unknown target %r (choose de|aqg|all)" % target)
    if not bundle_root.is_dir():
        raise ShellError("bundle root not found: %s" % bundle_root)

    with install_lock():
        # Snapshot the DE config BEFORE any body is replaced — config.json lives
        # inside the decision-engine body dir a reinstall rewrites (§9 path), so
        # capture the activation credential first, then merge it back.
        prior_config = load_json(de_config_path())

        results: List[Dict[str, object]] = []
        summary: Dict[str, object] = {"target": target, "components": results}
        for component in TARGETS[target]:
            result = install_component(component, bundle_root)
            results.append(result)
            if component in REQUIRED_COMPONENTS and not result.get("installed"):
                raise ShellError(
                    "%s not found in the bundle at %s (%s) — nothing was installed. Run install.sh "
                    "from a full clone of the repo, not a copy of the script alone."
                    % (component, bundle_root, result.get("reason") or "missing"))
            # Re-persist the DE config the instant its body is (re)laid down —
            # BEFORE any later component installs — so a failure while installing
            # eaf cannot strand the token that only lived in memory.
            if result.get("installed") and component in SERVER_BACKED:
                config_path = write_de_config(
                    server_endpoint=server_endpoint,
                    api_key=api_key,
                    device_name=device_name,
                    prior=prior_config,
                )
                summary["de_config"] = str(config_path)
            if (
                component in REQUIRED_COMPONENTS
                and result.get("integration_ready") is False
            ):
                raise ShellError(
                    "Decision Engine body and config were installed, but no detected "
                    "Agent skill route could be wired; existing user-managed entries "
                    "were preserved"
                )
    return summary


def _print_summary(summary: Dict[str, object]) -> None:
    print("Decision Engine shell install (target=%s)" % summary["target"])
    for comp in summary["components"]:  # type: ignore[index]
        if comp.get("installed"):
            clients = comp.get("routed_skill_clients") or {}
            skills = {
                skill
                for routed in clients.values()
                for skill in routed
            } or set(comp.get("routed_skills") or [])
            print(
                "  ✓ %s → %s (%d skills routed to %s)"
                % (
                    comp["component"],
                    comp["body"],
                    len(skills),
                    ", ".join(sorted(clients)) or "agents",
                )
            )
            for client, reason in sorted(
                (comp.get("routing_failures") or {}).items()
            ):
                print(
                    "  ! %s skill routing failed: %s; other agents remain installed"
                    % (client, reason)
                )
        else:
            print("  · %s skipped (%s)" % (comp["component"], comp.get("reason")))
    if summary.get("de_config"):
        print("  config: %s" % summary["de_config"])
        print("  next: ask the agent to open permanent setup: python3 -m installer.permanent_setup")
        print("        (see client docs — README.md 'Activate' / 'Use')")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install",
        description="Unified Decision Engine / AQG shell installer",
    )
    parser.add_argument(
        "target",
        nargs="?",
        choices=sorted(TARGETS),
        help="de | aqg | all",
    )
    parser.add_argument(
        "--bundle-root",
        help="unpacked shell bundle directory (contains decision-engine/, aqg/, ...)",
    )
    parser.add_argument(
        "--repair-client",
        choices=mcp_config.CLIENTS,
        help=(
            "repair one client through the shared MCP and managed-skill-link adapters"
        ),
    )
    parser.add_argument("--server-endpoint", default="",
                        help="owner-issued server endpoint URL (DE only)")
    parser.add_argument("--api-key", default="",
                        help="owner-issued API key (DE only; consumed at activation)")
    parser.add_argument("--device-name", default="",
                        help="friendly device name (defaults to user-host)")
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repair_client is not None:
        if (
            args.target is not None
            or args.bundle_root is not None
            or args.server_endpoint
            or args.api_key
            or args.device_name
        ):
            parser.error("--repair-client cannot be combined with install arguments")
        try:
            result = repair_client_integration(
                args.repair_client,
                managed_component_root("decision-engine"),
            )
        except (ShellError, OSError, ValueError) as exc:
            print("install: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            import json

            print(json.dumps(result, sort_keys=True))
        else:
            print(
                "install: %s MCP %s; %d managed skill links ready"
                % (
                    result["client"],
                    result["mcp"]["action"],
                    len(result["routed_skills"]),
                )
            )
            if result.get("failure"):
                print(
                    "install: %s requires skill-link repair: %s"
                    % (result["client"], result["failure"]),
                    file=sys.stderr,
                )
        return 1 if result.get("failure") else 0
    if args.target is None or args.bundle_root is None:
        parser.error("target and --bundle-root are required for bundle installation")
    try:
        summary = run_install(
            args.target,
            bundle_root=Path(args.bundle_root).expanduser(),
            server_endpoint=args.server_endpoint or None,
            api_key=args.api_key if args.api_key != "" else None,
            device_name=args.device_name or None,
        )
    except ShellError as exc:
        print("install: %s" % exc, file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
