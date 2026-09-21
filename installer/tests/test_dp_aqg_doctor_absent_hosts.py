#!/usr/bin/env python3
"""Installer presentation of AQG Doctor results for absent hosts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[2] / "dp-install.sh"


class AqgDoctorAbsentHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        scripts = self.home / "scripts"
        scripts.mkdir()
        (scripts / "aqg_doctor.py").write_text(
            """import json, os, sys
results = [
    {"status": "WARN", "name": "claude_skill_root", "detail": "/fake/.claude/skills does not exist (skills not installed for this agent)", "fix": "install Claude skills"},
    {"status": "WARN", "name": "codex_skill_root", "detail": "/fake/.codex/skills does not exist (skills not installed for this agent)", "fix": "install Codex skills"},
    {"status": "WARN", "name": "cli:gh", "detail": "gh not found in PATH", "fix": "install gh"},
]
if os.environ.get("DOCTOR_FAIL"):
    results.append({"status": "FAIL", "name": "integrity", "detail": "broken", "fix": "repair"})
if "--json" in sys.argv:
    print(json.dumps({"ok": not os.environ.get("DOCTOR_FAIL"), "results": results}))
else:
    print("original Doctor text")
sys.exit(1 if os.environ.get("DOCTOR_FAIL") else 0)
""",
            encoding="utf-8",
        )
        source = INSTALLER.read_text(encoding="utf-8")
        self.function = source.split("run_aqg_install_doctor() {\n", 1)[1].split(
            "\n}\nif ! run_aqg_install_doctor", 1
        )[0]

    def run_doctor(self, clients: str = "", fail: bool = False) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -euo pipefail",
                'clean_exec() { "$@"; }',
                'comma_list_contains() { case ",$1," in *",$2,"*) return 0 ;; *) return 1 ;; esac; }',
                "run_aqg_install_doctor() {" + self.function + "\n}",
                "run_aqg_install_doctor",
            )
        )
        env = os.environ.copy()
        env.update(
            HOME=str(self.home),
            AQG_ROOT=str(self.home),
            PYTHON_BIN=sys.executable,
            PYTHON_DIR=str(Path(sys.executable).parent),
            aqg_selected_clients=clients,
        )
        if fail:
            env["DOCTOR_FAIL"] = "1"
        else:
            env.pop("DOCTOR_FAIL", None)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_no_agents_skips_only_their_missing_skill_roots(self) -> None:
        proc = self.run_doctor()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIP claude_skill_root", proc.stdout)
        self.assertIn("SKIP codex_skill_root", proc.stdout)
        self.assertIn("WARN cli:gh", proc.stdout)
        self.assertIn("Summary: PASS=0 WARN=1 FAIL=0 SKIP=2", proc.stdout)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_present_host_keeps_its_warning(self) -> None:
        proc = self.run_doctor("codex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIP claude_skill_root", proc.stdout)
        self.assertIn("WARN codex_skill_root", proc.stdout)
        self.assertIn("fix: install Codex skills", proc.stdout)

    def test_both_present_preserves_original_doctor_output(self) -> None:
        proc = self.run_doctor("codex,claude-code")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "original Doctor text")

    def test_doctor_failure_still_blocks(self) -> None:
        proc = self.run_doctor(fail=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("FAIL integrity: broken", proc.stdout)
        self.assertIn("Summary: PASS=0 WARN=1 FAIL=1 SKIP=2", proc.stdout)


if __name__ == "__main__":
    unittest.main()
