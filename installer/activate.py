"""Device activation client for the Decision Engine public shell.

Exchanges the owner-issued **API key** (staged by the installer as
``api_key`` in the per-device ``config.json``) for a per-device access token
via ``POST /v1/devices/activate``, then writes the returned
``access_token`` + ``device_id`` back into that same ``config.json`` so the shim
can authenticate.

Transport-only, like the rest of the shell: no server IP or secret is baked in.
The endpoint and API key come only from the local config the installer
wrote. The API key IS a secret (the server hashes it as ``activation_secret``),
so — exactly like the shim's device-token rule — this client refuses to send it
over a plaintext ``http://`` endpoint (localhost excepted for dev), fail closed.
It also refuses to follow ANY redirect (the shared ``client.http_safety.NoRedirect``,
as used by the shim's artifact fetch and the popup's board fetch), so only the
configured hub can answer an activation. On success the one-time API key is
dropped from the config so a reusable secret does not linger next to the issued
tokens.

Usage (CLI):

    python3 -m installer.activate            # activate this device
    python3 -m installer.activate --force    # re-bind (consumes a device slot)
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from http.client import HTTPResponse
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from client import i18n
from client import runner as client_runner
from client.http_safety import NoRedirect  # shared refuse-every-redirect guard (stdlib leaf)

from .config import (
    ACTIVATION_RECOVERY_RELATIVE_PATH,
    ShellError,
    atomic_write_json,
    de_config_path,
    device_fingerprint,
    device_name_default,
    is_local_host,
    load_json,
    normalize_endpoint,
)
from client.version import CLIENT_VERSION  # SOT leaf module (no runtime pull); the version the device reports

ACTIVATE_PATH = "/v1/devices/activate"
CLIENT_USER_AGENT = "decision-engine-shell/0.1.0"
DEFAULT_TIMEOUT_S = 60
# Activation responses are tiny (a few tokens). Cap the read so a misconfigured
# or hostile server cannot exhaust client memory with an unbounded stream.
_MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB


class ActivationPersistenceError(ShellError):
    """The service issued credentials but the local durable write failed."""


class ActivationRefusedError(ShellError):
    """A typed HTTP rejection so trusted callers can render a safe local reason.

    ``reason`` remains available to the low-level activation CLI for backwards
    compatibility, but GUI callers must classify by ``status_code`` and never
    render this server-controlled text.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(
            "activation refused (HTTP %s): %s" % (status_code, reason)
        )


RECOVERY_REQUIRED_EXIT_CODE = 3


class ActivationRecoveryRequiredError(ShellError):
    """A prior remote activation may have succeeded; blind retry is unsafe."""


def activation_recovery_marker_path(config_path: Path) -> Path:
    return config_path.parent / ACTIVATION_RECOVERY_RELATIVE_PATH


def clear_activation_recovery_marker(config_path: Path, *, retry_must_be_safe: bool) -> None:
    marker = activation_recovery_marker_path(config_path)
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError:
        if retry_must_be_safe:
            raise ActivationRecoveryRequiredError(
                "device activation failed and its recovery marker could not be cleared; "
                "do not retry automatically"
            ) from None


def activate_with_recovery_guard(
    endpoint: str,
    activation_secret: str,
    config_path: Optional[Path] = None,
    *,
    force: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    allow_plaintext_local: bool = False,
) -> Dict[str, Any]:
    """Serialize activation and durably block replay after uncertain persistence."""
    path = config_path or de_config_path()
    marker = activation_recovery_marker_path(path)
    with client_runner.config_activation_lock(path):
        current = load_json(path)
        if is_permanently_activated(current):
            clear_activation_recovery_marker(path, retry_must_be_safe=False)
            return activate_with_credentials(
                endpoint,
                activation_secret,
                config_path=path,
                force=force,
                timeout_s=timeout_s,
                allow_plaintext_local=allow_plaintext_local,
            )
        if marker.exists():
            raise ActivationRecoveryRequiredError(
                "a previous device activation may have reached the service; "
                "do not retry automatically"
            )
        try:
            atomic_write_json(marker, {"schema": 1, "state": "recovery_required"})
        except OSError:
            raise ShellError(
                "device activation did not start because its recovery marker could not be saved"
            ) from None
        try:
            summary = activate_with_credentials(
                endpoint,
                activation_secret,
                config_path=path,
                force=force,
                timeout_s=timeout_s,
                allow_plaintext_local=allow_plaintext_local,
            )
        except ActivationPersistenceError:
            raise
        except Exception:  # aqg: top-level boundary — every ordinary failure clears the replay guard
            clear_activation_recovery_marker(path, retry_must_be_safe=True)
            raise
        clear_activation_recovery_marker(path, retry_must_be_safe=False)
        return summary


def _https_context() -> ssl.SSLContext:
    """Mirror the shim's CA resolution: explicit override, else the platform store, with certifi
    ONLY as a last-resort rescue when the platform ships no usable store.

    Kept byte-for-byte equivalent to `shim._https_context`; see that docstring for why certifi is
    a fallback rather than a supplement (it must never re-trust a root the OS distrusts) and why
    the three copies are duplicated on purpose.
    """
    import os

    ca_file = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    ca_dir = os.getenv("SSL_CERT_DIR")
    if ca_file or ca_dir:
        # An explicit override is authoritative AND exclusive — folding the OS store in on
        # top of a user-pinned CA would widen trust behind their back. SSL_CERT_DIR belongs
        # here with SSL_CERT_FILE: OpenSSL honors it when building the default context, so
        # leaving it out would let a pinned directory be judged by the platform probe below.
        # Consuming all three here means that probe runs ONLY when no override is set, where
        # it and OpenSSL's loader read the same compiled-in defaults and cannot disagree.
        return ssl.create_default_context(cafile=ca_file or None, capath=ca_dir or None)
    context = ssl.create_default_context()
    # Does the platform's own store actually hold anchors? Windows enumerates it eagerly, so
    # get_ca_certs() is truthful there. POSIX loads default paths lazily (get_ca_certs() is
    # empty even with a full /etc/ssl), so inspect the paths OpenSSL would use — but inspect
    # their CONTENT, not merely their existence: libssl creates /etc/ssl/certs, ca-certificates
    # fills it, so on a stripped image (alpine without ca-certificates, distroless) the
    # directory exists and is empty. Scoring that "usable" would skip the rescue and leave TLS
    # failing closed on exactly the host the rescue exists for.
    if sys.platform == "win32":
        platform_store_usable = bool(context.get_ca_certs())
    else:
        # get_default_verify_paths() already returns None unless the file/dir exists, and it
        # shadows SSL_CERT_FILE / SSL_CERT_DIR — both consumed above — so this sees only the
        # compiled-in defaults.
        paths = ssl.get_default_verify_paths()
        try:
            platform_store_usable = bool(
                (paths.cafile and os.path.getsize(paths.cafile) > 0)
                or (paths.capath and os.listdir(paths.capath))
            )
        except OSError:
            # Unreadable default path — treat as unusable and let the rescue supply anchors.
            # Safe in this direction only because the rescue below builds a FRESH certifi-only
            # context, so a wrong guess here cannot union certifi onto a populated store.
            platform_store_usable = False
    if platform_store_usable:
        # The OS store governs alone — an OS/enterprise CA distrust is honored, not re-widened.
        return context
    try:
        import certifi  # type: ignore

        # No usable OS store: certifi is the ONLY anchor source. Build a fresh certifi-only
        # context rather than loading certifi INTO `context` — create_default_context(cafile=...)
        # skips load_default_certs entirely, so this is structurally incapable of layering
        # certifi on top of a platform store, whatever the probe above concluded.
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # aqg: top-level boundary — the rescue is best-effort. If certifi is
        # absent/unreadable we fall back to `context`, which on a store-less host has no
        # anchors, so TLS fails CLOSED (CERTIFICATE_VERIFY_FAILED), never open; verify_mode /
        # check_hostname keep their secure defaults. Surface it so this is attributable.
        print("de-activate: certifi trust-store rescue unavailable; TLS may fail closed", file=sys.stderr)
    return context


def _opener(context: Optional[ssl.SSLContext]):
    handlers: list = [NoRedirect()]
    if context is not None:
        handlers.append(HTTPSHandler(context=context))
    return build_opener(*handlers)


def _read_capped(resp: HTTPResponse) -> str:
    return resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")


def _post_activate(
    endpoint: str, body: Dict[str, Any], *, timeout_s: int
) -> Dict[str, Any]:
    """POST the activation request; return the parsed, contract-checked 201 body.

    Fail closed: a 4xx/5xx (invalid / expired / device-limit), a non-JSON body, a
    non-dict body, or a success payload missing the contract's ``access_token`` /
    ``device_id`` all raise ShellError so no token is ever written. Error messages
    never echo the raw response body (it can carry other credential fields) — only
    the observed key set, so the public shell keeps tokens off its leak surface.
    """
    data = json.dumps(body).encode("utf-8")
    request = Request(
        endpoint + ACTIVATE_PATH,
        data=data,
        headers={
            "Accept": "application/json",
            "Accept-Language": i18n.accept_language(),
            "Content-Type": "application/json",
            "User-Agent": CLIENT_USER_AGENT,
        },
        method="POST",
    )
    opener = _opener(_https_context())
    try:
        with opener.open(request, timeout=timeout_s) as resp:
            raw = _read_capped(resp)
    except HTTPError as exc:
        try:
            detail = exc.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        finally:
            exc.close()
        reason = detail
        try:
            parsed_err = json.loads(detail)
            if isinstance(parsed_err, dict):
                reason = parsed_err.get("error") or detail
        except json.JSONDecodeError:
            pass
        raise ActivationRefusedError(exc.code, str(reason or exc.reason))
    except TimeoutError:
        raise ShellError("activation request timed out after %ss" % timeout_s)
    except URLError as exc:
        raise ShellError("activation request failed: %s" % exc.reason)
    except OSError as exc:
        raise ShellError("activation request failed: %s" % exc)

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ShellError("invalid JSON from %s: %s" % (ACTIVATE_PATH, exc))
    if not isinstance(parsed, dict):
        raise ShellError("activation response malformed (expected JSON object, got %s)" % type(parsed).__name__)
    if not parsed.get("access_token") or not parsed.get("device_id"):
        # Never interpolate the body — it may carry a refresh_token/other secret.
        raise ShellError("activation response malformed (keys: %s)" % sorted(parsed.keys()))
    return parsed


def validate_activation_endpoint(
    endpoint: str, *, allow_plaintext_local: bool = True
) -> str:
    """Validate the credential destination before an activation secret is sent.

    The legacy activation CLI keeps its localhost-only HTTP exception for
    development and hermetic tests. Human-entered permanent setup passes
    ``allow_plaintext_local=False`` and is HTTPS-only.
    """
    normalized = normalize_endpoint(endpoint)
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ShellError("server endpoint port is invalid") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ShellError(
            "server endpoint must be a valid URL without credentials, a query, or a fragment"
        )
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ShellError("server endpoint must use https://")
    if scheme == "http" and (
        not allow_plaintext_local or not is_local_host(normalized)
    ):
        raise ShellError(
            "refusing to send the API key over plaintext http:// endpoint %s — use https://"
            % normalized
        )
    return normalized


def _validate_activation_endpoint(endpoint: str) -> str:
    """Backward-compatible internal alias for the legacy localhost policy."""
    return validate_activation_endpoint(endpoint, allow_plaintext_local=True)


def is_permanently_activated(config: Dict[str, Any]) -> bool:
    """Return whether a complete local device binding is present."""
    return all(
        isinstance(config.get(key), str) and bool(config[key].strip())
        for key in ("server_endpoint", "device_id", "access_token")
    )


def scrub_staged_invitation(path: Path) -> bool:
    """Remove a residual legacy ``api_key`` from a complete device binding."""
    current = load_json(path)
    if not is_permanently_activated(current) or "api_key" not in current:
        return False
    current.pop("api_key", None)
    atomic_write_json(path, current)
    return True


def _activate_config(
    config: Dict[str, Any],
    path: Path,
    *,
    endpoint: str,
    api_key: str,
    force: bool,
    timeout_s: int,
) -> Dict[str, Any]:
    """Exchange one in-memory invite secret and persist only device credentials."""
    if is_permanently_activated(config) and not force:
        existing_endpoint = normalize_endpoint(str(config.get("server_endpoint") or ""))
        if existing_endpoint != endpoint:
            raise ShellError(
                "device is already activated for a different owner endpoint; "
                "re-binding requires an explicit owner-guided recovery flow"
            )
        if "api_key" in config:
            config.pop("api_key", None)
            atomic_write_json(path, config)
        return {
            "activated": False,
            "already_activated": True,
            "reason": "already activated (device_id=%s) — pass --force to re-bind"
            % (config.get("device_id") or "?"),
            "config": str(path),
        }

    if not isinstance(api_key, str) or not api_key.strip():
        raise ShellError("owner-issued activation key is required")

    body = {
        "device_name": config.get("device_name") or device_name_default(),
        "device_fingerprint": config.get("device_fingerprint") or device_fingerprint(),
        "activation_secret": api_key,
        # Report the RUNNING client's version, NOT a config value: the version is an intrinsic property
        # of the installed code, so a stale/hand-edited config can never make a 0.2.0 client advertise
        # < 0.2.0 and leave the server's version-gated GE byte-isolation dormant (audit 988bd8e1 4/4).
        "client_version": CLIENT_VERSION,
    }
    result = _post_activate(endpoint, body, timeout_s=timeout_s)

    # Apply the new binding wholesale — never keep a stale device_id/refresh_token
    # from a prior activation, which would leave a mismatched credential pair.
    config["server_endpoint"] = endpoint
    config["access_token"] = result["access_token"]
    config["device_id"] = result["device_id"]
    if result.get("refresh_token"):
        config["refresh_token"] = result["refresh_token"]
    else:
        config.pop("refresh_token", None)
    # The popup path never stages api_key. The legacy installer path may have;
    # either way, remove the activation key once permanent credentials exist.
    config.pop("api_key", None)
    try:
        atomic_write_json(path, config)
    except OSError as exc:
        raise ActivationPersistenceError(
            "activation credentials were issued but could not be saved locally"
        ) from exc
    return {
        "activated": True,
        "device_id": config["device_id"],
        "config": str(path),
    }


def activate(
    config_path: Optional[Path] = None,
    *,
    force: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Activate this device against the configured server; persist the token.

    Reads endpoint + api_key + device identity from ``config.json``, POSTs the
    activation, and merges ``access_token`` / ``device_id`` / ``refresh_token`` back
    into the same file (0600), dropping the now-spent API key. Returns a small
    summary dict. Idempotency: a complete endpoint/device/token binding is a no-op
    unless ``force``; re-activating mints a *new* device binding and consumes a
    slot against the account's max-devices cap.
    """
    path = config_path or de_config_path()
    config = load_json(path)

    endpoint_raw = config.get("server_endpoint") or ""
    if not endpoint_raw:
        raise ShellError(
            "no server_endpoint in %s — run the installer with --server-endpoint first" % path
        )
    endpoint = validate_activation_endpoint(endpoint_raw, allow_plaintext_local=True)

    api_key = config.get("api_key") or ""

    return _activate_config(
        config,
        path,
        endpoint=endpoint,
        api_key=api_key,
        force=force,
        timeout_s=timeout_s,
    )


def activate_with_credentials(
    endpoint: str,
    activation_secret: str,
    config_path: Optional[Path] = None,
    *,
    force: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    allow_plaintext_local: bool = False,
) -> Dict[str, Any]:
    """Permanently activate from in-memory credentials without staging the secret.

    This is the in-memory onboarding path used by the masked GUI and the
    installer environment reader. It never stages the activation secret in
    argv or ``config.json``. Only the normalized endpoint and server-issued
    device credentials are persisted after the server accepts the activation.
    """
    path = config_path or de_config_path()
    current = load_json(path)
    normalized = validate_activation_endpoint(
        endpoint, allow_plaintext_local=allow_plaintext_local
    )
    return _activate_config(
        current,
        path,
        endpoint=normalized,
        api_key=activation_secret,
        force=force,
        timeout_s=timeout_s,
    )


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="de-activate",
        description="Activate this device against the Decision Engine server (API key → token)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-activate even if a token exists (mints a NEW device binding, uses a slot)",
    )
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="read DE_ENDPOINT and DE_ACTIVATION_SECRET without putting either in argv",
    )
    args = parser.parse_args(argv)

    endpoint = ""
    activation_secret = ""
    if args.from_env:
        endpoint = os.environ.pop("DE_ENDPOINT", "").strip()
        activation_secret = os.environ.pop("DE_ACTIVATION_SECRET", "")
    try:
        if args.from_env:
            if not endpoint or not activation_secret.strip():
                raise ShellError(
                    "DE_ENDPOINT and DE_ACTIVATION_SECRET are both required for environment activation"
                )
            summary = activate_with_recovery_guard(
                endpoint,
                activation_secret,
                force=args.force,
                timeout_s=args.timeout_s,
                allow_plaintext_local=True,
            )
        else:
            summary = activate(force=args.force, timeout_s=args.timeout_s)
    except ActivationPersistenceError:
        print(
            "de-activate: the service may already have accepted this device, but local "
            "credentials could not be saved; do not retry automatically",
            file=sys.stderr,
        )
        return RECOVERY_REQUIRED_EXIT_CODE
    except ActivationRecoveryRequiredError:
        print(
            "de-activate: a previous activation may have reached the service; "
            "do not retry automatically — use owner-guided recovery",
            file=sys.stderr,
        )
        return RECOVERY_REQUIRED_EXIT_CODE
    except ShellError as exc:
        if args.from_env:
            print(
                "de-activate: device activation failed; verify the owner-issued values and network",
                file=sys.stderr,
            )
        else:
            print("de-activate: %s" % exc, file=sys.stderr)
        return 1
    finally:
        activation_secret = ""

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif summary.get("activated"):
        print("Device activated (device_id=%s)." % summary.get("device_id"))
        print("  config: %s" % summary.get("config"))
        print("  next: wire the shim into your agent (see client README 'Use').")
    else:
        print("de-activate: %s" % summary.get("reason"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
