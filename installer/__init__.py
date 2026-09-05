"""Decision Engine public shell (private staging draft).

This package is the *public shell* candidate for the future
``deeppatternai/decision-engine`` source-available repo. It contains only:

- ``install``  — the unified ``install de|aqg|all`` installer
- ``shim``     — an MCP-over-HTTP forwarding shim (transport only, no IP)
- ``config``   — small filesystem/config helpers shared by the two

By design it carries **no** orchestration, prompts, voice roster, layout
definitions, market-research / forecast pipeline, ad logic, GUI source, server
IP, secrets, or device tokens. See ``LEAK_SCAN.md`` and ``tests/test_leak_scan.py``
for the enforced red-line (release design §14).

Staging note: this lives in the private server repo (repo B) as a draft and is
intended to be lifted into a clean-room public repo at P7 (never by copying git
history — release design §11 P7, §14).
"""

from __future__ import annotations

SHELL_VERSION = "0.1.0"
