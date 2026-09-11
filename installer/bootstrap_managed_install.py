"""Bootstrap a fresh managed Decision Engine checkout, or repair its wiring.

Phase 3 gap this module closes: nothing in the codebase ever produced a
checkout satisfying ``managed_activation.activate_prepared_install``'s
preconditions (canonical root, exactly the two official named remotes,
HEAD proven at a signed ``stable`` release). That function is intentionally
"not a downloader" (see its own module docstring) — this module is the
downloader it was waiting for.

Shape mirrors the already-shipped, already-tested private-beta installer at
``user-script/common/de_private_repo.py`` (clone -> discover signed release
-> fetch+verify the exact tag -> detached checkout -> atomic rename ->
quarantine on failure), adapted from one ``origin`` remote to the two named
official remotes, and handing off to this repo's own (already-tested)
``installer.install`` body/config step and ``installer.managed_activation``
identity/state/MCP-wiring step instead of reinventing either.

This module never touches an existing checkout that is not *exactly* the
canonical managed root in the exact official shape — a directory that exists
but is not a valid managed checkout (wrong/missing remotes, no ``.git`` at
all, a raw developer clone) is refused, not migrated. Migrating pre-existing
installs into this shape is Phase 4's job (``installer/migrate_managed_install.py``,
not yet written); this module only ever creates a *fresh* managed root or
resumes its own previously-interrupted bootstrap of that same fresh root.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence, Tuple

from . import (
    config,
    gui_setup,
    install as install_module,
    managed_activation,
    managed_install,
    mcp_config,
    permanent_setup,
    release_acquisition,
    release_contract,
    update_transaction,
    updater,
)
from .config import ShellError


class BootstrapError(ShellError):
    """A safe-to-display bootstrap refusal."""


_OWNER_ENV_KEYS = ("DE_ENDPOINT", "DE_ACTIVATION_SECRET")


_CLONE_TIMEOUT_SECONDS = 600.0
_FETCH_TIMEOUT_SECONDS = 600.0
_PERMANENT_SETUP_TIMEOUT_SECONDS = 600.0
_TRUST_DISCOVERY_BUDGET_SECONDS = 60.0
_TAG_RE = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


@dataclass(frozen=True)
class BootstrapResult:
    root: Path
    version: str
    commit: str
    source: str
    clients: Tuple[str, ...]
    reused_existing: bool
    failed_clients: Tuple[Tuple[str, str], ...] = ()


def _quarantine(path: Path) -> Optional[Path]:
    """Rename a failed staging/candidate dir aside; never delete (mirrors
    ``de_private_repo.py::_quarantine`` so a failed bootstrap stays inspectable)."""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    candidate = path.with_name("%s.failed-%s" % (path.name, stamp))
    counter = 1
    while candidate.exists():
        candidate = path.with_name("%s.failed-%s-%d" % (path.name, stamp, counter))
        counter += 1
    path.rename(candidate)
    return candidate


def _git(
    executable: str, environment, root: Optional[Path], *args: str,
    timeout: float, extra_config: Sequence[str] = (),
) -> bytes:
    config_flags = []
    for item in extra_config:
        config_flags.extend(("-c", item))
    argv = (
        executable, *config_flags,
        *(("-C", str(root)) if root is not None else ()), *args,
    )
    code, output = updater._run_bounded_git(argv, environment, timeout_seconds=timeout)
    if code != 0:
        raise BootstrapError("git %s failed" % " ".join(args))
    return output


def _windows_checkout_policy_required() -> bool:
    return os.name == "nt"


def _pin_windows_checkout_policy(repo: Path) -> None:
    """Pin the managed checkout's *local* ``core.autocrlf`` and ``core.symlinks``
    on Windows so the launcher's Windows-only policy gate
    (``updater._validate_local_checkout_policy``) is satisfied.

    Git-for-Windows sets ``core.symlinks=false`` locally at clone time but leaves
    ``core.autocrlf`` only in *global* config, so ``git config --local --get
    core.autocrlf`` reads ``unset`` and every managed operation exits 1 with
    "managed Windows checkout must pin local core.autocrlf and core.symlinks".
    Nothing else pinned it — this closes that gap. Both are pinned to ``false``:
    a signed checkout must match the manifest byte-for-byte, so no EOL conversion
    (``autocrlf=false``) and no symlink materialization (``symlinks=false``,
    matching git-for-windows' own default). No-op off Windows, where the gate
    does not apply and where forcing ``symlinks=false`` could corrupt legitimate
    symlink tree entries. Idempotent — re-running the installer re-asserts the
    same values, which self-heals an older checkout cloned before this pin."""
    if not _windows_checkout_policy_required():
        return
    executable = updater._resolve_git_executable(
        minimum_version=updater._MINIMUM_GIT_VERSION
    )
    environment = updater._ambient_git_environment(executable)
    _git(executable, environment, repo, "config", "--local", "core.autocrlf", "false", timeout=30.0)
    _git(executable, environment, repo, "config", "--local", "core.symlinks", "false", timeout=30.0)


def _test_only_extra_git_config() -> Sequence[str]:
    """Internal seam for local ``-c url.<path>.insteadOf=<official-url>``
    redirects so tests can point the clone/fetch calls below at a local
    fixture while the *recorded* remote URL stays byte-identical to the real
    official one. Always ``()`` here — there is no parameter on any public
    function a caller could set to a non-empty value; the only way this ever
    returns anything else is ``unittest.mock.patch.object`` replacing this
    function itself, which only test code does. (Earlier this was a public
    ``extra_git_config`` keyword threaded through ``bootstrap_new_install``;
    a deep audit correctly flagged that as a standing supply-chain surface —
    an unrestricted ``-c`` key set as a public parameter's "no caller passes
    it today" is not an enforced invariant against a future refactor — so it
    was removed from every public signature instead of merely documented as
    unused.)
    """
    return ()


def _clone_staging(stage: Path, remote_urls: Sequence[Tuple[str, str]]) -> str:
    """Clone from the first reachable official remote into ``stage``, then add
    the other remote by name so both end up configured exactly as
    ``managed_install.OFFICIAL_REMOTE_URLS`` requires. Returns the remote name
    actually used to clone.

    Uses ``updater._ambient_git_environment`` (not the hardened
    ``updater._git_environment``): this whole module is exclusively a
    one-time, explicitly human-triggered install/repair flow (never called
    from the unattended per-MCP-start launcher path), so resolving the
    caller's own already-authenticated HTTPS credential helper / SSH agent is
    the deliberate, in-scope exception documented on
    ``_ambient_git_environment`` itself — needed for a still-private
    repository, harmless once it is public (a public clone needs no
    credentials regardless of which environment is used). ``-c`` flags are
    per-invocation and are not suppressed by either environment function
    (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_NOSYSTEM`` only block *ambient*
    config files, not an explicit per-invocation flag) — see
    ``_test_only_extra_git_config`` for why that is still safe in
    production. ``--`` ends option parsing before the remote name/URL/
    stage-path operands so a value that happens to start with ``-`` can
    never be parsed as a git flag (e.g. ``--upload-pack=<cmd>``).
    """
    executable = updater._resolve_git_executable(
        minimum_version=updater._MINIMUM_GIT_VERSION
    )
    environment = updater._ambient_git_environment(executable)
    extra_git_config = _test_only_extra_git_config()
    last_error: Optional[Exception] = None
    for name, url in remote_urls:
        if stage.exists():
            _quarantine(stage)
        try:
            _git(
                executable, environment, None,
                "clone", "--no-checkout", "--origin", name, "--", url, str(stage),
                timeout=_CLONE_TIMEOUT_SECONDS, extra_config=extra_git_config,
            )
        except BootstrapError as exc:
            last_error = exc
            continue
        for other_name, other_url in remote_urls:
            if other_name != name:
                _git(executable, environment, stage, "remote", "add", "--", other_name, other_url,
                     timeout=30.0)
        return name
    raise last_error or BootstrapError("no official remote is reachable")


def _fetch_and_verify_tag(
    stage: Path,
    manifest: release_contract.ReleaseManifest,
    remote_name: str,
) -> None:
    """Fetch the signed release's exact tag and prove it resolves to the
    signed commit before anything checks it out (mirrors
    ``de_private_repo.py::_fetch_and_verify_tag``)."""
    if not _TAG_RE.fullmatch(manifest.tag):
        raise BootstrapError("signed release tag is invalid")
    executable = updater._resolve_git_executable(
        minimum_version=updater._MINIMUM_GIT_VERSION
    )
    environment = updater._ambient_git_environment(executable)
    ref = "refs/tags/%s" % manifest.tag
    temporary = "refs/bootstrap-verified-%s" % manifest.tag
    _git(executable, environment, stage, "fetch", "--no-recurse-submodules", "--",
         remote_name, "+%s:%s" % (ref, temporary), timeout=_FETCH_TIMEOUT_SECONDS,
         extra_config=_test_only_extra_git_config())
    fetched = _git(executable, environment, stage, "rev-parse", "--verify",
                    "%s^{commit}" % temporary, timeout=30.0).decode("ascii", "replace").strip()
    if fetched != manifest.commit:
        raise BootstrapError("fetched release tag does not match the signed commit")
    _git(executable, environment, stage, "update-ref", "--", ref, manifest.commit, timeout=30.0)


def _checkout_release(stage: Path, manifest: release_contract.ReleaseManifest) -> None:
    """Land the worktree at the verified tag, detached (mirrors
    ``de_private_repo.py::_checkout_release`` — ``activate_prepared_install``
    only compares resolved commit hashes, so a detached HEAD passes its
    checks exactly like an attached branch would)."""
    executable = updater._resolve_git_executable(
        minimum_version=updater._MINIMUM_GIT_VERSION
    )
    environment = updater._ambient_git_environment(executable)
    _git(executable, environment, stage, "checkout", "--detach", manifest.tag, timeout=60.0)
    head = _git(executable, environment, stage, "rev-parse", "--verify", "HEAD^{commit}",
                timeout=30.0).decode("ascii", "replace").strip()
    if head != manifest.commit:
        raise BootstrapError("checked-out HEAD does not match the signed release")
    try:
        version = (stage / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise BootstrapError("checked-out VERSION is unreadable") from exc
    if version != manifest.version:
        raise BootstrapError("checked-out VERSION does not match the signed release")


_NoQuarantine = object()


def _default_trust_root() -> Path:
    """The DE_ROOT this installer code is itself currently running from.

    A fresh install through this entry point inherits trust from the channel
    that delivered the installer code being executed. It must not replace
    that anchor with material from the not-yet-verified remote clone being
    bootstrapped.
    ``stable`` never carries a full source tree; it is a metadata channel
    containing only ``release/manifest.json`` and
    ``release/manifest.sig.json``. Therefore ``installer/release-trust.json``
    can never be read from a freshly cloned checkout of ``stable``. It must
    instead come from wherever this very module --
    ``installer.bootstrap_managed_install`` -- is currently loaded from,
    i.e. the DE_ROOT the human/agent is running ``install.sh``/this
    bootstrap from."""
    return Path(__file__).resolve().parent.parent


def _bootstrap_fresh_clone(
    canonical_root: Path,
    *,
    remote_urls: Sequence[Tuple[str, str]] = managed_install.OFFICIAL_REMOTE_URLS,
    fetch_document=release_acquisition.fetch_document,
    release_sources: Sequence[release_acquisition.ReleaseSource] = release_acquisition.RELEASE_SOURCES,
) -> None:
    """Clone + verify + land a brand-new managed checkout at ``canonical_root``.

    Stages next to the target and only ``rename``s into place once every
    verification step has passed; any failure quarantines the staging
    directory (never deletes it) and never touches ``canonical_root`` itself,
    since it does not exist yet on this path.

    ``rename`` onto an *existing* directory only raises when that directory is
    non-empty — an empty one is silently replaced — so the ``canonical_root.
    exists()`` check just above the rename does not by itself close the
    window where something outside this call's own lock (a stray ``mkdir``, a
    non-cooperating tool) creates an empty directory there in between. Rather
    than chase that race with a platform-specific atomic-rename primitive,
    the checkout is independently re-verified immediately after the rename
    using the same check an existing-root resume would use — so if anything
    other than this call's own staged content ended up at ``canonical_root``,
    it is caught and quarantined right away instead of being trusted.

    ``remote_urls``/``fetch_document``/``release_sources`` default to the real
    production values everywhere except tests. Tests replace private module
    seams to substitute local fixture mirrors without adding a caller-
    controlled trust-root option to the production API.
    """
    stage = canonical_root.with_name(".%s.bootstrapping" % canonical_root.name)
    if stage.exists():
        _quarantine(stage)
    try:
        remote_name = _clone_staging(stage, remote_urls)
        # Git applies ambient core.autocrlf while populating a worktree. On
        # Windows, pin the managed checkout policy before the first checkout
        # so the signed tree is materialized without EOL conversion.
        _pin_windows_checkout_policy(stage)
        trusted_keys = release_acquisition.load_trusted_release_keys(_default_trust_root())
        if not trusted_keys:
            raise BootstrapError(
                "production release public keys are not provisioned in this checkout"
            )
        deadline = time.monotonic() + _TRUST_DISCOVERY_BUDGET_SECONDS
        try:
            acquired = release_acquisition.discover_initial_release(
                trusted_keys, deadline=deadline, fetch_document=fetch_document, sources=release_sources,
            )
        except release_acquisition.ReleaseTransportError:
            # Anonymous HTTPS mirrors are unreachable — most likely this
            # repository is still private (see de_private_repo.py's identical
            # rationale). Falling back to the git credentials this call's own
            # `git clone` already authenticated with. (installer/launcher.py's
            # own ongoing auto-update now has its own, separately-scoped git
            # fallback too — see release_acquisition.discover_release_via_git
            # — this is a knowingly accepted private-beta-only exception, not
            # an absolute rule that only bootstrap gets to touch git creds.)
            print(
                "bootstrap: anonymous HTTPS mirrors unreachable — falling back to "
                "this clone's own authenticated git access to %s" % remote_name,
                file=sys.stderr,
            )
            acquired = release_acquisition.discover_initial_release_via_git(
                stage, (remote_name,), trusted_keys,
            )
        _fetch_and_verify_tag(stage, acquired.manifest, acquired.source.name)
        _checkout_release(stage, acquired.manifest)
        if canonical_root.exists():
            raise BootstrapError("managed root appeared during bootstrap")
        stage.rename(canonical_root)
        stage = _NoQuarantine  # sentinel: successfully moved, nothing left to quarantine
        if not _existing_root_is_valid_managed_checkout(canonical_root):
            # Something other than our own staged content is at canonical_root
            # now (see docstring) — refuse to hand it to install/activation.
            quarantined = _quarantine(canonical_root)
            raise BootstrapError(
                "managed root did not match the freshly-verified checkout immediately "
                "after landing it (quarantined to %s) — bootstrap again" % quarantined
            )
    finally:
        if stage is not _NoQuarantine and isinstance(stage, Path) and stage.exists():
            quarantined = _quarantine(stage)
            if quarantined:
                print("bootstrap: staging preserved for inspection: %s" % quarantined,
                      file=sys.stderr)


def _existing_root_is_valid_managed_checkout(canonical_root: Path) -> bool:
    """True only if ``canonical_root`` is already exactly the shape this
    module produces (dual official remotes). Never mutates anything; any
    ambiguity is treated as "not valid" so the caller refuses rather than
    guesses."""
    try:
        canonical = managed_install.canonical_managed_root(canonical_root)
    except managed_install.ManagedInstallError:
        return False
    try:
        reader = updater._GitReader(canonical)
        remotes = updater._read_remotes(reader)
        managed_install._validate_official_remotes(remotes)
    except (updater.UpdateInspectionError, managed_install.ManagedInstallError):
        return False
    return True


def bootstrap_new_install(
    *,
    canonical_root: Optional[Path] = None,
    server_endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    device_name: Optional[str] = None,
    clients: Optional[Sequence[str]] = None,
    remote_urls: Sequence[Tuple[str, str]] = managed_install.OFFICIAL_REMOTE_URLS,
    fetch_document=release_acquisition.fetch_document,
    release_sources: Sequence[release_acquisition.ReleaseSource] = release_acquisition.RELEASE_SOURCES,
) -> BootstrapResult:
    """Produce (or resume/repair) a managed checkout, then hand off to the
    existing, unchanged body-install and activation steps.

    - Nothing at ``canonical_root``: clone + verify + land fresh, then install
      the body/config and activate.
    - Something at ``canonical_root`` that is already the exact official
      dual-remote shape: skip cloning; if not yet activated, activate now
      (idempotent resume of an interrupted prior bootstrap); if already
      activated, this is a clean no-op.
    - Something at ``canonical_root`` that is anything else (no ``.git``, a
      single-remote clone, a raw developer checkout): refuse. Migrating that
      state is Phase 4's job, not this module's.
    """
    # Deliberately NOT `managed_install.canonical_managed_root()` here — that
    # helper requires `.git` to already exist and would raise before
    # `_existing_root_is_valid_managed_checkout` below gets a chance to turn
    # "exists but not a valid checkout" into our own, more specific refusal.
    root = Path(canonical_root).expanduser() if canonical_root else config.managed_component_root("decision-engine")
    root = Path(os.path.abspath(str(root)))

    # Only the "decide what to do, and clone if needed" phase is guarded by our
    # own acquisition of installer.install's lock. `run_install` and
    # `activate_prepared_install` each acquire their own locks internally
    # (`install_lock()` again, and `update_coordination.install_transaction`
    # respectively) — holding ours across those calls would self-deadlock,
    # since flock is not re-entrant across separate open() calls even from the
    # same process. The narrower window here still prevents two concurrent
    # bootstrap runs from racing on the clone/staging step, which is the part
    # that isn't already covered by another lock. `root.exists()` itself is
    # read INSIDE the lock (not before it) — reading it earlier would let two
    # concurrent callers both decide "nothing here yet" and both attempt a
    # full clone, wasting a redundant network round-trip and one of them
    # failing on the other's `stage.rename(canonical_root)`; deciding under
    # the lock means only one caller ever reaches that branch at a time.
    with install_module.install_lock():
        root_exists = root.exists()
        if root_exists:
            if not _existing_root_is_valid_managed_checkout(root):
                raise BootstrapError(
                    "%s already exists and is not a managed Decision Engine checkout "
                    "(wrong or missing remotes, or no .git at all) — refusing to overwrite it. "
                    "Use --dev-root for a developer checkout, or run the migration tool for an "
                    "existing legacy install." % root
                )
            protocol_marker = root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
            already_activated = protocol_marker.exists()
        else:
            _bootstrap_fresh_clone(
                root,
                remote_urls=remote_urls,
                fetch_document=fetch_document,
                release_sources=release_sources,
            )
            already_activated = False

    # Windows-only: pin the local git policy the launcher enforces
    # (updater._validate_local_checkout_policy). Runs for BOTH the freshly cloned
    # and the pre-existing-valid-checkout branches above — so re-running the
    # installer self-heals a checkout cloned before this pin existed, without a
    # delete-and-reinstall. No-op off Windows.
    _pin_windows_checkout_policy(root)

    summary = install_module.run_install(
        "de",
        bundle_root=root.parent,
        server_endpoint=server_endpoint,
        api_key=api_key,
        device_name=device_name,
    )
    del summary  # body/config landed; activation below owns MCP wiring + identity

    if already_activated:
        reader = updater._GitReader(root)
        head = updater._single_commit(reader.run("head")[1], "current HEAD")
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
        targets = tuple(clients if clients is not None else mcp_config.detect_clients())
        return BootstrapResult(root, version, head, "existing", targets, reused_existing=True)

    result = managed_activation.activate_prepared_install(root, clients=clients)
    return BootstrapResult(
        result.root, result.version, result.commit, result.source, result.clients,
        reused_existing=root_exists,
        failed_clients=result.failed_clients,
    )


def _validated_interpreter(executable: str) -> str:
    """Light sanity check before persisting an interpreter path into an
    external Agent client's config: it must exist, be executable, and
    actually run. Deliberately NOT a "must live inside the managed root's own
    venv" check -- this is explicitly the recovery path for "the previously
    recorded interpreter is gone or wrong", so a bare system interpreter is a
    legitimate answer here, not just one inside a venv; this only guards
    against persisting an obviously broken path (e.g. a stale/moved
    ``sys.executable`` in a frozen or relocated runtime)."""
    resolved = Path(executable)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise BootstrapError("interpreter %s is not an executable file" % executable)
    try:
        subprocess.run(
            [str(resolved), "--version"], check=True, timeout=10.0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError(
            "interpreter %s failed a basic sanity check: %s" % (executable, exc)
        ) from exc
    return str(resolved)


def repair_wiring(
    *, canonical_root: Optional[Path] = None, clients: Optional[Sequence[str]] = None
) -> Tuple[str, ...]:
    """Rediscover the current Python interpreter and rewrite MCP wiring for an
    already-activated managed root, without touching git state at all.

    ``sys.executable`` here is simply whatever interpreter is running this
    command right now, so running this with a working Python is itself the
    rediscovery step.
    """
    root = managed_install.canonical_managed_root(
        Path(canonical_root) if canonical_root else config.managed_component_root("decision-engine")
    )
    managed_install._require_fixed_managed_root(root)
    protocol_marker = root / update_transaction.PROTOCOL_READY_RELATIVE_PATH
    if not protocol_marker.exists():
        raise BootstrapError(
            "%s is not an activated managed install; run bootstrap install first" % root
        )
    # The protocol marker alone is checkout-internal and copy-reproducible --
    # it says "activation ran here at some point", not "this is genuinely the
    # identity-registered managed install". Cross-check it against the same
    # paired marker + private (outside-checkout) registration + live-remotes
    # identity that activation itself proves, before trusting this root
    # enough to rewrite external Agent configs to point at it.
    reader = updater._GitReader(root)
    live_remotes = updater._read_remotes(reader)
    try:
        managed_install.validate_managed_identity(root, live_remotes)
    except managed_install.ManagedInstallError as exc:
        raise BootstrapError(
            "%s does not have a valid managed install identity; refusing to rewire it (%s)"
            % (root, exc)
        ) from exc
    if not _device_is_permanently_activated(root):
        raise BootstrapError(
            "device activation is incomplete; run installer.permanent_setup before repairing MCP wiring"
        )
    targets = tuple(clients if clients is not None else mcp_config.detect_clients())
    if not targets:
        raise BootstrapError("at least one supported Agent client must be selected for repair")
    interpreter = _validated_interpreter(sys.executable)
    for client in targets:
        mcp_config.write_entry(client, python=interpreter, cwd=root)
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="installer.bootstrap_managed_install",
        description="Bootstrap or repair a managed Decision Engine checkout (no Bash required)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="clone/verify/land a fresh managed checkout, or resume an interrupted one"
    )
    install_parser.add_argument("--managed-root", type=Path, default=None)
    install_parser.add_argument("--server-endpoint", default=None)
    install_parser.add_argument("--api-key", default=None)
    install_parser.add_argument("--device-name", default=None)
    install_parser.add_argument("--client", action="append", choices=mcp_config.CLIENTS)

    repair_parser = subparsers.add_parser(
        "repair-wiring", help="rediscover the current Python interpreter and rewrite MCP wiring"
    )
    repair_parser.add_argument("--managed-root", type=Path, default=None)
    repair_parser.add_argument("--client", action="append", choices=mcp_config.CLIENTS)

    return parser


@contextlib.contextmanager
def _isolated_owner_environment() -> Iterator[Dict[str, str]]:
    """Hide owner values from every child except permanent device setup."""
    captured = {
        key: os.environ[key]
        for key in _OWNER_ENV_KEYS
        if os.environ.get(key)
    }
    previous = {key: os.environ.get(key) for key in _OWNER_ENV_KEYS}
    for key in _OWNER_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield captured
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_permanent_setup_from_env(
    root: Path,
    owner_environment: Mapping[str, str],
    *,
    clients: Sequence[str] = (),
) -> int:
    """Adopt preconfigured owner values without placing either value in argv."""
    child_environment = os.environ.copy()
    for key in _OWNER_ENV_KEYS:
        child_environment.pop(key, None)
    child_environment.update(
        (key, value)
        for key, value in owner_environment.items()
        if key in _OWNER_ENV_KEYS and value
    )
    try:
        command = [sys.executable, "-m", "installer.permanent_setup", "--from-env"]
        for client in clients:
            command.extend(("--client", client))
        completed = subprocess.run(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            env=child_environment,
            timeout=_PERMANENT_SETUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its direct child on timeout. Once
        # activation starts, activate_with_recovery_guard atomically writes its
        # marker before the network request. A timeout can also happen before that
        # marker exists, so always treat the outcome as uncertain and never retry
        # automatically. Core files stay valid; host wiring remains deferred
        # unless permanent setup completed activation before the timeout.
        print(
            "bootstrap: device activation timed out; core install remains ready, but "
            "activation and MCP wiring may be incomplete. Preserve any recovery "
            "marker and do not retry automatically; use owner-guided recovery.",
            file=sys.stderr,
        )
        return permanent_setup.RECOVERY_REQUIRED_EXIT_CODE
    except OSError:  # aqg: top-level boundary — base install remains usable
        print(
            "bootstrap: device activation could not start; core install remains ready "
            "and MCP wiring was not published by the bootstrap",
            file=sys.stderr,
        )
        return 1
    if completed.returncode == permanent_setup.RECOVERY_REQUIRED_EXIT_CODE:
        print(
            "bootstrap: device activation may have reached the service, but local credential "
            "persistence is uncertain; the recovery marker was retained, so do not retry "
            "automatically; use owner-guided recovery. "
            "Core install and automatic updates remain ready; MCP wiring is published "
            "only by successful permanent setup.",
            file=sys.stderr,
        )
    elif completed.returncode != 0:
        print(
            "bootstrap: device activation failed; core install remains ready and MCP wiring "
            "was not published by the bootstrap. "
            "Correct the owner values and rerun installer.permanent_setup --from-env.",
            file=sys.stderr,
        )
    return completed.returncode


def _device_is_permanently_activated(root: Path) -> bool:
    """Read only the local activation shape; never expose config values."""
    try:
        current = config.load_json(root / "config.json")
    except (ShellError, OSError, UnicodeError):
        return False
    return permanent_setup.activate.is_permanently_activated(current)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    with _isolated_owner_environment() as owner_environment:
        try:
            if args.command == "install":
                result = bootstrap_new_install(
                    canonical_root=args.managed_root,
                    server_endpoint=args.server_endpoint,
                    api_key=args.api_key,
                    device_name=args.device_name,
                    clients=args.client,
                )
                setup_exit = _run_permanent_setup_from_env(
                    result.root,
                    owner_environment,
                    clients=result.clients,
                )
                try:
                    gui_setup.prepare_gui_environment(sys.executable)
                except Exception:  # aqg: top-level boundary — optional GUI setup never blocks core install
                    print(
                        "bootstrap: optional GUI preparation failed unexpectedly; "
                        "core install remains ready and Doctor can diagnose it later",
                        file=sys.stderr,
                    )
                if setup_exit == 0 and _device_is_permanently_activated(result.root):
                    print(
                        "bootstrap: activated %s@%s via %s at %s; restart %s"
                        % (result.version, result.commit[:12], result.source, result.root,
                           ", ".join(result.clients))
                    )
                else:
                    print(
                        "bootstrap: core ready %s@%s via %s at %s; device activation is "
                        "pending and MCP wiring is deferred"
                        % (result.version, result.commit[:12], result.source, result.root)
                    )
                for client, reason in result.failed_clients:
                    print(
                        "bootstrap: %s wiring failed: %s" % (client, reason),
                        file=sys.stderr,
                    )
            else:
                targets = repair_wiring(canonical_root=args.managed_root, clients=args.client)
                print("bootstrap: rewired MCP for %s" % ", ".join(targets))
                for notice in mcp_config.post_mcp_write_notices(targets):
                    print(mcp_config.console_safe_text("bootstrap: %s" % notice))
        except (ShellError, OSError, ValueError) as exc:
            print("bootstrap: %s" % exc, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
