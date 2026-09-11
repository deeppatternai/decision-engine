"""Decision Engine public client shell.

This package is the open-source client layer of the MIT-licensed
``deeppatternai/decision-engine`` repository. It contains only:

- ``install``  — the unified ``install de|aqg|all`` installer
- ``shim``     — an MCP-over-HTTP forwarding shim (transport only, no IP)
- ``config``   — small filesystem/config helpers shared by the two

By design it carries **no** orchestration, prompts, voice roster, layout
definitions, market-research / forecast pipeline, ad logic, GUI source, server
IP, secrets, or device tokens. See ``tests/test_leak_scan.py`` for the enforced
public client boundary.
"""

from __future__ import annotations

SHELL_VERSION = "0.1.0"
