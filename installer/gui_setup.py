"""Best-effort GUI environment preparation for installer entry points.

tkinter is a Python/system component and cannot be installed safely with pip;
we probe it in the same interpreter that MCP will run and report a warning when
it is absent. pywebview is a Python package, so the existing popup backend may
install it into that exact interpreter. Neither optional GUI component can turn
a successful core installation into a failure.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum

from client.popup import backend as popup_backend


class TkinterState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True)
class GuiSetupResult:
    tkinter: TkinterState
    pywebview: popup_backend.WebviewResult


def inspect_tkinter(python: str, *, timeout_s: int = 30) -> TkinterState:
    try:
        probe = subprocess.run(
            [python, "-c", "import tkinter"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            env=popup_backend.credential_free_environment(),
        )
    except Exception:  # aqg: top-level boundary — optional GUI probe becomes a warning
        return TkinterState.PROBE_FAILED
    return TkinterState.READY if probe.returncode == 0 else TkinterState.MISSING


def prepare_gui_environment(python: str) -> GuiSetupResult:
    tkinter_state = inspect_tkinter(python)
    webview_result = popup_backend.prepare_webview(python, label="de-gui-setup")
    print(
        "de-gui-setup: tkinter=%s pywebview=%s"
        % (tkinter_state.value, webview_result.state.value)
    )
    if tkinter_state is not TkinterState.READY:
        print(
            "de-gui-setup: tkinter is unavailable; core MCP remains installed, "
            "but the configuration/stop window may need a Python build with tkinter."
        )
    if not webview_result.ready:
        print(
            "de-gui-setup: pywebview is not ready; core MCP remains installed and "
            "the popup will retry preparation on first use."
        )
    return GuiSetupResult(tkinter=tkinter_state, pywebview=webview_result)
