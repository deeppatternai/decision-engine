"""Bounded, fail-closed acquisition of signed stable release metadata and objects.

The two mirrors are availability peers, not independent trust roots.  Every
document is authorized by the same locally pinned release key before Git is
allowed to fetch the manifest's immutable tag.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import managed_install, release_contract, updater


MAX_RELEASE_DOCUMENT_BYTES = 64 * 1024
TRUST_STORE_RELATIVE_PATH = Path("installer") / "release-trust.json"


class ReleaseAcquisitionError(release_contract.ReleaseContractError):
    """A release could not be acquired safely."""


class ReleaseTransportError(ReleaseAcquisitionError):
    """A mirror was unreachable; only this error permits mirror fallback."""


class ReleaseBudgetExpired(ReleaseTransportError):
    """The one shared launcher startup budget has expired."""


class ReleaseTrustUnavailable(ReleaseAcquisitionError):
    """No usable production release trust anchor is installed."""


@dataclass(frozen=True)
class ReleaseSource:
    name: str
    manifest_url: str
    signature_url: str


@dataclass(frozen=True)
class AcquiredRelease:
    source: ReleaseSource
    manifest: release_contract.ReleaseManifest
    signature: release_contract.ReleaseSignature
    uses_git_credentials: bool = False


GITHUB_SOURCE = ReleaseSource(
    "github",
    "https://raw.githubusercontent.com/deeppatternai/decision-engine/stable/release/manifest.json",
    "https://raw.githubusercontent.com/deeppatternai/decision-engine/stable/release/manifest.sig.json",
)
GITEE_SOURCE = ReleaseSource(
    "gitee",
    "https://gitee.com/deeppatternai/decision-engine/raw/stable/release/manifest.json",
    "https://gitee.com/deeppatternai/decision-engine/raw/stable/release/manifest.sig.json",
)
RELEASE_SOURCES = (GITHUB_SOURCE, GITEE_SOURCE)

_SOURCE_BY_NAME = {source.name: source for source in RELEASE_SOURCES}


def _canonical_source(remote_name: str) -> ReleaseSource:
    """Map an official remote name (github/gitee) to its canonical
    ``RELEASE_SOURCES`` singleton.

    The git-credential fallback discovery paths MUST return one of these exact
    instances — not a freshly built ``ReleaseSource(remote_name, ref, ref)`` —
    because ``fetch_release_objects`` gates the release with
    ``acquired.source not in RELEASE_SOURCES`` (value identity over all three
    fields). A synthetic source whose url fields hold a git ref never equals
    ``GITHUB_SOURCE`` / ``GITEE_SOURCE``, so the ongoing per-MCP-start update on
    a private repo (where the anonymous mirrors are unreachable and the git
    fallback is the ONLY path) was rejected with "authorized release source is
    invalid". ``fetch_release_objects`` fetches by ``source.name`` and
    re-verifies the tag against the signed commit, so the canonical instance is
    both correct and sufficient. ``remote_names`` are already filtered to the
    official remotes by callers, so a miss is a misconfigured checkout — a hard
    error, not a transport fallback."""
    source = _SOURCE_BY_NAME.get(remote_name)
    if source is None:
        raise ReleaseAcquisitionError(
            "git release source %r is not an official remote" % remote_name
        )
    return source


_TRUST_STORE_KEYS = {"schema", "keys"}
_TRUST_KEY_KEYS = {"key_id", "algorithm", "modulus_hex", "exponent", "revoked"}
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MODULUS_RE = re.compile(r"[0-9a-f]{512,2048}\Z")


def _remaining(deadline: float) -> float:
    if not isinstance(deadline, (int, float)):
        raise ReleaseBudgetExpired("release startup budget is invalid")
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise ReleaseBudgetExpired("release startup budget expired")
    return remaining


def _document_url(source: ReleaseSource, kind: str) -> str:
    if source not in RELEASE_SOURCES:
        raise ReleaseAcquisitionError("release source is not on the fixed allowlist")
    if kind == "manifest":
        return source.manifest_url
    if kind == "signature":
        return source.signature_url
    raise ReleaseAcquisitionError("release document kind is invalid")


_FETCH_SCRIPT = r"""
import socket, ssl, sys, urllib.error, urllib.parse, urllib.request
url = sys.argv[1]
limit = int(sys.argv[2])
timeout = float(sys.argv[3])
initial_url = urllib.parse.urlsplit(url)
if initial_url.scheme != "https" or not initial_url.hostname:
    sys.exit(8)
initial_host = initial_url.hostname
class SameHostHttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != initial_host:
            raise urllib.error.URLError("release redirect left the fixed HTTPS host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
try:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "decision-engine-updater/1"})
    opener = urllib.request.build_opener(SameHostHttpsRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise urllib.error.HTTPError(url, response.status, "unexpected status", response.headers, None)
        body = response.read(limit + 1)
    if len(body) > limit:
        sys.exit(5)
    sys.stdout.buffer.write(body)
except ssl.SSLCertVerificationError:
    sys.exit(3)
except urllib.error.HTTPError as exc:
    if exc.code in (404, 408, 429, 500, 502, 503, 504):
        sys.exit(7)
    sys.exit(4)
except urllib.error.URLError as exc:
    reason = exc.reason
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        sys.exit(3)
    if isinstance(reason, (socket.timeout, TimeoutError, socket.gaierror, ConnectionError, OSError)):
        sys.exit(2)
    sys.exit(6)
except (socket.timeout, TimeoutError, socket.gaierror, ConnectionError):
    sys.exit(2)
except ssl.SSLError:
    sys.exit(3)
except Exception:
    sys.exit(6)
"""


def fetch_document(source: ReleaseSource, kind: str, *, deadline: float) -> bytes:
    """Fetch one fixed public document under a hard parent-enforced deadline."""

    remaining = _remaining(deadline)
    url = _document_url(source, kind)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _FETCH_SCRIPT,
                url,
                str(MAX_RELEASE_DOCUMENT_BYTES),
                str(max(0.05, remaining)),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            shell=False,
        )
        try:
            output, _ignored = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            try:
                process.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
            raise ReleaseBudgetExpired("release startup budget expired") from exc
    except ReleaseAcquisitionError:
        raise
    except (OSError, ValueError) as exc:
        raise ReleaseTransportError("release transport could not start") from exc
    if process.returncode == 0:
        if len(output) > MAX_RELEASE_DOCUMENT_BYTES:
            raise ReleaseAcquisitionError("release document exceeds the size limit")
        return output
    if process.returncode == 2:
        raise ReleaseTransportError("release mirror is unreachable")
    if process.returncode == 3:
        raise ReleaseAcquisitionError("release mirror TLS verification failed")
    if process.returncode == 4:
        raise ReleaseAcquisitionError("release mirror returned an unexpected HTTP response")
    if process.returncode == 7:
        raise ReleaseTransportError("release mirror is temporarily unavailable")
    if process.returncode == 8:
        raise ReleaseAcquisitionError("release URL or redirect left the fixed HTTPS host")
    if process.returncode == 5:
        raise ReleaseAcquisitionError("release document exceeds the size limit")
    raise ReleaseAcquisitionError("release mirror response could not be validated")


def _discover_from_source(
    source: ReleaseSource,
    state: updater.UpdateState,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    deadline: float,
    fetch_document: Callable[..., bytes],
) -> AcquiredRelease:
    _remaining(deadline)
    manifest = release_contract.parse_release_manifest(
        fetch_document(source, "manifest", deadline=deadline)
    )
    _remaining(deadline)
    signature = release_contract.parse_release_signature(
        fetch_document(source, "signature", deadline=deadline)
    )
    release_contract.authorize_release(
        manifest,
        signature,
        trusted_keys,
        expected_repository_id=managed_install.REPOSITORY_ID,
        expected_channel=state.channel,
        last_sequence=state.last_release_sequence,
        running_python=tuple(sys.version_info[:3]),
        last_commit=state.last_release_commit,
        last_manifest_sha256=state.last_manifest_sha256,
    )
    return AcquiredRelease(source, manifest, signature)


def discover_release(
    state: updater.UpdateState,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    deadline: float,
    fetch_document: Callable[..., bytes] = fetch_document,
    sources: Sequence[ReleaseSource] = RELEASE_SOURCES,
) -> AcquiredRelease:
    """Return the first authorized release; fallback only on reachability failure."""

    if not trusted_keys:
        raise ReleaseTrustUnavailable("no trusted release key is installed")
    if not isinstance(state, updater.UpdateState):
        raise ReleaseAcquisitionError("protected update state is required")
    if not sources or any(source not in RELEASE_SOURCES for source in sources):
        raise ReleaseAcquisitionError("release source order is invalid")
    last_transport: Optional[ReleaseTransportError] = None
    for source in sources:
        try:
            return _discover_from_source(
                source,
                state,
                trusted_keys,
                deadline=deadline,
                fetch_document=fetch_document,
            )
        except ReleaseTransportError as exc:
            last_transport = exc
            _remaining(deadline)
    raise last_transport or ReleaseTransportError("no release mirror is reachable")


def discover_release_via_git(
    root: Path,
    remote_names: Sequence[str],
    state: updater.UpdateState,
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    deadline: float,
) -> AcquiredRelease:
    """Ongoing, unattended per-MCP-start fallback for a still-private repository.

    Deliberately reachable from ``installer.launcher``'s automatic update
    check on every MCP start — unlike ``discover_initial_release_via_git``
    (a one-time, human-triggered bootstrap/activation step), this one is not
    scoped to an explicit human action. That is a knowing, accepted exception
    to "never touch personal git credentials in the background", accepted
    only for the private-beta window: ``discover_release``'s own mirror loop
    (see its caller in ``installer.launcher``) only reaches this after every
    anonymous mirror raised ``ReleaseTransportError``, so once the repository
    is public the anonymous path succeeds first and this git path is never
    exercised again.

    Each call still does a fresh, bounded ``git fetch`` of ``stable`` from
    the caller's own already-authenticated remote (so a genuinely new release
    is detected, not just whatever a prior clone happened to have on disk),
    and every document is still run through the exact same
    ``authorize_release`` anti-rollback/sequence/signature checks the
    anonymous path uses — only the transport differs, so no trust boundary
    changes and a real signature/sequence failure still hard-stops instead of
    silently trying another remote.
    """

    if not trusted_keys:
        raise ReleaseTrustUnavailable("no trusted release key is installed")
    if not isinstance(state, updater.UpdateState):
        raise ReleaseAcquisitionError("protected update state is required")
    executable = updater._resolve_git_executable()
    environment = updater._ambient_git_environment(executable)
    last_transport: Optional[ReleaseTransportError] = None
    for remote_name in remote_names:
        remaining = _remaining(deadline)
        ref = "%s/stable" % remote_name
        try:
            code, _output = updater._run_bounded_git(
                (
                    executable, "-C", str(root), "fetch", "--quiet", "--no-tags",
                    "--no-recurse-submodules", remote_name,
                    "+refs/heads/stable:refs/remotes/%s" % ref,
                ),
                environment, timeout_seconds=remaining,
            )
            if code != 0:
                raise ReleaseTransportError(
                    "git fetch of stable failed via %s" % remote_name
                )
            code, manifest_bytes = updater._run_bounded_git(
                (executable, "-C", str(root), "show", "%s:release/manifest.json" % ref),
                environment, timeout_seconds=30.0,
            )
            if code != 0:
                raise ReleaseTransportError(
                    "could not read release manifest via git from %s" % ref
                )
            code, signature_bytes = updater._run_bounded_git(
                (executable, "-C", str(root), "show", "%s:release/manifest.sig.json" % ref),
                environment, timeout_seconds=30.0,
            )
            if code != 0:
                raise ReleaseTransportError(
                    "could not read release signature via git from %s" % ref
                )
            manifest = release_contract.parse_release_manifest(manifest_bytes)
            signature = release_contract.parse_release_signature(signature_bytes)
            release_contract.authorize_release(
                manifest,
                signature,
                trusted_keys,
                expected_repository_id=managed_install.REPOSITORY_ID,
                expected_channel=state.channel,
                last_sequence=state.last_release_sequence,
                running_python=tuple(sys.version_info[:3]),
                last_commit=state.last_release_commit,
                last_manifest_sha256=state.last_manifest_sha256,
            )
            return AcquiredRelease(
                _canonical_source(remote_name),
                manifest,
                signature,
                uses_git_credentials=True,
            )
        except ReleaseTransportError as exc:
            last_transport = exc
    raise last_transport or ReleaseTransportError(
        "no configured remote has local git access to the release manifest"
    )


def discover_initial_release(
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
    *,
    deadline: float,
    fetch_document: Callable[..., bytes] = fetch_document,
    sources: Sequence[ReleaseSource] = RELEASE_SOURCES,
) -> AcquiredRelease:
    """Authorize the first release for a freshly prepared managed checkout.

    There is deliberately no anti-rollback prior yet.  The activation boundary
    separately proves that HEAD, the immutable tag and VERSION all match this
    signed release before it creates update-state.
    """

    if not trusted_keys:
        raise ReleaseTrustUnavailable("no trusted release key is installed")
    if not sources or any(source not in RELEASE_SOURCES for source in sources):
        raise ReleaseAcquisitionError("release source order is invalid")
    last_transport: Optional[ReleaseTransportError] = None
    for source in sources:
        try:
            _remaining(deadline)
            manifest = release_contract.parse_release_manifest(
                fetch_document(source, "manifest", deadline=deadline)
            )
            _remaining(deadline)
            signature = release_contract.parse_release_signature(
                fetch_document(source, "signature", deadline=deadline)
            )
            release_contract.verify_release_signature(
                manifest,
                signature,
                trusted_keys,
                expected_repository_id=managed_install.REPOSITORY_ID,
                expected_channel=managed_install.CHANNEL,
            )
            if not release_contract.python_is_compatible(
                manifest, tuple(sys.version_info[:3])
            ):
                raise ReleaseAcquisitionError(
                    "release requires a newer Python runtime"
                )
            return AcquiredRelease(source, manifest, signature)
        except ReleaseTransportError as exc:
            last_transport = exc
            _remaining(deadline)
    raise last_transport or ReleaseTransportError("no release mirror is reachable")


def discover_initial_release_via_git(
    root: Path,
    remote_names: Sequence[str],
    trusted_keys: Mapping[str, release_contract.RsaPublicKey],
) -> AcquiredRelease:
    """One-time, human-triggered fallback for a still-private repository.

    Reads the same tracked ``release/manifest.json`` / ``release/manifest.sig.json``
    bytes the anonymous-HTTPS mirrors would have served, via ``git show
    <remote>/stable:...`` against a checkout whose ``git clone``/``fetch`` this
    process already ran with the caller's own, already-authenticated git
    credentials. Verification below is byte-for-byte identical to
    ``discover_initial_release`` — only the transport differs, so no trust
    boundary changes; a signature/schema/sequence failure still raises exactly
    as it would from the anonymous path.

    Callers only reach this after the anonymous mirrors already raised
    ``ReleaseTransportError`` (see ``bootstrap_managed_install`` and
    ``managed_activation``, both exclusively one-time, explicitly
    human-triggered flows). This specific function is still bootstrap/
    activation-only — it authorizes with no anti-rollback prior, which is
    only safe before any protected update state exists yet. The ongoing,
    unattended per-MCP-start path in ``installer.launcher`` uses the separate
    ``discover_release_via_git`` below instead, which requires and checks
    against that protected state; see its docstring for the (deliberate,
    knowingly accepted) private-beta-only exception this project makes to
    "never touch personal git credentials in the background."
    """

    if not trusted_keys:
        raise ReleaseTrustUnavailable("no trusted release key is installed")
    executable = updater._resolve_git_executable()
    environment = updater._ambient_git_environment(executable)
    last_transport: Optional[ReleaseTransportError] = None
    for remote_name in remote_names:
        ref = "%s/stable" % remote_name
        try:
            code, manifest_bytes = updater._run_bounded_git(
                (executable, "-C", str(root), "show", "%s:release/manifest.json" % ref),
                environment, timeout_seconds=30.0,
            )
            if code != 0:
                raise ReleaseTransportError(
                    "could not read release manifest via git from %s" % ref
                )
            code, signature_bytes = updater._run_bounded_git(
                (executable, "-C", str(root), "show", "%s:release/manifest.sig.json" % ref),
                environment, timeout_seconds=30.0,
            )
            if code != 0:
                raise ReleaseTransportError(
                    "could not read release signature via git from %s" % ref
                )
            manifest = release_contract.parse_release_manifest(manifest_bytes)
            signature = release_contract.parse_release_signature(signature_bytes)
            release_contract.verify_release_signature(
                manifest,
                signature,
                trusted_keys,
                expected_repository_id=managed_install.REPOSITORY_ID,
                expected_channel=managed_install.CHANNEL,
            )
            if not release_contract.python_is_compatible(
                manifest, tuple(sys.version_info[:3])
            ):
                raise ReleaseAcquisitionError(
                    "release requires a newer Python runtime"
                )
            return AcquiredRelease(
                _canonical_source(remote_name),
                manifest,
                signature,
                uses_git_credentials=True,
            )
        except ReleaseTransportError as exc:
            last_transport = exc
    raise last_transport or ReleaseTransportError(
        "no configured remote has local git access to the release manifest"
    )


def load_trusted_release_keys(
    root: Path,
    *,
    deadline: Optional[float] = None,
    expected_commit: Optional[str] = None,
) -> Mapping[str, release_contract.RsaPublicKey]:
    """Load strict tracked public-key material; absence deliberately disables updates."""

    path = Path(root) / TRUST_STORE_RELATIVE_PATH
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ReleaseTrustUnavailable("release trust store is unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > MAX_RELEASE_DOCUMENT_BYTES:
        raise ReleaseTrustUnavailable("release trust store has an unsafe file type or size")
    try:
        worktree_bytes = path.read_bytes()
        loaded = json.loads(worktree_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleaseTrustUnavailable("release trust store is invalid") from exc
    try:
        reader = updater._GitReader(
            Path(root),
            deadline=deadline,
            trust_store_only=True,
        )
        commit = expected_commit
        if commit is None:
            _code, head_output = reader.run("head")
            commit = updater._single_commit(head_output, "release trust HEAD")
        if not updater._COMMIT_RE.fullmatch(commit):
            raise ReleaseTrustUnavailable("release trust commit is invalid")
        _code, tracked_bytes = reader.run("trust_store", commit=commit)
        tracked = json.loads(tracked_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, updater.UpdateInspectionError) as exc:
        raise ReleaseTrustUnavailable(
            "release trust store is not anchored in the current known-good commit"
        ) from exc
    if tracked != loaded:
        raise ReleaseTrustUnavailable(
            "release trust store differs from the current known-good commit"
        )
    loaded = tracked
    if not isinstance(loaded, dict) or set(loaded) != _TRUST_STORE_KEYS or loaded.get("schema") != 1:
        raise ReleaseTrustUnavailable("release trust store schema is invalid")
    items = loaded.get("keys")
    if not isinstance(items, list) or len(items) > 16:
        raise ReleaseTrustUnavailable("release trust key list is invalid")
    keys = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != _TRUST_KEY_KEYS:
            raise ReleaseTrustUnavailable("release trust key schema is invalid")
        key_id = item.get("key_id")
        modulus_text = item.get("modulus_hex")
        exponent = item.get("exponent")
        if (
            not isinstance(key_id, str)
            or not _KEY_ID_RE.fullmatch(key_id)
            or key_id in keys
            or item.get("algorithm") != release_contract.RSA_SHA256_ALGORITHM
            or type(item.get("revoked")) is not bool
            or not isinstance(modulus_text, str)
            or not _MODULUS_RE.fullmatch(modulus_text)
            or type(exponent) is not int
        ):
            raise ReleaseTrustUnavailable("release trust key is invalid")
        if not item["revoked"]:
            keys[key_id] = release_contract.RsaPublicKey(
                key_id, int(modulus_text, 16), exponent
            )
    return keys


def fetch_release_objects(root: Path, acquired: AcquiredRelease, *, deadline: float) -> None:
    """Fetch only the authorized immutable tag into the already-validated checkout.

    Anonymous release discovery keeps the fully isolated Git environment. The
    private-beta authenticated fallback carries explicit provenance so this
    exact-tag fetch can resolve the same ambient credential helper; every
    identity, refspec and signed-commit check remains unchanged.
    """

    _remaining(deadline)
    if (
        not isinstance(acquired, AcquiredRelease)
        or acquired.source not in RELEASE_SOURCES
        or type(acquired.uses_git_credentials) is not bool
    ):
        raise ReleaseAcquisitionError("authorized release source is invalid")
    reader = updater._GitReader(Path(root), deadline=deadline)
    remotes = updater._read_remotes(reader)
    identity = managed_install.validate_managed_identity(Path(root), remotes)
    if identity.remotes.get(acquired.source.name) != dict(managed_install.OFFICIAL_REMOTE_URLS)[acquired.source.name]:
        raise ReleaseAcquisitionError("release source does not match managed Git identity")
    tag = acquired.manifest.tag
    refspec = "refs/tags/%s:refs/tags/%s" % (tag, tag)
    credential_arguments = () if acquired.uses_git_credentials else (
        "-c", "credential.helper=",
    )
    environment = reader.environment
    if acquired.uses_git_credentials:
        environment = updater._ambient_git_environment(reader.git_executable)
    argv = (
        reader.git_executable,
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "-c", "gc.auto=0",
        "-c", "maintenance.auto=0",
        "-c", "core.hooksPath=" + os.devnull,
        *credential_arguments,
        "-C", str(identity.canonical_root),
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        acquired.source.name,
        refspec,
    )
    remaining = _remaining(deadline)
    code, _output = updater._run_bounded_git(
        argv, environment, timeout_seconds=remaining
    )
    if code != 0:
        raise ReleaseAcquisitionError("authorized release Git fetch failed")
    _code, output = reader.run("target", tag=tag)
    if updater._single_commit(output, "release tag") != acquired.manifest.commit:
        raise ReleaseAcquisitionError("fetched release tag does not match the signed commit")
