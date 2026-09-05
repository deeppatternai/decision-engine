from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from client import windows_security


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "audit" / "scripts" / "de_lite_capability_status.py"


class DeLiteCapabilityStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.dp = self.home / ".deeppattern"
        self.config = self.dp / "decision-engine" / "config.json"

    def _run(self) -> dict:
        env = {
            "HOME": str(self.home),
            "DEEPPATTERN_HOME": str(self.dp),
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"status"})
        return payload

    def _write_config(self, payload: str, *, mode: int = 0o600) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            windows_security.harden_private_data_acl(self.config.parent)
        self.config.write_text(payload, encoding="utf-8")
        self.config.chmod(mode)

    def test_missing_or_empty_token_is_unactivated(self) -> None:
        self.assertEqual(self._run(), {"status": "unactivated"})
        self._write_config(json.dumps({"access_token": ""}))
        self.assertEqual(self._run(), {"status": "unactivated"})

    def test_nonempty_token_is_activated_without_echoing_secret(self) -> None:
        self._write_config(json.dumps({"access_token": "owner-secret-token"}))
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={
                "HOME": str(self.home),
                "DEEPPATTERN_HOME": str(self.dp),
                "PATH": os.environ.get("PATH", ""),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"status": "activated"})
        self.assertNotIn("owner-secret-token", result.stdout + result.stderr)

    def test_malformed_or_insecure_config_is_unknown(self) -> None:
        self._write_config("{broken")
        self.assertEqual(self._run(), {"status": "unknown"})
        if os.name != "nt":
            self._write_config(json.dumps({"access_token": "token"}), mode=0o644)
            self.assertEqual(self._run(), {"status": "unknown"})

    @unittest.skipUnless(os.name == "nt", "Windows DACL behavior")
    @unittest.skipUnless(shutil.which("icacls"), "icacls is required for ACL integration")
    def test_world_readable_windows_config_is_unknown(self) -> None:
        self._write_config(json.dumps({"access_token": "token"}))
        granted = subprocess.run(
            ["icacls", str(self.config), "/grant", "*S-1-1-0:(R)"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(granted.returncode, 0)
        self.assertEqual(self._run(), {"status": "unknown"})

    def test_symlinked_config_is_unknown(self) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        target = self.config.parent / "real-config.json"
        target.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
        target.chmod(0o600)
        self.config.symlink_to(target)
        self.assertEqual(self._run(), {"status": "unknown"})


if __name__ == "__main__":
    unittest.main()
