"""Behavior contracts for the installed Doctor entrypoints."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UNIX_ENTRYPOINT = ROOT / "dp-doctor.sh"
WINDOWS_ENTRYPOINT = ROOT / "dp-doctor.ps1"


@unittest.skipIf(os.name == "nt", "Unix Doctor behavior requires a native Unix host")
class UnixDoctorEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.managed_root = self.home / ".deeppattern" / "decision-engine"
        self.python_root = self.home / ".deeppattern" / "de-python"
        self.capture_args = self.base / "args.txt"
        self.capture_env = self.base / "env.txt"
        (self.managed_root / "installer").mkdir(parents=True)
        (self.managed_root / "installer" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (self.managed_root / "installer" / "doctor.py").write_text(
            "raise SystemExit(99)\n", encoding="utf-8"
        )
        (self.python_root / "bin").mkdir(parents=True)
        (self.python_root / ".deeppattern-python-environment").write_text(
            "schema=1\n", encoding="utf-8"
        )
        fake_python = self.python_root / "bin" / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" >\"$CAPTURE_ARGS\"\n"
            "/usr/bin/env >\"$CAPTURE_ENV\"\n"
            "case \"$*\" in *sys.prefix*) exit 0 ;; esac\n"
            "printf 'fixture doctor\\n'\n"
            "exit \"${DP_DOCTOR_FIXTURE_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)

    def run_entrypoint(self, *args: str, exit_code: str = "0") -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(self.home),
                "CAPTURE_ARGS": str(self.capture_args),
                "CAPTURE_ENV": str(self.capture_env),
                "DP_DOCTOR_FIXTURE_EXIT": exit_code,
                "PYTHONPATH": str(self.base / "hostile-pythonpath"),
                "PYTHONHOME": str(self.base / "hostile-pythonhome"),
                "DE_ENDPOINT": "https://owner.invalid",
                "DE_ACTIVATION_SECRET": "plaintext-secret",
                "GIT_DIR": str(self.base / "hostile-git-dir"),
                "GIT_CONFIG_COUNT": "1",
            }
        )
        hostile_cwd = self.base / "hostile-cwd"
        (hostile_cwd / "installer").mkdir(parents=True, exist_ok=True)
        (hostile_cwd / "installer" / "doctor.py").write_text(
            "raise SystemExit(98)\n", encoding="utf-8"
        )
        return subprocess.run(
            ["bash", str(UNIX_ENTRYPOINT), *args],
            cwd=hostile_cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_runs_managed_doctor_from_any_cwd_and_forwards_json(self) -> None:
        result = self.run_entrypoint("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.capture_args.read_text(encoding="utf-8").splitlines()
        self.assertEqual(arguments[:3], ["-I", "-X", "utf8"])
        self.assertIn("runpy.run_module", arguments[4])
        self.assertNotIn('\\\\"installer.doctor', arguments[4])
        self.assertEqual(arguments[5:], [str(self.managed_root), "--json"])

        environment = self.capture_env.read_text(encoding="utf-8")
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "DE_ENDPOINT",
            "DE_ACTIVATION_SECRET",
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
        ):
            self.assertNotRegex(environment, rf"(?m)^{name}=")
        self.assertRegex(environment, r"(?m)^PYTHONUTF8=1$")
        self.assertRegex(environment, r"(?m)^PYTHONIOENCODING=utf-8$")

    def test_runs_human_report_without_arguments(self) -> None:
        result = self.run_entrypoint()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture doctor", result.stdout)
        arguments = self.capture_args.read_text(encoding="utf-8").splitlines()
        self.assertEqual(arguments[5:], [str(self.managed_root)])

    def test_propagates_doctor_exit_code(self) -> None:
        result = self.run_entrypoint(exit_code="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("fixture doctor", result.stdout)

    def test_real_isolated_python_imports_only_the_managed_doctor(self) -> None:
        shutil.rmtree(self.python_root)
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(self.python_root)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        (self.python_root / ".deeppattern-python-environment").write_text(
            "schema=1\n", encoding="utf-8"
        )
        (self.managed_root / "installer" / "doctor.py").write_text(
            "import json, sys\n"
            "print(json.dumps({'managed': True, 'json': '--json' in sys.argv}))\n",
            encoding="utf-8",
        )

        result = self.run_entrypoint("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"managed": true', result.stdout)
        self.assertIn('"json": true', result.stdout)

    def test_unknown_argument_is_usage_error_before_python(self) -> None:
        result = self.run_entrypoint("--unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(self.capture_args.exists())

    def test_missing_install_has_actionable_error_without_traceback(self) -> None:
        missing_home = self.base / "missing-home"
        result = subprocess.run(
            ["bash", str(UNIX_ENTRYPOINT)],
            env={**os.environ, "HOME": str(missing_home)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("official installer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("runtimes/", result.stderr)

    def test_rejects_symlinked_managed_root_and_module(self) -> None:
        real_root = self.base / "real-root"
        self.managed_root.rename(real_root)
        self.managed_root.symlink_to(real_root, target_is_directory=True)
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a trusted directory", result.stderr)

        self.managed_root.unlink()
        real_root.rename(self.managed_root)
        doctor = self.managed_root / "installer" / "doctor.py"
        real_doctor = self.base / "doctor.py"
        doctor.rename(real_doctor)
        doctor.symlink_to(real_doctor)
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Doctor module", result.stderr)


class WindowsDoctorEntrypointContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WINDOWS_ENTRYPOINT.read_text(encoding="utf-8")

    def test_uses_fixed_managed_paths_and_isolated_module_launch(self) -> None:
        self.assertIn('.deeppattern\\decision-engine', self.source)
        self.assertIn('.deeppattern\\de-python', self.source)
        self.assertIn('Scripts\\python.exe', self.source)
        self.assertIn("-I", self.source)
        self.assertIn("-X", self.source)
        self.assertIn("runpy.run_module", self.source)
        self.assertNotIn("Get-Command python", self.source)
        self.assertNotIn("Set-Location", self.source)

    def test_rejects_reparse_points_and_cleans_inherited_environment(self) -> None:
        self.assertIn("ReparsePoint", self.source)
        self.assertIn('"^(?i:PYTHON)"', self.source)
        self.assertIn("DE_ACTIVATION_SECRET", self.source)
        self.assertIn('"^(?i:GIT_)"', self.source)
        self.assertIn("PYTHONUTF8", self.source)
        self.assertIn("PYTHONIOENCODING", self.source)

    def test_json_and_usage_contract_are_explicit(self) -> None:
        self.assertIn("-Json", self.source)
        self.assertIn("exit 2", self.source)
        self.assertIn("exit $LASTEXITCODE", self.source)


if __name__ == "__main__":
    unittest.main()
