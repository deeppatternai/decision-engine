"""Regression: `client.runner` must import on Windows, where `fcntl` does not exist.

The board-render path (`client/popup/launcher.fetch_board_html`) lazily does
`from client.runner import USER_AGENT, https_context`. runner.py used to `import fcntl`
at module top; on Windows that raised ModuleNotFoundError, the lazy import failed, and
`open_db_board` surfaced it to the user as a `render-fetch-failed` board popup — even
though the audit transport (which never loads runner.py) worked fine.

The import is exercised in a CHILD process with `fcntl` forced absent, because this test
runner has already imported the real `fcntl` (and runner.py) into its own interpreter.
Injecting a fake `msvcrt` is deliberately avoided here: CPython's `subprocess` decides it
is on Windows by whether `import msvcrt` succeeds, so a stubbed msvcrt would send
subprocess looking for `_winapi`. This test proves the IMPORT no longer hard-depends on
fcntl (the reported bug); the msvcrt lock branch is covered separately below.

Run:  python3 -m unittest installer.tests.test_runner_windows_import
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_child(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter rooted at the repo, returning the completed proc."""
    tmp = tempfile.mkdtemp()
    env = {
        "PATH": "/usr/bin:/bin",
        # A deliberately minimal child environment still needs an explicit home on
        # Windows: pathlib.Path.home() consults USERPROFILE (HOME on POSIX).
        "HOME": tmp,
        "USERPROFILE": tmp,
        "DE_CONFIG_PATH": str(Path(tmp) / "config.json"),        # keep locks out of real $HOME
        "DE_ACTIVE_RUNS": str(Path(tmp) / "active-runs.json"),
        "PYTHONPATH": str(_REPO_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class WindowsImportTests(unittest.TestCase):
    def test_runner_imports_without_fcntl(self):
        """`import fcntl` absent (Windows) → runner still imports and the board-path symbols resolve."""
        proc = _run_child(
            """
            import sys
            sys.modules["fcntl"] = None      # make `import fcntl` raise ImportError (Windows)
            import client.runner as r
            assert r.fcntl is None, r.fcntl
            assert isinstance(r.USER_AGENT, str) and r.USER_AGENT, r.USER_AGENT
            assert callable(r.https_context), r.https_context
            # the advisory locks must still be usable — no fcntl AND no msvcrt → no-op, never a crash
            with r.active_runs_lock():
                pass
            with r.config_activation_lock():
                pass
            print("WINDOWS_IMPORT_OK")
            """
        )
        self.assertEqual(proc.returncode, 0, "child failed:\nSTDOUT:%s\nSTDERR:%s" % (proc.stdout, proc.stderr))
        self.assertIn("WINDOWS_IMPORT_OK", proc.stdout)

    def test_locks_use_msvcrt_when_present(self):
        """No fcntl but a Windows-like msvcrt → the lock acquires/releases via msvcrt.locking."""
        proc = _run_child(
            """
            import sys, types, subprocess   # real subprocess FIRST: CPython detects Windows via a
            sys.modules["fcntl"] = None      # successful `import msvcrt`, so cache it before we stub one
            calls = []
            mv = types.ModuleType("msvcrt")
            mv.LK_LOCK, mv.LK_UNLCK = 1, 0
            mv.locking = lambda fd, mode, n: calls.append((mode, n))
            sys.modules["msvcrt"] = mv
            import client.runner as r
            assert r.fcntl is None and r.msvcrt is mv, (r.fcntl, r.msvcrt)
            with r.active_runs_lock():
                pass
            assert calls == [(1, 1), (0, 1)], calls   # LK_LOCK(1 byte) then LK_UNLCK(1 byte)
            print("MSVCRT_LOCK_OK")
            """
        )
        self.assertEqual(proc.returncode, 0, "child failed:\nSTDOUT:%s\nSTDERR:%s" % (proc.stdout, proc.stderr))
        self.assertIn("MSVCRT_LOCK_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
