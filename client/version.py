"""Single source of truth for the client version.

A LEAF module — no imports, no side effects — so the installer can read the version without pulling
the whole `client.runner` runtime (audit 988bd8e1 convergent 4/4). `client.runner`, `installer.install`,
and `installer.activate` all import `CLIENT_VERSION` from here.

The version the device reports at activation gates the server's version-gated GE byte-isolation
(design §8: bytes are stripped from the model-facing `audit_skill_result` only for clients ≥ 0.2.0).
It is a property of the RUNNING code, not user config — activation reports this constant directly.
"""
from __future__ import annotations

CLIENT_VERSION = "0.2.85"
USER_AGENT = f"decision-engine-client/{CLIENT_VERSION}"
