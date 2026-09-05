"""Native-popup backend pre-flight: probe + one-time auto-install of pywebview.

The DB/GE popup opens in a native pywebview window. When pywebview is genuinely
ABSENT we install it once (into the target venv) rather than degrade — but we NEVER
open a system browser as a fallback (Owner constraint: the popup is native-window
only). All functions are pure over a passed venv-python path with no module-level
side effects, so importing this is cheap and safe.

Mechanism mirrors the hub's popup pre-flight (real backend probe, pinned floor,
no --upgrade); the feature-heavy window chrome is deliberately NOT copied — this
ships a placeholder shell only.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# How much of a failed `pip install` to surface on stderr: enough to show the actual
# failing step (e.g. "Failed building wheel for pyobjc-core" + the compiler error under
# it) without dumping a multi-hundred-line build log.
_INSTALL_ERROR_TAIL_LINES = 25
_OWNER_ENV_KEYS = ("DE_ENDPOINT", "DE_ACTIVATION_SECRET")


def credential_free_environment():
    """Return a child environment that cannot expose owner activation values."""
    environment = os.environ.copy()
    for key in _OWNER_ENV_KEYS:
        environment.pop(key, None)
    return environment


class WebviewState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    INSTALL_FAILED = "install_failed"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    PROBE_FAILED = "probe_failed"
    NO_GUI_SESSION = "no_gui_session"


@dataclass(frozen=True)
class WebviewResult:
    state: WebviewState
    pip_exit_code: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self.state is WebviewState.READY


def _process_is_sandboxed() -> Optional[bool]:
    """Whether this process runs under a macOS sandbox — ``None`` when undetectable.

    ``sandbox_check(pid, NULL, 0)`` is libsystem's own "am I confined" query (1 =
    confined). We ask about OURSELVES because a probe child inherits our sandbox.
    """
    try:
        sandbox_check = ctypes.CDLL(None).sandbox_check
    except Exception:  # aqg: top-level boundary — no symbol here means no verdict, not a crash
        return None
    sandbox_check.restype = ctypes.c_int
    sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    try:
        return sandbox_check(os.getpid(), None, 0) == 1
    except Exception:  # aqg: top-level boundary — an unusable syscall yields no verdict
        return None


def gui_registration_blocked_reason() -> Optional[str]:
    """Why a DIAGNOSTIC probe should not make a child register as a macOS GUI app.

    pywebview's cocoa backend calls ``NSApplication.sharedApplication()`` in its class
    body, so ``webview.initialize()`` registers the child with the WindowServer/Dock at
    import time. Where that registration is refused macOS does not raise — it ABORTS the
    child inside ``HIServices _RegisterApplication`` (SIGABRT) and shows the user a
    "Python quit unexpectedly" crash dialog. Observed in the wild from an agent's
    seatbelt shell running ``./install.sh`` (crash reports: parentProc Python,
    responsibleProc the agent app).

    This is deliberately an OVER-APPROXIMATION, not a measurement: confinement is not the
    same as refused registration (a permissive seatbelt profile registers fine), and the
    ssh variables describe provenance, not capability. It is therefore only ever used to
    skip a probe NOBODY ASKED FOR — see ``avoid_gui_registration``. It must never be used
    to refuse a window the user actually asked for, where a false positive would silently
    disable the feature. Detection is also FAIL-OPEN: an undetectable environment answers
    ``None`` and probes exactly as it did before this gate existed.
    """
    if sys.platform != "darwin":  # only macOS aborts instead of failing; leave others untouched
        return None
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "remote shell session"
    if _process_is_sandboxed():
        return "sandboxed process (agent shell)"
    return None


# The ObjC class Tk installs for its own NSApplication. Anything else in that slot was put
# there by another framework and does not answer Tk's selectors.
_TK_APPLICATION_CLASS = "TKApplication"


def tk_creation_blocked_reason() -> Optional[str]:
    """Why constructing ``tkinter.Tk()`` in THIS process would abort it, or ``None``.

    Tk 9.0's macOS backend resolves system colours through ``[NSApp macOSVersion]``, a
    selector defined only on its own ``TKApplication`` subclass. Where pyobjc got to
    ``NSApplication.sharedApplication()`` first — pywebview's cocoa backend does it at
    import time — ``NSApp`` is a plain ``NSApplication``, the selector is unrecognised, and
    Tk aborts the process inside ``Tkapp_New`` instead of raising::

        -[NSApplication macOSVersion]: unrecognized selector sent to instance …
        Tkapp_New -> Tcl_AppInit -> TkCreateFrame -> Tk_GetColor -> TkpGetColor -> GetRGBA

    (Measured, not inferred: crash report ``Python-2026-08-26-213325.ips``, SIGABRT out of
    ``libtcl9tk9.0.dylib``, reproduced with both ``AppKit`` and ``webview.initialize()``
    as the first mover.)

    NOT a Tk 9 regression, and a Tk downgrade does not fix it: Tk 8.5 aborts the same way on
    a foreign ``NSApp``, only through ``-[NSApplication _setup:]`` instead. The invariant is
    that Tk must OWN the process NSApplication, so this gate keys on ownership — an
    allow-list of Tk's own class — and never on a Tk version.

    Polarity is the OPPOSITE of ``gui_registration_blocked_reason``: this one is
    FAIL-CLOSED. There, a false positive would silently disable a window the user asked
    for; here, a false negative kills the process with an uncatchable abort while a false
    positive only degrades a path that has already fallen back once.

    ``AppKit`` is read out of ``sys.modules`` and never imported: importing it is precisely
    the registration this function exists to detect.
    """
    if sys.platform != "darwin":  # only Tk's macOS backend reaches for an NSApp selector
        return None
    appkit = sys.modules.get("AppKit")
    if appkit is None:  # nothing has registered; Tk will install its own TKApplication
        return None
    try:
        running_application = appkit.NSApp()
    except Exception:  # aqg: top-level boundary — no verdict means do not build (fail closed)
        return "the running macOS application object could not be inspected"
    if running_application is None:  # AppKit merely imported — sharedApplication() never ran
        return None
    if type(running_application).__name__ == _TK_APPLICATION_CLASS:
        return None  # Tk already owns the process; further Tk windows are the normal case
    return "another framework already registered this process as an NSApplication"


def _ready_probe_state(
    python: str, *, timeout_s: int = 30, avoid_gui_registration: bool = False
) -> WebviewState:
    if avoid_gui_registration and gui_registration_blocked_reason() is not None:
        return WebviewState.NO_GUI_SESSION
    try:
        probe = subprocess.run(
            [python, "-c", "import webview; webview.initialize()"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            env=credential_free_environment(),
        )
    except Exception:  # aqg: top-level boundary — probe failures are classified, never raised
        return WebviewState.PROBE_FAILED
    return WebviewState.READY if probe.returncode == 0 else WebviewState.BACKEND_UNAVAILABLE


def webview_backend_ready(python: str, *, timeout_s: int = 30) -> bool:
    """True iff ``python`` can ACTUALLY open a pywebview window — not merely import it.

    A bare ``import webview`` succeeds even where no GUI backend (macOS WebKit /
    WebKit2GTK / Qt) is available — a useless shell that would spawn-then-crash and
    lose even the honest failure. ``webview.initialize()`` runs pywebview's real
    backend selection and exits nonzero when none is available. Probed in a
    subprocess so loading a GUI backend can never perturb this process.

    (`webview.initialize()` is a real public API — verified present + callable in
    pywebview 6.2.1, the version the reference client ships; the ``>=4.0`` floor in
    ``pywebview_pip_spec`` guarantees it exists.)
    """
    return _ready_probe_state(python, timeout_s=timeout_s) is WebviewState.READY


def inspect_webview(
    python: str, *, timeout_s: int = 30, avoid_gui_registration: bool = False
) -> WebviewResult:
    """Classify pywebview without installing or mutating the environment.

    ``avoid_gui_registration`` is for callers that only want a DIAGNOSIS (Doctor) and are
    not about to open a window: where GUI registration is refused, the readiness probe
    would abort its child with a user-visible macOS crash dialog instead of answering, so
    those callers get ``NO_GUI_SESSION`` rather than a crash. Callers that are preparing a
    real window leave it False and probe exactly as before — a session misjudged as
    GUI-less must never cost the user a popup they asked for.

    A bare ``import webview`` does NOT load a platform backend (``webview/__init__`` pulls
    ``guilib`` but the cocoa module is imported inside ``initialize()``), so the MISSING
    classification below stays registration-free and pip install still runs everywhere —
    verified under a WindowServer-denying seatbelt profile.
    """
    try:
        imported = subprocess.run(
            [python, "-c", "import webview"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            env=credential_free_environment(),
        )
    except Exception:  # aqg: top-level boundary — probe failures are returned as data
        return WebviewResult(WebviewState.PROBE_FAILED)
    if imported.returncode != 0:
        return WebviewResult(WebviewState.MISSING)
    return WebviewResult(
        _ready_probe_state(
            python, timeout_s=timeout_s, avoid_gui_registration=avoid_gui_registration
        )
    )


def pywebview_pip_spec() -> str:
    """The pip requirement for a native-popup backend.

    macOS/Windows: plain ``pywebview`` pulls its native backend (pyobjc / pythonnet)
    as wheels. Linux: the GTK extra. Pin a floor (``>=4.0``) so the installed
    pywebview always exposes the public ``initialize()`` selector the probe relies
    on. Do NOT ``--upgrade`` — install the absent package, never bump a user's
    existing pinned one.
    """
    return "pywebview>=4.0" if sys.platform in ("darwin", "win32") else "pywebview[gtk]>=4.0"


def prepare_webview(
    python: str, label: str = "de-popup", *, install_timeout_s: int = 300
) -> WebviewResult:
    """Prepare pywebview in ``python`` and return a diagnostic result.

    If pywebview is ABSENT, pip-install it INTO that venv once, then re-probe. Install
    ONLY when webview is genuinely absent: if it imports but the GUI backend won't
    init (a headless box, or Linux missing the WebKit2GTK system lib), pip cannot fix
    that — installing would just burn the whole ``install_timeout_s`` every launch —
    so we skip to the honest ``False`` return with a hint. ``label`` prefixes the
    user-facing stderr notices.
    """
    current = inspect_webview(python)
    if current.state is WebviewState.MISSING:
        # An old bundled pip (macOS's Xcode-CLT python3 ships pip ~21.2) can fail to
        # resolve a prebuilt pyobjc-core wheel and fall back to a from-source build,
        # which then breaks against a newer Xcode clang's stricter default warnings.
        # Upgrading pip first makes it prefer the wheel it was supposed to get in the
        # first place. Best-effort: a failed upgrade (no network, locked-down site-
        # packages) still falls through to the install attempt below as before.
        try:
            subprocess.run(
                [python, "-m", "pip", "install", "--upgrade", "pip"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                env=credential_free_environment(),
            )
        except Exception:  # aqg: top-level boundary — pip upgrade failed; install attempt decides
            pass
        print(
            "%s: installing pywebview for the native popup (one-time, ~30s)…" % label,
            file=sys.stderr,
        )
        try:
            install = subprocess.run(
                [python, "-m", "pip", "install", pywebview_pip_spec()],
                stdin=subprocess.DEVNULL,  # never block on a pip confirmation (PEP 668, etc.)
                stdout=subprocess.PIPE,    # captured (not DEVNULL) so a real failure is diagnosable —
                stderr=subprocess.STDOUT,  # this is what surfaced the pyobjc-core compile error by hand.
                timeout=install_timeout_s,
                text=True,
                env=credential_free_environment(),
            )
            if install.returncode != 0:
                lines = install.stdout.strip().splitlines() if install.stdout else []
                tail = "\n".join(lines[-_INSTALL_ERROR_TAIL_LINES:])
                print(
                    "%s: pywebview install failed (pip exit %d), last %d line(s):\n%s"
                    % (label, install.returncode, min(len(lines), _INSTALL_ERROR_TAIL_LINES), tail),
                    file=sys.stderr,
                )
                return WebviewResult(
                    WebviewState.INSTALL_FAILED, pip_exit_code=install.returncode
                )
        except Exception as exc:  # aqg: top-level boundary — install crashed; re-probe decides
            print("%s: pywebview install crashed: %r" % (label, exc), file=sys.stderr)
            return WebviewResult(WebviewState.INSTALL_FAILED)
        current = inspect_webview(python)
        if current.state is WebviewState.MISSING:
            current = WebviewResult(WebviewState.INSTALL_FAILED, pip_exit_code=0)
    if current.state is WebviewState.BACKEND_UNAVAILABLE and sys.platform not in ("darwin", "win32"):
        print(
            "%s: the native popup needs the WebKit2GTK system library "
            "(Debian/Ubuntu: sudo apt install gir1.2-webkit2-4.1), which pip can't install."
            % label,
            file=sys.stderr,
        )
    return current


def ensure_webview(python: str, label: str = "de-popup", *, install_timeout_s: int = 300) -> bool:
    """Backward-compatible runtime helper returning only popup readiness."""
    return prepare_webview(
        python, label=label, install_timeout_s=install_timeout_s
    ).ready
