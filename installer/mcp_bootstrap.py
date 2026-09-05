"""Cwd-independent MCP launcher bootstrap without inline Python arguments.

The registered command passes this tracked file by absolute path, followed by
the same checkout root.  Binding the two paths before importing the package
keeps an open workspace from shadowing ``installer.launcher`` while satisfying
hosts that reject the semicolons used by ``python -c``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print(
            "de-mcp-bootstrap: expected checkout root, launcher mode, and bound root",
            file=sys.stderr,
        )
        return 2

    root = Path(arguments.pop(0)).resolve()
    mode = arguments[0]
    bound_root = Path(arguments[1]).resolve()
    if mode not in {"--dev-root", "--managed-root"} or bound_root != root:
        print("de-mcp-bootstrap: launcher mode/root binding is invalid", file=sys.stderr)
        return 2
    actual = Path(__file__).resolve()
    expected = (root / "installer" / "mcp_bootstrap.py").resolve()
    if actual != expected:
        print("de-mcp-bootstrap: bootstrap path does not match checkout root", file=sys.stderr)
        return 2

    sys.path[0] = str(root)
    from installer.launcher import main as launcher_main

    return launcher_main([mode, str(root)])


if __name__ == "__main__":
    raise SystemExit(main())
