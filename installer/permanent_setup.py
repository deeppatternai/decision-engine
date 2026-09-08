"""Agent-launched permanent Decision Engine setup for macOS and Windows.

The default CLI opens one native desktop form, preferring pywebview with a
tkinter fallback. The endpoint is visible; the owner-issued activation secret
is masked. ``--from-env`` is the
non-interactive installation path: it reads ``DE_ENDPOINT`` and
``DE_ACTIVATION_SECRET`` from the current process without copying either value
into argv. Both paths pass the secret directly in memory to
:func:`installer.activate.activate_with_recovery_guard`; it is never persisted.

Successful activation persists the endpoint plus server-issued device/access/
refresh credentials. Subsequent Agent and machine restarts therefore require
no repeated input. The activation key itself is deliberately not retained.

Usage:

    python3 -m installer.permanent_setup
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from . import activate, doctor, install, managed_install, mcp_config
from .config import (
    ShellError,
    de_config_path,
    load_json,
    managed_component_root,
)


Credentials = Tuple[str, str]
CredentialPrompt = Callable[[], Optional[Credentials]]
CredentialSubmitHandler = Callable[[str, str], object]
RECOVERY_REQUIRED_EXIT_CODE = activate.RECOVERY_REQUIRED_EXIT_CODE

_MAX_ENDPOINT_INPUT = 2048
_MAX_SECRET_INPUT = 4096
# The masked-credential form, ONE per backend. Visible copy and the page-side JS strings are baked in
# by `_render_credential_form_html` from the resolved locale's PERMANENT_SETUP table; the `__DE_*__`
# sentinels are its only substitution points. The example endpoint URL and the element ids/JS control
# flow are language-independent and stay literal here (a parity/behavior test pins that flow). Strings
# are injected via .replace() (not %-format or .format()) so the CSS `{}` / `%` need no escaping — the
# same idiom client/popup/launcher.py uses for its popups.
_CREDENTIAL_FORM_TEMPLATE = r"""<!doctype html>
<html lang="__DE_LANG__"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;background:#f4f6fa;color:#172b4d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}html{overflow:hidden}body{overflow-y:auto}
.header{height:104px;padding:22px 30px;background:#132238;color:#fff}.title{font-size:24px;font-weight:750;letter-spacing:-.3px}.subtitle{margin-top:6px;color:#b9c5d6;font-size:13px}
.wrap{padding:20px 24px 24px}.card{padding:22px 24px 20px;background:#fff;border:1px solid #e2e7ef;border-radius:12px;box-shadow:0 10px 28px rgba(19,34,56,.08)}
label{display:block;margin-bottom:7px;font-size:13px;font-weight:650;color:#26364a}input{width:100%;height:42px;padding:0 12px;border:1px solid #c9d2e0;border-radius:8px;color:#172b4d;background:#fff;font-size:14px;outline:none}
input:focus{border-color:#2f6feb;box-shadow:0 0 0 3px rgba(47,111,235,.13)}.hint{margin:6px 0 15px;color:#718096;font-size:11px}.error{min-height:18px;color:#b42318;font-size:12px}.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:10px}
button{min-width:96px;height:38px;padding:0 18px;border-radius:8px;border:1px solid #cbd5e1;font-size:13px;font-weight:650;cursor:pointer}.cancel{color:#344054;background:#fff}.primary{color:#fff;background:#2f6feb;border-color:#2f6feb}.primary:hover{background:#2459bd}
</style></head><body><header class="header"><div class="title">__DE_TITLE__</div><div class="subtitle">__DE_SUBTITLE__</div></header>
<main class="wrap"><form class="card" id="setup-form"><label for="endpoint">__DE_ENDPOINT_LABEL__</label>
<input id="endpoint" required autocomplete="url" maxlength="2048" placeholder="https://engine.example.com" autofocus><div class="hint">__DE_ENDPOINT_HINT__</div>
<label for="secret">__DE_SECRET_LABEL__</label><input id="secret" type="password" required autocomplete="off" maxlength="4096" spellcheck="false" placeholder="__DE_SECRET_PLACEHOLDER__">
<div class="hint">__DE_SECRET_HINT__</div><div class="error" id="form-error" role="alert" aria-live="polite"></div><div class="actions"><button type="button" class="cancel" id="cancel">__DE_CANCEL__</button><button type="submit" class="primary" id="activate">__DE_ACTIVATE__</button></div></form></main>
<script>const S=__DE_SETUP_STRINGS__;const endpoint=document.getElementById('endpoint'),secret=document.getElementById('secret'),form=document.getElementById('setup-form'),cancel=document.getElementById('cancel'),activate=document.getElementById('activate'),error=document.getElementById('form-error');let submitting=false;
function setBusy(busy){submitting=busy;endpoint.disabled=busy;secret.disabled=busy;cancel.disabled=busy;activate.disabled=busy;activate.textContent=busy?S.activating:S.activate}
form.addEventListener('submit',async e=>{e.preventDefault();if(submitting)return;error.textContent='';setBusy(true);try{const result=await window.pywebview.api.submit(endpoint.value,secret.value);if(result&&result.ok===false){setBusy(false);error.textContent=result.error||S.generic;if(result.field==='secret')secret.focus();else endpoint.focus()}}catch(_bridgeError){setBusy(false);error.textContent=S.bridge_error}});[endpoint,secret].forEach(input=>input.addEventListener('input',()=>{error.textContent=''}));cancel.addEventListener('click',async()=>{if(!submitting)await window.pywebview.api.cancel()});
document.addEventListener('keydown',async e=>{if(e.key==='Escape'&&!submitting)await window.pywebview.api.cancel()});</script></body></html>"""


def _setup_strings(locale: Optional[str] = None) -> dict:
    """The PERMANENT_SETUP table for a resolved locale (defaults to the resolver's chosen language).

    One decision point for the form's language: pass ``None`` to follow ``i18n.resolve_locale``
    (explicit → $DE_UI_LOCALE → system → en-US), or a pre-resolved tag when a caller already resolved
    it. Direct/unit callers that pass nothing on a machine with no locale env fall back to en-US, so
    the shared validators keep their historical English text unless a caller wires in another language.
    """
    from client import i18n

    return i18n.permanent_setup(locale or i18n.resolve_locale())


_SENTINEL_RE = re.compile(r"__DE_[A-Z_]+__")


def _render_credential_form_html(strings: dict) -> str:
    """Bake one locale's PERMANENT_SETUP strings into the masked-credential form.

    Substitution is a SINGLE pass over the template (``re.sub`` with a callback): each ``__DE_*__``
    sentinel is replaced exactly once and the replacement text is never rescanned, so a value that
    itself contains a sentinel is inserted verbatim as data — it can never re-enter substitution and
    splice one slot's payload into another slot's context. Visible copy is ``html.escape``-d for its
    text/quoted-attribute slot; the page-side JS bundle is the only slot rendered as a JSON literal
    (with ``<`` / ``>`` / ``&`` / U+2028 / U+2029 escaped so it cannot terminate the ``<script>`` or
    open a tag) and it is the only sentinel inside ``<script>``. The ``lang`` slot carries a bare
    BCP-47 subtag. Every value is a self-authored locale constant, never user input or server output;
    the escaping + single-pass containment is defense in depth against a future edited translation —
    the same discipline ``client/popup/launcher.py`` applies. An unknown sentinel is left intact so a
    template/table drift surfaces as a visible ``__DE_*__`` token (the render test asserts none survive).
    """
    errors = strings["errors"]
    js_strings = {
        "activate": strings["activate"],
        "activating": strings["activating"],
        "generic": errors["generic"],
        "bridge_error": errors["bridge_error"],
    }
    literal = json.dumps(js_strings, ensure_ascii=True)
    literal = (
        literal.replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )
    # sentinel → already-final replacement text. The JS bundle ships its escaped JSON literal as-is;
    # every visible slot is html.escape-d (quote=True covers the one quoted-attribute slot). Built
    # once and consumed by a single re.sub, so no replacement is ever fed back through substitution.
    replacements = {
        "__DE_SETUP_STRINGS__": literal,
        "__DE_LANG__": html.escape(strings["html_lang"], quote=True),
        "__DE_TITLE__": html.escape(strings["title"], quote=True),
        "__DE_SUBTITLE__": html.escape(strings["subtitle"], quote=True),
        "__DE_ENDPOINT_LABEL__": html.escape(strings["endpoint_label"], quote=True),
        "__DE_ENDPOINT_HINT__": html.escape(strings["endpoint_hint"], quote=True),
        "__DE_SECRET_LABEL__": html.escape(strings["secret_label"], quote=True),
        "__DE_SECRET_HINT__": html.escape(strings["secret_hint"], quote=True),
        "__DE_SECRET_PLACEHOLDER__": html.escape(strings["secret_placeholder"], quote=True),
        "__DE_CANCEL__": html.escape(strings["cancel"], quote=True),
        "__DE_ACTIVATE__": html.escape(strings["activate"], quote=True),
    }
    return _SENTINEL_RE.sub(
        lambda m: replacements.get(m.group(0), m.group(0)), _CREDENTIAL_FORM_TEMPLATE
    )


ActivationRecoveryRequiredError = activate.ActivationRecoveryRequiredError

_ACTIVATION_RECOVERY_MESSAGE = (
    "the service may already have accepted this device, but its credentials "
    "could not be saved locally; do not retry automatically — preserve the "
    "installation and use the owner-guided recovery flow"
)


class PermanentSetupError(ShellError):
    """A predictable top-level setup failure with a locale-stable GUI message code."""

    def __init__(self, code: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.code = code


def _localized_setup_failure_dialog(
    exc: Optional[BaseException] = None,
) -> Tuple[str, str]:
    strings = _setup_strings()
    code = exc.code if isinstance(exc, PermanentSetupError) else "unexpected_setup_failure"
    return (
        strings["setup_failed_title"],
        strings.get(code, strings["unexpected_setup_failure"]),
    )


def _localized_setup_recovery_dialog() -> Tuple[str, str]:
    strings = _setup_strings()
    return strings["setup_recovery_title"], strings["activation_recovery_required"]


def _localized_setup_success_dialog(result: PermanentSetupResult) -> Tuple[str, str]:
    strings = _setup_strings()
    if result.host_failures:
        failed_hosts = ", ".join(client for client, _reason in result.host_failures)
        message = strings["success_with_host_failures"] % {"failed_hosts": failed_hosts}
    elif result.already_activated:
        message = strings["already_activated_success"]
    else:
        message = strings["setup_succeeded"]
    return strings["setup_success_title"], message


class _CredentialFormValidationError(ShellError):
    """Safe field-specific feedback for the local credential form."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _validate_form_credentials(
    endpoint: str, secret: str, errors: Optional[dict] = None
) -> Credentials:
    # `errors` is the resolved locale's PERMANENT_SETUP["errors"] table; the form layers pass their
    # resolved one so the feedback language matches the visible form. Default to en-US so direct/unit
    # callers keep the historical English text.
    errors = errors if errors is not None else _setup_strings("en-US")["errors"]
    if not isinstance(endpoint, str):
        raise _CredentialFormValidationError("endpoint", errors["endpoint_invalid"])
    if len(endpoint) > _MAX_ENDPOINT_INPUT:
        raise _CredentialFormValidationError("endpoint", errors["endpoint_too_long"])
    if not endpoint.strip():
        raise _CredentialFormValidationError("endpoint", errors["endpoint_required"])
    try:
        normalized_endpoint = _validate_endpoint(endpoint)
    except ShellError:
        raise _CredentialFormValidationError(
            "endpoint", errors["endpoint_invalid"]
        ) from None
    if not isinstance(secret, str):
        raise _CredentialFormValidationError("secret", errors["secret_invalid"])
    if len(secret) > _MAX_SECRET_INPUT:
        raise _CredentialFormValidationError("secret", errors["secret_too_long"])
    if not secret.strip():
        raise _CredentialFormValidationError("secret", errors["secret_required"])
    # Preserve the owner-issued key byte-for-byte; the activation service owns
    # key semantics, while the local form only rejects an all-whitespace value.
    return normalized_endpoint, secret


def _activation_refusal_feedback(
    exc: activate.ActivationRefusedError, errors: Optional[dict] = None
) -> _CredentialFormValidationError:
    """Map an untrusted HTTP refusal to fixed text safe for the credential page.

    ``errors`` is the resolved locale's PERMANENT_SETUP["errors"] table (en-US by default); only the
    fixed, self-authored slug is chosen from the status code — the server's body is never surfaced.
    """
    errors = errors if errors is not None else _setup_strings("en-US")["errors"]
    if exc.status_code in (401, 403):
        return _CredentialFormValidationError("secret", errors["secret_rejected"])
    if exc.status_code == 429:
        return _CredentialFormValidationError("secret", errors["rate_limited"])
    if 400 <= exc.status_code < 500:
        return _CredentialFormValidationError("endpoint", errors["rejected"])
    return _CredentialFormValidationError("endpoint", errors["service_unavailable"])


def _activate_once(
    endpoint: str,
    activation_secret: str,
    *,
    config_path: Path,
):
    """Run the guarded activation with one recovery contract for every input path."""
    try:
        return activate.activate_with_recovery_guard(
            endpoint,
            activation_secret,
            config_path=config_path,
        )
    except activate.ActivationPersistenceError:
        raise ActivationRecoveryRequiredError(_ACTIVATION_RECOVERY_MESSAGE) from None
    except activate.ActivationRecoveryRequiredError:
        raise
    finally:
        # Best-effort removal of our local reference. The caller also drops its reference;
        # Python strings cannot be reliably zeroed in place.
        activation_secret = ""


def _activate_from_form(
    endpoint: str,
    activation_secret: str,
    *,
    config_path: Path,
    errors: Optional[dict] = None,
):
    """Activate inside the still-live form and expose only fixed retryable feedback.

    ``errors`` is the resolved locale's PERMANENT_SETUP["errors"] table (en-US by default) so every
    retry message matches the visible form's language.
    """
    errors = errors if errors is not None else _setup_strings("en-US")["errors"]
    try:
        summary = _activate_once(
            endpoint,
            activation_secret,
            config_path=config_path,
        )
    except activate.ActivationRecoveryRequiredError:
        raise
    except activate.ActivationRefusedError as exc:
        raise _activation_refusal_feedback(exc, errors) from None
    except Exception:  # aqg: top-level boundary — never render server-controlled credential details
        raise _CredentialFormValidationError(
            "endpoint", errors["activation_failed"]
        ) from None
    finally:
        activation_secret = ""
    if not summary.get("activated") and not summary.get("already_activated"):
        raise _CredentialFormValidationError(
            "endpoint", errors["activation_incomplete"]
        )
    return summary


@dataclass(frozen=True)
class PermanentSetupResult:
    permanent: bool
    already_activated: bool = False
    cancelled: bool = False
    credentials_missing: bool = False
    host_failures: Tuple[Tuple[str, str], ...] = ()
    post_mcp_write_notices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class HostConfigurationResult:
    failures: Tuple[Tuple[str, str], ...] = ()
    notices: Tuple[str, ...] = ()


def _validate_endpoint(value: str) -> str:
    return activate.validate_activation_endpoint(
        value, allow_plaintext_local=False
    )


def _managed_config_path() -> Path:
    if "DE_CONFIG_PATH" in os.environ:
        raise ShellError(
            "permanent setup refuses DE_CONFIG_PATH; unset it and run setup from "
            "the fixed managed installation"
        )
    if "DEEPPATTERN_HOME" in os.environ:
        raise ShellError(
            "permanent setup refuses DEEPPATTERN_HOME; managed device credentials "
            "belong to the fixed ~/.deeppattern/decision-engine installation"
        )
    expected = managed_component_root("decision-engine") / "config.json"
    configured = de_config_path()
    if os.path.normcase(os.path.abspath(str(configured))) != os.path.normcase(
        os.path.abspath(str(expected))
    ):
        raise ShellError(
            "permanent setup refuses a non-managed config path; run it for the "
            "fixed managed installation"
        )
    try:
        root = managed_install.canonical_managed_root(expected.parent)
        if managed_install._is_link_like(root / "config.json"):
            raise managed_install.ManagedInstallError(
                "managed config must not be a symlink or reparse point"
            )
    except managed_install.ManagedInstallError as exc:
        raise ShellError(str(exc)) from None
    return root / "config.json"


def _load_managed_config(path: Path):
    try:
        return load_json(path)
    except Exception:  # aqg: top-level boundary — never expose config contents/parse details
        raise ShellError(
            "managed device config is unreadable; preserve it and use the managed "
            "repair/support flow instead of overwriting it"
        ) from None


def _configure_agent_hosts(
    config_path: Path, clients: Optional[Sequence[str]] = None,
) -> HostConfigurationResult:
    """Wire MCP and shared skill links with per-host failure isolation."""
    targets = tuple(clients if clients is not None else mcp_config.detect_clients())
    if not targets:
        raise ShellError(
            "device credentials are saved, but no supported Agent client was detected"
        )
    if any(client not in mcp_config.CLIENTS for client in targets):
        raise ShellError("device credentials are saved, but an unsupported Agent client was selected")
    if len(set(targets)) != len(targets):
        raise ShellError("device credentials are saved, but the Agent client list has duplicates")
    failures = []
    blocked_clients = set()
    if "cursor" in targets:
        migration_failure = install.retire_legacy_cursor_owned_copy(
            config_path.parent
        )
        if migration_failure is not None:
            failures.append(("cursor", migration_failure))
            blocked_clients.add("cursor")
    writable_targets = [client for client in targets if client not in blocked_clients]
    if not writable_targets:
        raise ShellError(
            "device credentials are saved, but no Agent MCP entry could be wired"
        )
    wiring = mcp_config.write_entries(writable_targets)
    if not wiring.written:
        raise ShellError(
            "device credentials are saved, but no Agent MCP entry could be wired"
        )
    failures.extend(wiring.failed)
    blocked_routes = set(blocked_clients)
    for client, _reason in wiring.failed:
        spec = mcp_config.CLIENT_SPECS.get(client)
        blocked_routes.add(
            spec.skill_route_name if spec and spec.skill_route_name else client
        )
    try:
        skill_routes = install.repair_detected_skill_routes(
            config_path.parent,
            excluded_clients=frozenset(blocked_routes),
        )
    except (ShellError, OSError) as exc:
        raise ShellError(
            "device credentials and MCP wiring are saved, but shared skill-link "
            "repair could not run; retry permanent setup"
        ) from exc
    if skill_routes.failed and not skill_routes.routed:
        raise ShellError(
            "device credentials and MCP wiring are saved, but no detected Agent "
            "skill route could be wired; existing user-managed entries were preserved"
        )
    failures.extend(
        (client, "skill routing: %s" % reason)
        for client, reason in skill_routes.failed.items()
    )
    deduplicated = {}
    for client, reason in failures:
        deduplicated.setdefault(client, reason)
    return HostConfigurationResult(tuple(deduplicated.items()), wiring.notices)


def _doctor_has_blocking_failure(
    host_failures: Tuple[Tuple[str, str], ...],
) -> bool:
    """Keep core Doctor failures blocking while isolating known host routes."""
    if not host_failures:
        return doctor.main([]) != 0
    results = doctor.run_all()
    rendered = "Decision Engine doctor:\n" + doctor._render(results)
    output_encoding = sys.stdout.encoding or "utf-8"
    print(
        rendered.encode(output_encoding, errors="replace").decode(
            output_encoding, errors="replace"
        )
    )
    if any(
        result.status == "FAIL" and result.name != "skills"
        for result in results
    ):
        return True
    failed_routes = set()
    for client, _reason in host_failures:
        spec = mcp_config.CLIENT_SPECS.get(client)
        route_name = spec.skill_route_name if spec is not None else None
        failed_routes.add(route_name or client)
    try:
        broken_routes = set(doctor.skill_route_failures())
    except Exception:  # aqg: top-level boundary -- unreadable routes remain blocking
        return True
    return bool(broken_routes - failed_routes)


def _gui_session_blocked() -> Optional[str]:
    """Why opening a Tk window here would be unsafe, or ``None`` to go ahead.

    Measured, not inferred: in an agent's seatbelt shell — the same session where
    ``sandbox_check(getpid(), NULL, 0)`` returns 1 — ``python3 -c "import tkinter;
    tkinter.Tk()"`` exits 134 (128 + SIGABRT). Whether Tk aborts through the same
    ``_RegisterApplication`` path as pywebview's cocoa backend is NOT established here; what
    is established is that Tk creation kills the process in that environment, and that an
    abort is not catchable by the ``except TclError`` guards below.

    Imported lazily: the ``--from-env`` path never opens a dialog, and must not pay for (or
    fail on) a client-side import while it is holding the activation secret.
    """
    from client.popup import backend as popup_backend  # lazy: same idiom as the tkinter imports

    reason = popup_backend.gui_registration_blocked_reason()
    if reason:
        return reason

    from installer.client_hosts import registry  # lazy: setup GUI only

    return registry.interactive_setup_blocked_reason()


def _webview_render_blocked() -> Optional[str]:
    """Why the WebView form specifically must not be attempted here, or ``None``.

    Sibling to ``_gui_session_blocked``, asked only on the production path: that
    one means NO window of any kind may open (macOS refuses registration and
    ABORTS the process); this one means only the WebView backend is unreliable
    here while the Tk form still renders. Measured inside the affected Windows
    sandbox: WebView2 paints a dead black frame that takes no input AND raises
    no exception, so the exception-driven Tk fallback below never triggers —
    the routing has to happen before webview is ever started.

    Imported lazily for the same reason as its sibling: ``--from-env`` never
    opens a dialog and must not pay for (or fail on) a client-side import.
    """

    from installer.client_hosts import registry  # lazy: setup GUI only

    return registry.webview_render_blocked_reason()


def _tk_creation_blocked() -> Optional[str]:
    """Why a Tk window can no longer be built in THIS process, or ``None`` to go ahead.

    Sibling to ``_gui_session_blocked`` and asked at the same two places, but about a
    different thing: that one is a property of the SESSION (may this process register a
    window at all), this one is a property of what has already happened INSIDE it — once
    pywebview's cocoa backend has installed a plain ``NSApplication``, Tk 9.0 aborts on
    creation. Both end in an uncatchable SIGABRT, so both are asked before Tk is touched.

    Imported lazily for the same reason as its sibling: ``--from-env`` never opens a dialog
    and must not pay for a client-side import while it is holding the activation secret.
    """
    from client.popup import backend as popup_backend  # lazy: same idiom as the tkinter imports

    return popup_backend.tk_creation_blocked_reason()


def _claim_app_identity() -> None:
    """Best-effort, and BEFORE the first Tk() — see client.tk_icon.claim_app_identity.

    Windows only. macOS has the same fall-back-to-the-interpreter defect but its lever must be
    pulled AFTER Tk exists, so it lives in ``_apply_window_icon`` instead."""
    try:
        from client.tk_icon import claim_app_identity

        claim_app_identity()
    except Exception:  # aqg: top-level boundary — the icon is never worth a failed installation
        return


def _apply_window_icon(root) -> None:
    """Best-effort: give Tk setup windows the product icon instead of Tk's default feather.

    ``apply_window_icon`` covers the Tk fallback form and establishes an application-wide
    default inherited by the status ``messagebox`` child. The import is guarded because the
    installer is its own package with no other dependency on ``client`` — if the body is not
    importable from here, the dialogs keep the feather rather than failing setup over decoration.

    Also where the macOS Dock tile is claimed: its call must come AFTER Tk has created its own
    ``NSApplication`` subclass, which is the opposite of the AUMID's ordering rule and the reason
    it is not in ``_claim_app_identity`` beside it. See ``client.tk_icon.apply_dock_icon``."""
    try:
        from client.tk_icon import apply_dock_icon, apply_window_icon

        apply_window_icon(root)
        apply_dock_icon()
    except Exception:  # aqg: top-level boundary — the icon is never worth a failed installation
        return


class _CredentialFormApi:
    """One-shot, thread-safe bridge from the local form to Python memory.

    This bridge deliberately holds no window reference and closes nothing. pywebview returns a
    ``js_api`` result by evaluating JS back into the page *after* the handler returns, on the same
    thread that ran it (``webview.util.js_bridge_call``), and the cocoa and GTK backends block that
    thread on a semaphore only a live webview can release. Destroying the window from inside a
    handler queues the teardown ahead of that delivery, so the form's promise never resolves and
    the non-daemon bridge thread then blocks interpreter shutdown for good. Window teardown is
    ``_close_form_when_settled``'s job, once the bridge thread is finished with the page.
    """

    def __init__(
        self,
        submit_handler: Optional[CredentialSubmitHandler] = None,
        errors: Optional[dict] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._result: Optional[object] = None
        self._terminal_error: Optional[ShellError] = None
        self._close_requested = False
        self._delivery_pending = False
        self._submitting = False
        self._settled = threading.Event()
        self._bridge_thread: Optional[threading.Thread] = None
        self._submit_handler = submit_handler
        # Resolved locale's PERMANENT_SETUP["errors"] for this bridge's own feedback (validation +
        # the concurrency notice). en-US by default so a directly-constructed bridge keeps English.
        self._errors = errors if errors is not None else _setup_strings("en-US")["errors"]

    def _finish(
        self,
        result: Optional[object],
        *,
        terminal_error: Optional[ShellError] = None,
    ):
        with self._lock:
            if self._done:
                return {"ok": False}
            self._done = True
            self._result = result
            self._terminal_error = terminal_error
            self._close_requested = True
            self._delivery_pending = True
            self._submitting = False
            self._bridge_thread = threading.current_thread()
        self._settled.set()
        return {"ok": True}

    def _abandon(self) -> bool:
        """Wake the closer after an OS/backend close without closing the window again."""
        with self._lock:
            if self._done:
                return False
            if self._submitting:
                return False
            self._done = True
            self._result = None
            self._terminal_error = None
            self._close_requested = False
            self._delivery_pending = False
            self._submitting = False
            self._bridge_thread = None
        self._settled.set()
        return True

    def _wait_until_settled(self):
        """Block until a form outcome exists, then return its teardown instruction."""
        self._settled.wait()
        with self._lock:
            return self._bridge_thread, self._close_requested

    def _submission_in_progress(self) -> bool:
        with self._lock:
            return self._submitting

    def _mark_delivery_complete(self) -> None:
        with self._lock:
            self._delivery_pending = False

    def submit(self, endpoint, secret):
        try:
            credentials = _validate_form_credentials(endpoint, secret, self._errors)
            with self._lock:
                if self._done:
                    return {"ok": False}
                if self._submitting:
                    return {
                        "ok": False,
                        "error": self._errors["in_progress"],
                    }
                self._submitting = True
            result = (
                self._submit_handler(*credentials)
                if self._submit_handler is not None
                else credentials
            )
        except _CredentialFormValidationError as exc:
            with self._lock:
                if not self._done:
                    self._submitting = False
            return {"ok": False, "field": exc.field, "error": str(exc)}
        except ActivationRecoveryRequiredError as exc:
            return self._finish(None, terminal_error=exc)
        except Exception:  # aqg: top-level boundary — release the one-shot bridge admission
            with self._lock:
                if not self._done:
                    self._submitting = False
            raise
        return self._finish(result)

    def cancel(self):
        with self._lock:
            if self._done:
                return {"ok": False}
            if self._submitting:
                return {
                    "ok": False,
                    "error": self._errors["in_progress"],
                }
            self._done = True
            self._result = None
            self._terminal_error = None
            self._close_requested = True
            self._delivery_pending = True
            self._bridge_thread = threading.current_thread()
        self._settled.set()
        return {"ok": True}

    def _window_close_allowed(self, *_args) -> bool:
        """Match pywebview's contract: ``False`` vetoes a cancellable close event."""
        with self._lock:
            return not (self._submitting or self._delivery_pending)

    def _is_done(self) -> bool:
        with self._lock:
            return self._done

    def _take_result(self) -> Optional[object]:
        with self._lock:
            result = self._result
            terminal_error = self._terminal_error
            self._result = None
            self._terminal_error = None
        if terminal_error is not None:
            raise terminal_error
        return result


def _apply_webview_icon(window) -> None:
    """Give the setup window the product icon once its native handle exists.

    The Tk dialogs get theirs from ``_apply_window_icon``; this one is pywebview, whose Windows
    backend is a WinForms host with no ``icon`` argument — that parameter exists only on the GTK
    and Qt backends — so the icon has to be stamped onto the window handle with WM_SETICON. Without
    it the first thing a new device sees is a window and a taskbar button branded as Python.

    On ``loaded`` because the handle does not exist until the window is realised, and idempotent
    because that event fires once per navigation."""
    if getattr(window, "_de_icon_set", False):
        return
    try:
        from client.tk_icon import apply_dock_icon, apply_taskbar_icon, native_window_handle

        hwnd = native_window_handle(window)
        window._de_icon_set = bool(hwnd) and apply_taskbar_icon(hwnd)
        apply_dock_icon()      # macOS: no handle involved, and a no-op everywhere else
    except Exception:  # aqg: top-level boundary — activation never fails over decoration
        return


def _close_form_when_settled(api, window) -> None:
    """Close the setup window once its form call is finished with the page, never before.

    pywebview hands a ``js_api`` return value back to the form after the handler returns, and on
    the cocoa and GTK backends that delivery blocks the bridge thread until the live webview
    answers. Waiting for that thread to finish is what keeps the teardown out of its way. A timed
    destroy is intentionally forbidden: if delivery is still running, destroying the cocoa/GTK
    webview can strand pywebview's non-daemon bridge thread during interpreter shutdown."""
    bridge, close_requested = api._wait_until_settled()
    if not close_requested:
        return
    # pywebview always runs a js_api handler on a worker thread, so a form that settled on the
    # main thread (a test double, a direct call) has no pending delivery to wait behind — and
    # joining the main thread would deadlock the caller.
    if bridge is not None and bridge not in (threading.current_thread(), threading.main_thread()):
        bridge.join()
    api._mark_delivery_complete()
    try:
        window.destroy()
    except Exception:  # aqg: top-level boundary — the OS close button may win the race
        pass


def _prompt_credentials_webview(
    webview_module,
    submit_handler: Optional[CredentialSubmitHandler] = None,
    strings: Optional[dict] = None,
):
    # One resolved locale table drives the page copy, the OS window title, and the bridge's own
    # feedback; en-US by default so a direct caller/test keeps the historical English form.
    strings = strings if strings is not None else _setup_strings()
    api = _CredentialFormApi(submit_handler=submit_handler, errors=strings["errors"])
    _claim_app_identity()
    try:
        window = webview_module.create_window(
            title=strings["window_title"],
            html=_render_credential_form_html(strings),
            js_api=api,
            width=520,
            height=560,
            resizable=True,
            background_color="#F4F6FA",
            text_select=False,
            zoomable=False,
        )
        if window is None:
            raise RuntimeError("webview returned no window")
        try:
            window.events.loaded += lambda *_a: _apply_webview_icon(window)
        except Exception:  # aqg: top-level boundary — backend event support is optional
            pass
        try:
            window.events.closing += api._window_close_allowed
        except Exception:  # aqg: top-level boundary — old backends may lack cancellable close
            pass
        closer = threading.Thread(
            target=_close_form_when_settled,
            args=(api, window),
            name="de-setup-form-closer",
            daemon=True,
        )
        closer.start()
        webview_module.start(debug=False, private_mode=True)
    except Exception:  # aqg: top-level boundary — clear captured input before backend fallback
        # A native backend may fail its UI loop while its bridge worker is still completing.
        # Preserve that outcome so a successful activation is never followed by a second prompt.
        if api._submission_in_progress():
            api._wait_until_settled()
        if api._is_done():
            return api._take_result()
        api._take_result()
        raise
    finally:
        if not api._abandon() and api._submission_in_progress():
            api._wait_until_settled()
    return api._take_result()


def _prompt_credentials_tk(
    tk_module=None, widget_module=None, strings: Optional[dict] = None
) -> Optional[Credentials]:
    # Resolve the form language ONCE up front (before any widget is built) so the window title, every
    # label/hint/button, and the error feedback all read from the same PERMANENT_SETUP table. en-US by
    # default so a direct caller/test with no locale env keeps the historical English form.
    strings = strings if strings is not None else _setup_strings()
    errors = strings["errors"]
    if tk_module is None:
        try:
            import tkinter as tk_module
        except ImportError as exc:
            raise ShellError(
                "the masked configuration window is unavailable because tkinter is not installed"
            ) from exc
    if widget_module is None:
        try:
            from tkinter import ttk as widget_module
        except ImportError as exc:
            raise ShellError(
                "the masked configuration window is unavailable because tkinter ttk is not installed"
            ) from exc
    if getattr(tk_module, "TkVersion", 8.6) < 8.6:
        raise ShellError(
            "the masked configuration window requires tkinter 8.6 or newer"
        )
    widgets = widget_module

    _claim_app_identity()
    try:
        root = tk_module.Tk()
    except tk_module.TclError as exc:
        raise ShellError(
            "the masked configuration window could not open in this desktop session"
        ) from exc
    result: Optional[Credentials] = None
    closed = False

    def close() -> None:
        nonlocal closed
        if not closed:
            closed = True
            root.destroy()

    try:
        root.withdraw()
        root.title(strings["window_title"])
        root.configure(background="#F4F6FA")
        root.resizable(False, False)
        _apply_window_icon(root)

        style = widgets.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Setup.TFrame", background="#F4F6FA")
        style.configure("Header.TFrame", background="#132238")
        style.configure(
            "HeaderTitle.TLabel",
            background="#132238",
            foreground="#FFFFFF",
            font=("Segoe UI", 19, "bold"),
        )
        style.configure(
            "HeaderSubtitle.TLabel",
            background="#132238",
            foreground="#B9C5D6",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Card.TFrame",
            background="#FFFFFF",
            bordercolor="#E2E7EF",
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Field.TLabel",
            background="#FFFFFF",
            foreground="#26364A",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Hint.TLabel",
            background="#FFFFFF",
            foreground="#718096",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Error.TLabel",
            background="#FFFFFF",
            foreground="#B42318",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Setup.TEntry",
            fieldbackground="#FFFFFF",
            foreground="#172B4D",
            bordercolor="#C9D2E0",
            lightcolor="#C9D2E0",
            darkcolor="#C9D2E0",
            padding=(10, 8),
            font=("Segoe UI", 11),
        )
        style.configure(
            "Secondary.TButton",
            background="#FFFFFF",
            foreground="#344054",
            bordercolor="#CBD5E1",
            padding=(18, 8),
            font=("Segoe UI", 10),
        )
        style.configure(
            "Primary.TButton",
            background="#2F6FEB",
            foreground="#FFFFFF",
            bordercolor="#2F6FEB",
            padding=(20, 9),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#2459BD"), ("pressed", "#1D4A9E")],
        )

        endpoint_var = tk_module.StringVar(root)
        secret_var = tk_module.StringVar(root)
        endpoint_validation = (
            root.register(lambda proposed: len(proposed) <= _MAX_ENDPOINT_INPUT),
            "%P",
        )
        secret_validation = (
            root.register(lambda proposed: len(proposed) <= _MAX_SECRET_INPUT),
            "%P",
        )

        def submit() -> None:
            nonlocal result
            try:
                result = _validate_form_credentials(
                    endpoint_var.get(), secret_var.get(), errors
                )
            except _CredentialFormValidationError as exc:
                error_label.configure(text=str(exc))
                if exc.field == "secret":
                    secret_entry.focus_set()
                else:
                    endpoint_entry.focus_set()
                return
            close()

        header = widgets.Frame(root, style="Header.TFrame", height=104)
        header.pack(fill="x")
        if hasattr(header, "pack_propagate"):
            header.pack_propagate(False)
        widgets.Label(
            header,
            text=strings["title"],
            style="HeaderTitle.TLabel",
            anchor="w",
        ).pack(fill="x", padx=30, pady=(22, 2))
        widgets.Label(
            header,
            text=strings["subtitle"],
            style="HeaderSubtitle.TLabel",
            anchor="w",
        ).pack(fill="x", padx=30)

        body = widgets.Frame(root, style="Setup.TFrame")
        body.pack(fill="both", expand=True, padx=24, pady=20)
        card = widgets.Frame(body, style="Card.TFrame")
        card.pack(fill="both", expand=True)
        card.columnconfigure(0, weight=1)

        widgets.Label(
            card,
            text=strings["endpoint_label"],
            style="Field.TLabel",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 6))
        endpoint_entry = widgets.Entry(
            card,
            textvariable=endpoint_var,
            style="Setup.TEntry",
            validate="key",
            validatecommand=endpoint_validation,
        )
        endpoint_entry.grid(row=1, column=0, sticky="ew", padx=24)
        widgets.Label(
            card,
            text=strings["endpoint_hint"],
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="ew", padx=24, pady=(5, 14))

        widgets.Label(
            card,
            text=strings["secret_label"],
            style="Field.TLabel",
            anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 6))
        secret_entry = widgets.Entry(
            card,
            textvariable=secret_var,
            show="•",
            style="Setup.TEntry",
            validate="key",
            validatecommand=secret_validation,
        )
        secret_entry.grid(row=4, column=0, sticky="ew", padx=24)
        widgets.Label(
            card,
            text=strings["secret_hint"],
            style="Hint.TLabel",
        ).grid(row=5, column=0, sticky="ew", padx=24, pady=(5, 16))

        error_label = widgets.Label(
            card,
            text="",
            style="Error.TLabel",
            anchor="w",
        )
        error_label.grid(row=6, column=0, sticky="ew", padx=24)

        actions = widgets.Frame(card, style="Card.TFrame")
        actions.grid(row=7, column=0, sticky="e", padx=24, pady=(8, 20))
        widgets.Button(
            actions,
            text=strings["cancel"],
            command=close,
            style="Secondary.TButton",
        ).pack(side="left", padx=(0, 10))
        widgets.Button(
            actions,
            text=strings["activate"],
            command=submit,
            style="Primary.TButton",
        ).pack(side="left")

        root.protocol("WM_DELETE_WINDOW", close)
        endpoint_entry.bind("<Return>", lambda _event: submit())
        secret_entry.bind("<Return>", lambda _event: submit())
        root.bind("<Escape>", lambda _event: close())
        root.update_idletasks()
        width, height = 520, 430
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        root.deiconify()
        root.wait_visibility()
        endpoint_entry.focus_set()
        root.grab_set()
        root.wait_window()
        return result
    except tk_module.TclError as exc:
        raise ShellError(
            "the masked configuration window could not open in this desktop session"
        ) from exc
    finally:
        if not closed:
            try:
                root.destroy()
            except tk_module.TclError:
                pass


def _prompt_credentials_gui(
    tk_module=None,
    widget_module=None,
    webview_module=None,
    submit_handler: Optional[CredentialSubmitHandler] = None,
    strings: Optional[dict] = None,
):
    # Resolve the form language ONCE here and hand the same table to whichever backend wins, so the
    # webview and its Tk fallback never render two different languages within a single prompt. The
    # caller (run_permanent_setup) passes the table it already bound into submit_handler; a direct
    # caller/test that passes nothing gets the resolver's choice (en-US when no locale env is set).
    strings = strings if strings is not None else _setup_strings()
    # Both Tk and pywebview register an in-process desktop application. In a confined agent shell,
    # macOS aborts that registration instead of raising, so refuse before importing either backend.
    blocked = _gui_session_blocked()
    if blocked is not None:
        raise ShellError(
            "the masked configuration window cannot open in this session (%s); "
            "run permanent setup from a normal desktop terminal, or non-interactively with "
            "--from-env (reads DE_ENDPOINT and DE_ACTIVATION_SECRET from the environment)"
            % blocked
        )
    if tk_module is not None or widget_module is not None:
        return _prompt_credentials_tk(tk_module, widget_module, strings)
    if webview_module is None:
        # Only the production path consults the WebView render gate: a caller
        # (or test) injecting an explicit webview_module has already chosen that
        # backend. The gate must fire BEFORE webview starts because a rendering
        # failure inside the affected Windows host sandbox does not raise —
        # instead, it paints a dead black frame and blocks until the window is
        # killed, so no exception-driven fallback would ever run.
        webview_block = _webview_render_blocked()
        if webview_block is not None:
            # The Tk form draws with plain GDI — no browser subprocess, no GPU
            # composition — and is the one backend measured to render there.
            print(
                "de-permanent-setup: %s; opening the masked Tk form" % webview_block,
                file=sys.stderr,
            )
            return _prompt_credentials_tk(strings=strings)
        try:
            import webview as webview_module
        except ImportError:
            return _prompt_credentials_tk(strings=strings)
    try:
        return _prompt_credentials_webview(webview_module, submit_handler, strings)
    except ActivationRecoveryRequiredError:
        raise
    except Exception:  # aqg: top-level boundary — backend details must not reflect credential input
        # pywebview may have registered this process as an NSApplication on its way down;
        # the Tk fallback would then ABORT rather than raise, and this handler would never
        # run again. Refuse it with the one route the caller has left instead.
        poisoned = _tk_creation_blocked()
        if poisoned is not None:
            raise ShellError(
                "the masked configuration window failed and its fallback cannot open in "
                "this process (%s); rerun permanent setup, or run it non-interactively "
                "with --from-env (reads DE_ENDPOINT and DE_ACTIVATION_SECRET from the "
                "environment)" % poisoned
            ) from None
        try:
            return _prompt_credentials_tk(strings=strings)
        except Exception:  # aqg: top-level boundary — neither backend detail may reflect input
            raise ShellError(
                "the masked configuration window could not open in this desktop session"
            ) from None


def _credentials_from_env() -> Optional[Credentials]:
    endpoint = os.environ.pop("DE_ENDPOINT", "").strip()
    activation_secret = os.environ.pop("DE_ACTIVATION_SECRET", "")
    if not endpoint or not activation_secret.strip():
        activation_secret = ""
        return None
    return endpoint, activation_secret


def _show_gui_message(title: str, message: str, *, error: bool = False) -> None:
    if _gui_session_blocked() is not None or _tk_creation_blocked() is not None:
        # Same in-process abort risk as the prompt above, and a status dialog is never worth
        # it: every caller already printed this message to stderr/stdout first (see the four
        # sites in run_permanent_setup), so nothing is lost by staying silent here.
        #
        # The second gate covers the path that actually shipped broken: after the pywebview
        # form ran, EVERY exit through main() came here and aborted on ``tk.Tk()`` — which
        # took the exit code with it, so even a SUCCESSFUL activation looked like a failed
        # install, and a failed one lost its error dialog on top of the real error.
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return
    _claim_app_identity()
    try:
        root = tk.Tk()
    except tk.TclError:
        return
    root.withdraw()
    _apply_window_icon(root)
    try:
        if error:
            messagebox.showerror(title, message, parent=root)
        else:
            messagebox.showinfo(title, message, parent=root)
    finally:
        root.destroy()


def run_permanent_setup(
    *,
    prompt: Optional[CredentialPrompt] = None,
    from_env: bool = False,
    clients: Optional[Sequence[str]] = None,
) -> PermanentSetupResult:
    """Configure once, persist device credentials, wire MCP, and run Doctor."""
    config_path = _managed_config_path()
    environment_credentials = _credentials_from_env() if from_env else None
    current = _load_managed_config(config_path)
    already_activated = activate.is_permanently_activated(current)
    if not already_activated:
        if activate.activation_recovery_marker_path(config_path).exists():
            raise ActivationRecoveryRequiredError(
                "a previous device activation may have reached the service without saving "
                "local credentials; do not retry automatically — preserve the installation "
                "and use the owner-guided recovery flow"
            )
        credentials = None
        summary = None
        if not from_env and prompt is None:
            # Resolve the form language ONCE here so the visible form and the in-form activation
            # feedback (raised by _activate_from_form) speak the same language: the same errors table
            # is bound into the handler and handed to the GUI.
            setup_strings = _setup_strings()
            setup_errors = setup_strings["errors"]
            form_result = _prompt_credentials_gui(
                submit_handler=lambda endpoint, activation_secret: _activate_from_form(
                    endpoint,
                    activation_secret,
                    config_path=config_path,
                    errors=setup_errors,
                ),
                strings=setup_strings,
            )
            if form_result is None:
                return PermanentSetupResult(permanent=False, cancelled=True)
            if isinstance(form_result, tuple) and len(form_result) == 2:
                # Compatibility for injected/test prompts that still return credentials.
                credentials = form_result
            else:
                summary = form_result
        else:
            credentials = environment_credentials if from_env else prompt()
            if credentials is None:
                return PermanentSetupResult(
                    permanent=False,
                    cancelled=not from_env,
                    credentials_missing=from_env,
                )
        environment_credentials = None
        if credentials is not None:
            endpoint, activation_secret = credentials
            credentials = None
            endpoint = _validate_endpoint(endpoint)
            if not activation_secret.strip():
                raise ShellError("owner-issued activation key is required")
            try:
                summary = _activate_once(
                    endpoint,
                    activation_secret,
                    config_path=config_path,
                )
            except activate.ActivationRecoveryRequiredError:
                raise
            except Exception:  # aqg: top-level boundary — server output may reflect the activation secret
                # Never surface a server-controlled detail in the GUI: a hostile or
                # misconfigured endpoint could reflect the supplied secret.
                raise PermanentSetupError(
                    "activation_failed_safe",
                    "device activation failed; verify the owner-issued values and network, then retry"
                ) from None
            finally:
                activation_secret = ""
            if not summary.get("activated") and not summary.get("already_activated"):
                raise PermanentSetupError(
                    "activation_incomplete",
                    "device activation did not return a permanent binding",
                )
    else:
        environment_credentials = None
        activate.clear_activation_recovery_marker(config_path, retry_must_be_safe=False)
        activate.scrub_staged_invitation(config_path)

    host_configuration = (
        _configure_agent_hosts(config_path)
        if clients is None
        else _configure_agent_hosts(config_path, clients)
    )
    failures = host_configuration.failures
    notices = host_configuration.notices
    if _doctor_has_blocking_failure(failures):
        raise PermanentSetupError(
            "doctor_failure",
            "device credentials and MCP wiring are saved, but Doctor still reports a "
            "failure; fix the reported item and rerun permanent setup"
        )
    return PermanentSetupResult(
        permanent=True,
        already_activated=already_activated,
        host_failures=failures,
        post_mcp_write_notices=notices,
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="de-permanent-setup",
        description="Permanently activate this managed device using a masked dialog or process environment",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="read DE_ENDPOINT and DE_ACTIVATION_SECRET without opening a dialog",
    )
    parser.add_argument(
        "--client",
        action="append",
        choices=mcp_config.CLIENTS,
        help="wire this selected Agent client after activation (repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        result = run_permanent_setup(from_env=args.from_env, clients=args.client)
    except ActivationRecoveryRequiredError as exc:
        message = str(exc)
        print("de-permanent-setup: %s" % message, file=sys.stderr)
        if not args.from_env:
            title, gui_message = _localized_setup_recovery_dialog()
            _show_gui_message(title, gui_message, error=True)
        return RECOVERY_REQUIRED_EXIT_CODE
    except ShellError as exc:  # aqg: top-level boundary — render a redacted user-facing failure
        message = str(exc)
        print("de-permanent-setup: %s" % message, file=sys.stderr)
        if not args.from_env:
            title, gui_message = _localized_setup_failure_dialog(exc)
            _show_gui_message(title, gui_message, error=True)
        return 1
    except Exception:  # aqg: top-level boundary — never print unexpected credential-path details
        message = "unexpected setup failure; no credential details were printed"
        print("de-permanent-setup: %s" % message, file=sys.stderr)
        if not args.from_env:
            title, gui_message = _localized_setup_failure_dialog()
            _show_gui_message(title, gui_message, error=True)
        return 1

    if result.credentials_missing:
        print(
            "Decision Engine installation is ready; device activation was skipped because "
            "DE_ENDPOINT and DE_ACTIVATION_SECRET are not both available in this process."
        )
        return 0
    if result.cancelled:
        print("Decision Engine permanent setup was cancelled; no credentials were changed.")
        return 2
    if result.host_failures:
        failed_hosts = ", ".join(client for client, _reason in result.host_failures)
        message = (
            "Permanent setup succeeded for the other Agent clients. "
            "These clients still need repair: %s. Fully restart the successfully "
            "configured Agents."
            % failed_hosts
        )
    elif result.already_activated:
        message = (
            "This device was already permanently activated. MCP wiring and Doctor are ready. "
            "Fully restart the Agent to load Decision Engine."
        )
    else:
        message = (
            "Permanent setup succeeded. The activation key was not retained. "
            "Fully restart the Agent; future restarts require no repeated input."
        )
    if result.post_mcp_write_notices:
        message += "\n\n" + "\n".join(result.post_mcp_write_notices)
    print(mcp_config.console_safe_text(message))
    if not args.from_env:
        title, gui_message = _localized_setup_success_dialog(result)
        _show_gui_message(title, gui_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
