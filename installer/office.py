"""LibreOffice discovery + on-demand install for the Office → board conversion path.

LibreOffice is OPTIONAL: boards and PDF files work without it; only converting
DOCX / PPTX / XLSX to a board needs it. Unlike pywebview (a pip package installed
into the client's own environment), LibreOffice is a SYSTEM package — so "on
demand" means: discover it; if it is absent, attempt a *user-space* install
(macOS Homebrew cask) or return the exact command for the user to run. This
module NEVER runs a silent ``sudo``. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional, Tuple

# macOS installs the .app bundle, whose CLI is not on PATH by default — probe both
# the system and per-user Applications locations.
_MAC_APP_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_MAC_APP_SOFFICE_USER = "~/Applications/LibreOffice.app/Contents/MacOS/soffice"


def find_libreoffice() -> Optional[str]:
    """Absolute path to a runnable LibreOffice CLI (``soffice`` / ``libreoffice``),
    or ``None`` when it is not installed."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "darwin":
        for cand in (_MAC_APP_SOFFICE, os.path.expanduser(_MAC_APP_SOFFICE_USER)):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def install_hint() -> str:
    """The exact manual install command for this platform (never executed silently)."""
    if sys.platform == "darwin":
        return "brew install --cask libreoffice"
    # Linux: apt is the common case; dnf for Fedora/RHEL. Both need root — the
    # user runs them, we never sudo on their behalf.
    return "sudo apt-get install -y libreoffice   # or: sudo dnf install -y libreoffice"


def ensure_libreoffice(*, auto: bool = True, install_timeout_s: int = 1800) -> Tuple[bool, str]:
    """Make LibreOffice available for Office → PDF; return ``(ready, detail)``.

    Discovers first. If it is absent, ``auto`` is set, and Homebrew is present
    (macOS), run the *user-space* cask install once, then re-discover. Linux
    installs need root and are NOT auto-run — the apt/dnf command is returned for
    the user instead. Returns ``(True, <path>)`` when ready, else
    ``(False, <install command>)``. Never raises for an install failure — a failed
    auto-install degrades to the manual hint.
    """
    found = find_libreoffice()
    if found:
        return True, found
    if auto and sys.platform == "darwin":
        # Pin the RESOLVED brew path (don't invoke a bare `brew` off a mutable PATH).
        # stdin=DEVNULL so a cask prompt can't block until the timeout; capture_output
        # keeps the install quiet. Any failure degrades to the manual hint (never raises).
        brew = shutil.which("brew")
        if brew:
            try:
                subprocess.run(
                    [brew, "install", "--cask", "libreoffice"],
                    check=False, timeout=install_timeout_s,
                    stdin=subprocess.DEVNULL, capture_output=True,
                )
            except Exception:  # aqg: top-level boundary — any install failure degrades to the hint
                pass
            found = find_libreoffice()
            if found:
                return True, found
    return False, install_hint()


def main() -> int:
    """`python3 -m installer.office` — install LibreOffice on demand (or print how to).

    Exit 0 when LibreOffice is available afterwards, 1 when the user must run the
    printed command themselves (Linux root install, or a failed brew install).
    """
    ready, detail = ensure_libreoffice()
    if ready:
        print("LibreOffice ready: %s" % detail)
        return 0
    print("LibreOffice not installed. Install it with:\n    %s" % detail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
