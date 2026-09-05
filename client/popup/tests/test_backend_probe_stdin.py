"""Regression: the pywebview probe subprocesses must NOT inherit the caller's stdin.

`client/popup/backend.py` probes whether pywebview can open a window by running
`python -c "import webview; webview.initialize()"` (and a bare `import webview`) in a
child process. Those `subprocess.run` calls used to omit `stdin`, so the child inherited
the parent's stdin. When the parent is a serving `python -m installer.shim` whose stdin is
the live MCP JSON-RPC pipe, `webview.initialize()` spawns msedgewebview2.exe helpers that
inherit that pipe handle and the init BLOCKS until the 30s timeout — ~93s across the probes
→ the MCP call times out (-32001) and `open_db_board` returns `no-webview-backend` with no
window. The detached popup child (session.py:119-122) and the pip child (backend.py:87)
already pass `stdin=subprocess.DEVNULL` for exactly this reason; the two probes were missed.

This test locks the invariant: every pywebview PROBE subprocess passes
`stdin=subprocess.DEVNULL`, so it can never inherit (and hang on) the caller's stdin pipe.

Run:  python3 -m unittest client.popup.tests.test_backend_probe_stdin
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from client.popup import backend


class _FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _probe_calls(run_mock) -> list:
    """The recorded subprocess.run calls that are pywebview PROBES (not the pip install)."""
    calls = []
    for call in run_mock.call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and any("import webview" in str(p) for p in cmd):
            calls.append(call)
    return calls


class ProbeStdinTests(unittest.TestCase):
    def test_ready_probe_passes_devnull_stdin(self):
        """webview_backend_ready's probe must not inherit the caller's stdin."""
        with mock.patch.object(backend.subprocess, "run", return_value=_FakeCompleted(0)) as run:
            backend.webview_backend_ready("py")
        probes = _probe_calls(run)
        self.assertEqual(len(probes), 1, "expected exactly one probe call")
        self.assertIs(
            probes[0].kwargs.get("stdin"),
            subprocess.DEVNULL,
            "webview_backend_ready probe must pass stdin=subprocess.DEVNULL, got %r"
            % (probes[0].kwargs.get("stdin"),),
        )

    def test_ensure_webview_import_probe_passes_devnull_stdin(self):
        """ensure_webview runs a `ready` probe then an `import` probe — BOTH must not inherit stdin."""
        # ready-probe returncode 1 → not ready → reach the import probe; import-probe
        # returncode 0 → importable → no pip install (keeps the test hermetic).
        def fake_run(cmd, *a, **kw):
            joined = " ".join(str(p) for p in cmd)
            if "initialize()" in joined:
                return _FakeCompleted(1)   # backend not "ready" → fall through to import probe
            return _FakeCompleted(0)       # bare `import webview` succeeds → skip install

        with mock.patch.object(backend.subprocess, "run", side_effect=fake_run) as run:
            backend.ensure_webview("py", "test")
        probes = _probe_calls(run)
        self.assertGreaterEqual(len(probes), 2, "expected the ready probe AND the import probe")
        for call in probes:
            self.assertIs(
                call.kwargs.get("stdin"),
                subprocess.DEVNULL,
                "every pywebview probe must pass stdin=subprocess.DEVNULL, got %r for cmd %r"
                % (call.kwargs.get("stdin"), call.args[0] if call.args else None),
            )


if __name__ == "__main__":
    unittest.main()
