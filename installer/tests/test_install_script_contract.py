"""Static behavior locks for the Python/AQG shell bootstrap boundary.

These assertions intentionally inspect the shell entrypoint: an unsupported or
missing interpreter must be rejected before any Decision Engine Python module
can be imported, so a Python unit test cannot exercise that boundary directly.
"""

from __future__ import annotations

import re
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class InstallScriptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "install.sh").read_text(encoding="utf-8")

    def test_shell_preflight_requires_python_312_before_installer_import(self):
        self.assertIn('DE_MIN_PYTHON_MAJOR="3"', self.script)
        self.assertIn('DE_MIN_PYTHON_MINOR="12"', self.script)
        self.assertIn('DE_PYTHON', self.script)
        self.assertIn('import ssl, venv', self.script)
        self.assertIn('import tkinter', self.script)
        self.assertIn('-m pip --version', self.script)
        preflight = self.script.index("resolve_install_python")
        first_import_match = re.search(
            r"(?m)^(?![ \t]*#)[ \t]*[^\r\n]*-m installer\.",
            self.script,
        )
        self.assertIsNotNone(first_import_match)
        first_installer_import = first_import_match.start()
        self.assertLess(preflight, first_installer_import)

    def test_preflight_rejects_an_externally_managed_interpreter(self):
        """A PEP 668 interpreter passes every other gate and then cannot install anything.

        `pip --version` only proves pip EXISTS. On an externally-managed interpreter
        (Homebrew's python@3.13 is the common one) pip is present and refuses every
        install, so the run gets as far as recording that interpreter and only fails later
        — when a dependency, or the popup backend, cannot be installed into it. The floor
        check must therefore test that pip can INSTALL, not that pip is there.
        """
        preflight = self._resolve_install_python_body()
        self.assertIn("EXTERNALLY-MANAGED", preflight)
        # the remedy has to be actionable: a venv built FROM that interpreter clears it
        self.assertIn("venv", preflight)
        self.assertIn("DE_PYTHON", preflight)
        # and it must be decided before the interpreter is accepted for the whole run
        self.assertLess(
            preflight.index("EXTERNALLY-MANAGED"),
            preflight.index('PYTHON_BIN="${candidate}"'),
        )

    def _resolve_install_python_body(self) -> str:
        start = self.script.index("\nresolve_install_python() {") + 1
        return self.script[start:self.script.index("\n}\n", start)]

    def _externally_managed_probe(self) -> str:
        """The probe expression as install.sh actually spells it.

        Extracted rather than restated so this cannot drift into testing a copy that no
        longer matches the shipped gate.
        """
        match = re.search(
            r"'(import os, sys, sysconfig;[^']*)'", self._resolve_install_python_body()
        )
        self.assertIsNotNone(match, "the externally-managed probe is no longer a one-liner")
        return match.group(1)

    def _probe_exit_code(self, interpreter: str) -> int:
        return subprocess.run(
            [interpreter, "-c", self._externally_managed_probe()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode

    def test_probe_rejects_the_externally_managed_base_it_is_aimed_at(self):
        # `sys._base_executable` rather than base_prefix/bin/python3: a framework build
        # (Homebrew's, the one this gate exists for) does not put the binary at that
        # conventional path, so composing it by hand skips the test where it matters most.
        base = getattr(sys, "_base_executable", None) or sys.executable
        if not Path(base).is_file():
            self.skipTest("base interpreter is not locatable")
        if not (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists():
            self.skipTest("this machine's base Python is not externally managed")
        self.assertEqual(self._probe_exit_code(base), 1)

    def test_probe_accepts_a_real_venv_built_from_that_base(self):
        """The venv is the remedy the gate recommends — it must not reject it.

        A venv reports the BASE stdlib as its own, marker and all, so dropping the
        `sys.prefix == sys.base_prefix` term would make the gate refuse every venv built
        from an externally-managed Python — the exact interpreter the message tells the
        user to create. Only a REAL venv exercises that; a stub answering by argv pattern
        never runs the expression under test.
        """
        if not (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists():
            self.skipTest("this machine's base Python is not externally managed")
        with tempfile.TemporaryDirectory() as tmp:
            venv_root = Path(tmp) / "venv"
            venv.create(venv_root, with_pip=False)
            interpreter = venv_root / "bin" / "python3"
            self.assertTrue(interpreter.is_file())
            # precondition: the venv really does inherit the marker, else this proves nothing
            reported = subprocess.run(
                [str(interpreter), "-c",
                 "import os, sysconfig; print(os.path.exists(os.path.join("
                 "sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED')))"],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(reported, "True")
            self.assertEqual(self._probe_exit_code(str(interpreter)), 0)

    def test_de_flow_checks_aqg_health_before_installing_de(self):
        start = self.script.index("\ninstall_de() {") + 1
        end = self.script.index("\n}\n", start)
        install_de = self.script[start:end]
        expected_order = (
            "ensure_aqg_checkout_and_deps",
            "run_aqg_client_phase apply --apply",
            "run_aqg_client_phase verify --verify",
            "scripts/aqg_doctor.py",
            "install_de_body",
        )
        for earlier, later in zip(expected_order, expected_order[1:]):
            self.assertLess(
                install_de.index(earlier),
                install_de.index(later),
                "expected %s before %s" % (earlier, later),
            )

    def test_an_optional_stopper_agent_failure_does_not_abort_the_install(self):
        """The Stopper LaunchAgent is a convenience: it lets a sandboxed host wake the panel it
        cannot launch itself. Registering it can legitimately fail (locked-down launchd, MDM), and
        aborting there threw away an otherwise COMPLETE install — the user is told to reinstall
        something that already worked. The MCP wiring two lines below already models the right
        shape: warn on stderr, keep going. Same class of defect as the abort that made a
        successful activation exit 134 (f356e07)."""
        for call in re.findall(
            r"^.*installer\.stopper_launch_agent install.*$(?:\n.*)?",
            self.script,
            re.M,
        ):
            self.assertNotIn(
                "|| die", call,
                "an optional step must not abort the install: %s" % call.strip(),
            )
        self.assertIn("could not register the Stopper host LaunchAgent", self.script)
        self.assertIn("installer.stopper_launch_agent install", self.script)

    def test_missing_owner_values_end_with_the_approved_short_message(self):
        self.assertIn(
            "AQG is installed and verified. Decision Engine is not activated.",
            self.script,
        )
        self.assertIn("continue installing DE", self.script)

    def test_global_agents_cleanup_is_a_gated_migration_not_a_writer(self):
        self.assertNotIn("integrations/codex/AGENTS.md", self.script)
        self.assertNotIn("install the Codex routing fragment", self.script)
        self.assertEqual(self.script.count("--require-skills-root"), 2)

        dev = self.script[
            self.script.index("\ninstall_de_dev_mode() {"):
            self.script.index("\ninstall_de_managed() {")
        ]
        with_mcp = dev.index('if [ "${WITH_MCP}" = "1" ]; then')
        without_mcp = dev.index("else", with_mcp)
        cleanup = dev.index("--require-skills-root", with_mcp)
        self.assertLess(cleanup, without_mcp)


if __name__ == "__main__":
    unittest.main()
