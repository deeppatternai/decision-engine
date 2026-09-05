"""Repo-wide test isolation: a test must never write into this machine's LIVE client state.

`client.runner.config_path()` resolves `DE_CONFIG_PATH` at CALL time and otherwise falls back to
`~/.deeppattern/decision-engine/config.json`, and every other runtime path hangs off it
(`runtime_dir()` is literally `config_path().parent / ".runtime"`). So a test that exercises a real
write path without patching lands on the developer's ACTIVATED client. Observed, not hypothetical:

  * `test_bootstrap_managed_install.py` (7 tests) rewrites the live `config.json` IN PLACE —
    `server_endpoint` becomes the fixture's `https://example.test`, `device_name` becomes
    `fixture-device`, `device_id` is blanked. Every audit fails until the device is re-activated.
  * `test_stopper_panel.py` appends to `.runtime/logs/stopper.log` and writes the active-runs
    registry + its locks; `test_shim.py` / `test_local_degraded_e2e.py` write the MCP request log.

Because all of it derives from that ONE env var, pointing it at a throwaway directory relocates
every leak at once — which is why this file lives at the repo root and covers both test trees
(`installer/tests`, `client/popup/tests`) rather than being per-suite.

Three things happen here, in order:

  1. The var is set at IMPORT time. pytest loads the root conftest before it imports any test
     module, so even a module that resolves a path at import time is covered.
  2. It is re-pinned before each test, because a teardown elsewhere may pop it (see
     `test_activate.py`, which saves/restores it by hand).
  3. It is GUARDED. If `config_path()` still resolves under the real `~/.deeppattern`, the test
     fails instead of quietly writing there. That guard, not the redirect, is the durable part:
     the next test that forgets to patch turns red here rather than eating someone's config.

Tests that need their own config path keep setting `DE_CONFIG_PATH` themselves — this only supplies
the default they inherit, and their own `mock.patch.dict` restores it afterwards.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# macOS's default temp base is /var/folders/..., and /var is a symlink to /private/var. The client
# refuses any managed root whose path contains a link — managed_install._reject_link_components
# (installer/managed_install.py), cursor_skill_payload, mcp_config — so a plain
# `tempfile.TemporaryDirectory()` root trips those guards before the test reaches what it actually
# asserts. Measured on this repo: 18 tests across five files fail on a Mac and pass on Linux CI for
# that reason alone. Normalising the temp base to its realpath makes a Mac behave like the CI these
# tests were written against. Tests that genuinely exercise link rejection are unaffected: they
# create their own symlink explicitly rather than relying on the ambient one.
_REAL_TMP = os.path.realpath(tempfile.gettempdir())
os.environ["TMPDIR"] = _REAL_TMP + os.sep
tempfile.tempdir = _REAL_TMP  # also override the value tempfile may already have cached

# One throwaway state root per pytest process (xdist workers each get their own).
_SANDBOX = Path(tempfile.mkdtemp(prefix="de-test-state-"))
atexit.register(shutil.rmtree, _SANDBOX, True)

_SANDBOX_CONFIG = str(_SANDBOX / "config.json")

# Deliberately an assignment, not `setdefault`: inheriting a live `DE_CONFIG_PATH` exported in the
# developer's shell is precisely the failure this file exists to prevent.
os.environ["DE_CONFIG_PATH"] = _SANDBOX_CONFIG

_LIVE_STATE_ROOT = (Path.home() / ".deeppattern").resolve()


def _is_live_state(path: Path) -> bool:
    """True if `path` sits inside the real installed-client state root."""
    try:
        resolved = path.resolve()
    except OSError:  # a path we cannot even resolve is not the live root
        return False
    return resolved == _LIVE_STATE_ROOT or _LIVE_STATE_ROOT in resolved.parents


@pytest.fixture(autouse=True)
def _sandboxed_client_state():
    """Re-pin the sandbox default, then refuse to run a test aimed at live client state."""
    if os.environ.get("DE_CONFIG_PATH") != _SANDBOX_CONFIG and not os.environ.get("DE_CONFIG_PATH"):
        os.environ["DE_CONFIG_PATH"] = _SANDBOX_CONFIG
    try:
        from client import runner
    except ImportError:  # a tree/venv where the client package is not importable: redirect still holds
        yield
        return
    resolved = runner.config_path()
    if _is_live_state(resolved):
        pytest.fail(
            "test would write LIVE client state at %s — patch DE_CONFIG_PATH (or "
            "runner.config_path) to a tmp path in this test" % resolved
        )
    yield
