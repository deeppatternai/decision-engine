"""Strict signed-release data contract used before any future Git mutation.

This module parses deterministic manifest/signature documents, verifies a
detached RSA PKCS#1 v1.5 SHA-256 signature against caller-supplied trusted public
keys, and enforces mirror/sequence/Python compatibility rules. It performs no
network, Git or filesystem operation and contains no production private key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .config import ShellError


MANIFEST_SCHEMA_VERSION = 1
SIGNATURE_SCHEMA_VERSION = 1
RSA_SHA256_ALGORITHM = "rsa-pkcs1v15-sha256"
_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_SIGNATURE_TEXT = 8192
_MAX_RSA_MODULUS_BITS = 8192
_MAX_RELEASE_SEQUENCE = (1 << 63) - 1
_SEMVER_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PYTHON_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REPOSITORY_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z"
)
_UTC_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")
_MANIFEST_KEYS = {
    "schema", "repository_id", "channel", "release_sequence", "version", "tag", "commit",
    "min_python", "published_at", "key_id",
}
_SIGNATURE_KEYS = {"schema", "algorithm", "key_id", "signature"}


class ReleaseContractError(ShellError):
    """A release artifact is malformed, untrusted or conflicts with local state."""


@dataclass(frozen=True)
class ReleaseManifest:
    schema: int
    repository_id: str
    channel: str
    release_sequence: int
    version: str
    tag: str
    commit: str
    min_python: str
    published_at: str
    key_id: str


@dataclass(frozen=True)
class ReleaseSignature:
    schema: int
    algorithm: str
    key_id: str
    signature: str


@dataclass(frozen=True)
class RsaPublicKey:
    key_id: str
    modulus: int
    exponent: int


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: ReleaseManifest
    key_id: str


JsonInput = Union[bytes, str, Mapping[str, Any]]


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseContractError("release document contains a duplicate JSON key")
        value[key] = item
    return value


def _load_exact_object(payload: JsonInput, expected_keys: set, label: str) -> Dict[str, Any]:
    if isinstance(payload, bytes):
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise ReleaseContractError("%s document exceeds the size limit" % label)
        try:
            value = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseContractError("%s document is not valid UTF-8 JSON" % label) from exc
    elif isinstance(payload, str):
        encoded = payload.encode("utf-8")
        if len(encoded) > _MAX_DOCUMENT_BYTES:
            raise ReleaseContractError("%s document exceeds the size limit" % label)
        try:
            value = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ReleaseContractError("%s document is not valid JSON" % label) from exc
    elif isinstance(payload, Mapping):
        value = dict(payload)
    else:
        raise ReleaseContractError("%s document must be a JSON object" % label)
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ReleaseContractError("%s document has an unexpected schema" % label)
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseContractError("release %s must be non-empty text" % name)
    return value


def parse_release_manifest(payload: JsonInput) -> ReleaseManifest:
    value = _load_exact_object(payload, _MANIFEST_KEYS, "release manifest")
    if type(value.get("schema")) is not int or value["schema"] != MANIFEST_SCHEMA_VERSION:
        raise ReleaseContractError("release manifest schema is not supported")
    sequence = value.get("release_sequence")
    if type(sequence) is not int or not 1 <= sequence <= _MAX_RELEASE_SEQUENCE:
        raise ReleaseContractError("release sequence must be a positive integer")
    repository_id = _require_text(value.get("repository_id"), "repository_id")
    if not _REPOSITORY_ID_RE.fullmatch(repository_id):
        raise ReleaseContractError("release repository_id is invalid")
    channel = _require_text(value.get("channel"), "channel")
    if channel not in {"stable", "beta"}:
        raise ReleaseContractError("release channel is not supported")
    version = _require_text(value.get("version"), "version")
    if not _SEMVER_RE.fullmatch(version):
        raise ReleaseContractError("release version is not canonical semantic version text")
    tag = _require_text(value.get("tag"), "tag")
    if tag != "v" + version:
        raise ReleaseContractError("release tag does not match version")
    commit = _require_text(value.get("commit"), "commit")
    if not _COMMIT_RE.fullmatch(commit):
        raise ReleaseContractError("release commit must be a full lowercase Git SHA-1")
    minimum = _require_text(value.get("min_python"), "min_python")
    if not _PYTHON_RE.fullmatch(minimum):
        raise ReleaseContractError("release min_python must be major.minor")
    published = _require_text(value.get("published_at"), "published_at")
    if not _UTC_TIMESTAMP_RE.fullmatch(published):
        raise ReleaseContractError("release published_at must be canonical UTC time")
    try:
        datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ReleaseContractError("release published_at must be canonical UTC time") from exc
    key_id = _require_text(value.get("key_id"), "key_id")
    if not _KEY_ID_RE.fullmatch(key_id):
        raise ReleaseContractError("release key_id is invalid")
    return ReleaseManifest(
        schema=value["schema"],
        repository_id=repository_id,
        channel=channel,
        release_sequence=sequence,
        version=version,
        tag=tag,
        commit=commit,
        min_python=minimum,
        published_at=published,
        key_id=key_id,
    )


def parse_release_signature(payload: JsonInput) -> ReleaseSignature:
    value = _load_exact_object(payload, _SIGNATURE_KEYS, "release signature")
    if type(value.get("schema")) is not int or value["schema"] != SIGNATURE_SCHEMA_VERSION:
        raise ReleaseContractError("release signature schema is not supported")
    algorithm = _require_text(value.get("algorithm"), "signature algorithm")
    if algorithm != RSA_SHA256_ALGORITHM:
        raise ReleaseContractError("release signature algorithm is not supported")
    key_id = _require_text(value.get("key_id"), "signature key_id")
    if not _KEY_ID_RE.fullmatch(key_id):
        raise ReleaseContractError("release signature key_id is invalid")
    signature = _require_text(value.get("signature"), "signature")
    if len(signature) > _MAX_SIGNATURE_TEXT:
        raise ReleaseContractError("release signature exceeds the size limit")
    try:
        decoded = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseContractError("release signature is not canonical base64") from exc
    if base64.b64encode(decoded).decode("ascii") != signature:
        raise ReleaseContractError("release signature is not canonical base64")
    return ReleaseSignature(
        schema=value["schema"], algorithm=algorithm, key_id=key_id, signature=signature
    )


def canonical_manifest_bytes(manifest: ReleaseManifest) -> bytes:
    if not isinstance(manifest, ReleaseManifest):
        raise ReleaseContractError("release manifest must be parsed before canonicalization")
    manifest = parse_release_manifest(asdict(manifest))
    return json.dumps(
        asdict(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def manifest_sha256(manifest: ReleaseManifest) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def _validated_public_key(key: RsaPublicKey, expected_id: str) -> RsaPublicKey:
    if not isinstance(key, RsaPublicKey) or key.key_id != expected_id:
        raise ReleaseContractError("trusted release key is invalid")
    if type(key.modulus) is not int \
            or not 2048 <= key.modulus.bit_length() <= _MAX_RSA_MODULUS_BITS \
            or key.modulus % 2 == 0:
        raise ReleaseContractError("trusted release RSA modulus is invalid")
    if type(key.exponent) is not int \
            or key.exponent < 3 \
            or key.exponent >= key.modulus \
            or key.exponent % 2 == 0:
        raise ReleaseContractError("trusted release RSA exponent is invalid")
    return key


def verify_release_signature(
    manifest: ReleaseManifest,
    signature: ReleaseSignature,
    trusted_keys: Mapping[str, RsaPublicKey],
    *,
    expected_repository_id: str,
    expected_channel: str,
) -> VerifiedRelease:
    """Raise unless the detached signature exactly authenticates ``manifest``."""
    if not isinstance(manifest, ReleaseManifest):
        raise ReleaseContractError("release manifest must be parsed before verification")
    if not isinstance(signature, ReleaseSignature):
        raise ReleaseContractError("release signature contract is invalid")
    manifest = parse_release_manifest(asdict(manifest))
    signature = parse_release_signature(asdict(signature))
    if not isinstance(expected_repository_id, str) \
            or manifest.repository_id != expected_repository_id:
        raise ReleaseContractError("release repository does not match the managed installation")
    if not isinstance(expected_channel, str) or manifest.channel != expected_channel:
        raise ReleaseContractError("release channel does not match the managed installation")
    if signature.key_id != manifest.key_id:
        raise ReleaseContractError("release signature key ID does not match manifest")
    key = trusted_keys.get(signature.key_id) if isinstance(trusted_keys, Mapping) else None
    if key is None:
        raise ReleaseContractError("release signing key is not trusted")
    key = _validated_public_key(key, signature.key_id)
    try:
        raw = base64.b64decode(signature.signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReleaseContractError("release signature is not canonical base64") from exc
    width = (key.modulus.bit_length() + 7) // 8
    if base64.b64encode(raw).decode("ascii") != signature.signature:
        raise ReleaseContractError("release signature is not canonical base64")
    if len(raw) != width:
        raise ReleaseContractError("release signature verification failed")
    signature_int = int.from_bytes(raw, "big")
    if signature_int >= key.modulus:
        raise ReleaseContractError("release signature verification failed")
    recovered = pow(signature_int, key.exponent, key.modulus).to_bytes(width, "big")
    digest = hashlib.sha256(canonical_manifest_bytes(manifest)).digest()
    encoded = _DIGEST_INFO_SHA256 + digest
    padding_len = width - len(encoded) - 3
    if padding_len < 8:
        raise ReleaseContractError("trusted release RSA modulus is too small")
    expected = b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + encoded
    if not hmac.compare_digest(recovered, expected):
        raise ReleaseContractError("release signature verification failed")
    return VerifiedRelease(manifest=manifest, key_id=signature.key_id)


def authorize_release(
    manifest: ReleaseManifest,
    signature: ReleaseSignature,
    trusted_keys: Mapping[str, RsaPublicKey],
    *,
    expected_repository_id: str,
    expected_channel: str,
    last_sequence: int,
    running_python: Tuple[int, int, int],
    last_commit: Optional[str] = None,
    last_manifest_sha256: Optional[str] = None,
) -> VerifiedRelease:
    """Authenticate and authorize one release in a single fail-closed call.

    ``last_sequence`` and ``last_commit`` must come from the protected
    last-known-good state owned by the updater. A rollback publishes a new,
    higher sequence that points to the prior commit; sequence never decreases.
    """
    verified = verify_release_signature(
        manifest,
        signature,
        trusted_keys,
        expected_repository_id=expected_repository_id,
        expected_channel=expected_channel,
    )
    candidate = verified.manifest
    if type(last_sequence) is not int or last_sequence < 0:
        raise ReleaseContractError("local release sequence is invalid")
    if last_sequence > 0 and last_commit is None:
        raise ReleaseContractError("local release commit is required")
    if last_sequence > 0 and last_manifest_sha256 is None:
        raise ReleaseContractError("local release manifest digest is required")
    if last_commit is not None and not _COMMIT_RE.fullmatch(last_commit):
        raise ReleaseContractError("local release commit is invalid")
    if last_manifest_sha256 is not None and not _SHA256_RE.fullmatch(last_manifest_sha256):
        raise ReleaseContractError("local release manifest digest is invalid")
    if not python_is_compatible(candidate, running_python):
        raise ReleaseContractError("release requires a newer Python runtime")
    if candidate.release_sequence < last_sequence:
        raise ReleaseContractError("release sequence downgrade is not authorized")
    if candidate.release_sequence == last_sequence \
            and last_commit is not None and candidate.commit != last_commit:
        raise ReleaseContractError("release sequence conflict changes the commit")
    if candidate.release_sequence == last_sequence \
            and last_manifest_sha256 is not None \
            and manifest_sha256(candidate) != last_manifest_sha256:
        raise ReleaseContractError("release sequence conflict changes the manifest")
    return verified


def require_matching_release_artifacts(
    left_manifest: ReleaseManifest,
    left_signature: ReleaseSignature,
    right_manifest: ReleaseManifest,
    right_signature: ReleaseSignature,
) -> None:
    if not isinstance(left_signature, ReleaseSignature) \
            or not isinstance(right_signature, ReleaseSignature):
        raise ReleaseContractError("release signatures must be parsed before mirror comparison")
    left = (
        canonical_manifest_bytes(left_manifest),
        asdict(parse_release_signature(asdict(left_signature))),
    )
    right = (
        canonical_manifest_bytes(right_manifest),
        asdict(parse_release_signature(asdict(right_signature))),
    )
    if left != right:
        raise ReleaseContractError("release mirror artifacts do not match")


def python_is_compatible(
    manifest: ReleaseManifest, version_info: Tuple[int, int, int]
) -> bool:
    match = _PYTHON_RE.fullmatch(manifest.min_python)
    if match is None \
            or not isinstance(version_info, tuple) \
            or len(version_info) < 2 \
            or type(version_info[0]) is not int \
            or type(version_info[1]) is not int:
        return False
    return tuple(version_info[:2]) >= (int(match.group(1)), int(match.group(2)))
