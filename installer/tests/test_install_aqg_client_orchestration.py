"""Locks observable install.sh behavior for the AQG client wrapper orchestration.

Slice A+B (DE-027): a healthy AQG checkout must still trigger the AQG client
adapter wrapper before DE core installs, in strict prepare -> apply -> verify
-> Doctor -> DE core order. The pre-apply Doctor gate that previously blocked
an unhealthy/fresh host surface from ever reaching apply has been replaced by
a post-verify Doctor gate: apply/verify must not be blocked by an unhealthy
host surface, but a Doctor failure after them must still fail closed before
DE core runs. See requirements R27-01..R27-08.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"

FAKE_PYTHON_SCRIPT = r"""#!/usr/bin/env bash
set -u
args="$*"
event_file="${EVENT_FILE:?EVENT_FILE not set}"
case "$args" in
  *aqg_doctor.py*)
    echo "doctor" >> "$event_file"
    exit "${FAKE_DOCTOR_RC:-0}"
    ;;
  *install_aqg_clients.py*--apply*)
    echo "apply" >> "$event_file"
    {
      printf "apply"
      printf "\t%s" "$@"
      printf "\n"
    } >> "${ARGV_FILE:?ARGV_FILE not set}"
    exit "${FAKE_APPLY_RC:-0}"
    ;;
  *install_aqg_clients.py*--verify*)
    echo "verify" >> "$event_file"
    {
      printf "verify"
      printf "\t%s" "$@"
      printf "\n"
    } >> "${ARGV_FILE:?ARGV_FILE not set}"
    exit "${FAKE_VERIFY_RC:-0}"
    ;;
  *-m\ installer.install*)
    echo "de-core" >> "$event_file"
    if [ -n "${FAKE_DE_CORE_MANAGED_ROOT:-}" ]; then
      mkdir -p "${FAKE_DE_CORE_MANAGED_ROOT}"
    fi
    exit 0
    ;;
  *installer.activate*--from-env*)
    echo "activate" >> "$event_file"
    exit "${FAKE_ACTIVATE_RC:-0}"
    ;;
  *from\ installer\ import\ activate,\ config*)
    echo "activate-probe" >> "$event_file"
    exit "${FAKE_ACTIVATED_RC:-1}"
    ;;
  *)
    exit 0
    ;;
esac
"""


def _bash_executable():
    resolved = shutil.which("bash")
    if resolved:
        return resolved
    raise unittest.SkipTest("bash executable not found on PATH")


def _bash_path(path):
    resolved = Path(path).resolve()
    if os.name != "nt":
        return str(resolved)
    return "/%s%s" % (resolved.drive[0].lower(), resolved.as_posix()[2:])


def _bash_script_command(script, fake_bin, target):
    return [
        _bash_executable(),
        "-c",
        'PATH="$1:$PATH"; exec "$2" "$3"',
        "install-test",
        _bash_path(fake_bin),
        _bash_path(script),
        target,
    ]


def _make_usable_aqg_dest(base, *, include_wrapper=True):
    aqg_dest = base / "aqg dest"
    (aqg_dest / ".git").mkdir(parents=True)
    scripts = aqg_dest / "scripts"
    scripts.mkdir()
    (scripts / "install.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (scripts / "aqg_doctor.py").write_text(
        "# fake doctor placeholder, never executed directly\n"
    )
    if include_wrapper:
        (scripts / "install_aqg_clients.py").write_text(
            "# fake wrapper placeholder, never executed directly\n"
        )
    (aqg_dest / "requirements.txt").write_text("")
    return aqg_dest


class InstallAqgClientOrchestrationTest(unittest.TestCase):
    """Exercises the real install.sh to lock the AQG client wrapper contract."""

    def _run_install(
        self,
        *,
        doctor_rc=0,
        apply_rc=0,
        verify_rc=0,
        target="de",
        include_wrapper=True,
        git_file=False,
    ):
        tmp = Path(tempfile.mkdtemp(prefix="aqg-orch-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        home = tmp / "home"
        home.mkdir()
        aqg_dest = _make_usable_aqg_dest(tmp, include_wrapper=include_wrapper)
        if git_file:
            (aqg_dest / ".git").rmdir()
            (aqg_dest / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
        fake_bin = tmp / "fake-bin"
        fake_bin.mkdir()

        fake_python = tmp / "fake-python"
        fake_python.write_text(FAKE_PYTHON_SCRIPT)
        fake_python.chmod(0o755)

        event_file = tmp / "events.log"
        event_file.write_text("")
        argv_file = tmp / "wrapper-argv.log"
        argv_file.write_text("")

        env = dict(os.environ)
        env.pop("DE_ENDPOINT", None)
        env.pop("DE_ACTIVATION_SECRET", None)
        env.update(
            {
                "HOME": _bash_path(home),
                "AQG_DEST": _bash_path(aqg_dest),
                "WITH_AQG": "1",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_PYTHON": _bash_path(fake_python),
                "EVENT_FILE": _bash_path(event_file),
                "ARGV_FILE": _bash_path(argv_file),
                "FAKE_DOCTOR_RC": str(doctor_rc),
                "FAKE_APPLY_RC": str(apply_rc),
                "FAKE_VERIFY_RC": str(verify_rc),
            }
        )

        cmd = _bash_script_command(INSTALL_SH, fake_bin, target)
        completed = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=30
        )
        events = [line for line in event_file.read_text().splitlines() if line]
        invocations = [
            line.split("\t") for line in argv_file.read_text().splitlines() if line
        ]
        return completed, events, invocations

    def test_updated_aqg_worktree_is_reused_without_cloning(self):
        completed, events, _ = self._run_install(git_file=True)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(events, ["apply", "verify", "doctor", "de-core"])
        self.assertNotIn("cloning", completed.stderr.lower())

    def test_r27_01_and_02_healthy_checkout_still_runs_wrapper_in_strict_order(self):
        completed, events, invocations = self._run_install(
            doctor_rc=0, apply_rc=0, verify_rc=0
        )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn(
            "apply",
            events,
            "a healthy AQG Doctor check must not skip the AQG client wrapper apply step",
        )
        self.assertIn(
            "verify",
            events,
            "a healthy AQG Doctor check must not skip the AQG client wrapper verify step",
        )
        self.assertEqual(
            events,
            ["apply", "verify", "doctor", "de-core"],
            "expected strict apply -> verify -> Doctor -> DE core order, got %r"
            % (events,),
        )
        script = str(PurePosixPath(invocations[0][1]))
        base = PurePosixPath(script).parents[2]
        common = ["--home", str(base / "home"), "--aqg-root", str(base / "aqg dest")]
        self.assertEqual(
            invocations,
            [
                ["apply", script, "--installed-supported", "--apply", *common],
                ["verify", script, "--installed-supported", "--verify", *common],
            ],
        )

    def test_r27_03_no_supported_client_is_treated_as_successful_noop(self):
        for apply_rc, verify_rc in ((3, 3), (3, 0), (0, 3)):
            with self.subTest(apply_rc=apply_rc, verify_rc=verify_rc):
                completed, events, _ = self._run_install(
                    doctor_rc=0, apply_rc=apply_rc, verify_rc=verify_rc
                )
                self.assertEqual(completed.returncode, 0, msg=completed.stderr)
                self.assertIn(
                    "de-core",
                    events,
                    "exit 3 (no detected supported host) must be a no-op success, "
                    "DE core must still run",
                )
                self.assertIn("no supported Agent host", completed.stdout)
                self.assertIn("no-op", completed.stdout)

    def test_r27_04_apply_failure_is_propagated_and_blocks_de_core(self):
        for apply_rc in (2, 4, 5):
            with self.subTest(apply_rc=apply_rc):
                completed, events, _ = self._run_install(
                    doctor_rc=0, apply_rc=apply_rc, verify_rc=0
                )
                self.assertEqual(
                    completed.returncode,
                    apply_rc,
                    "wrapper apply exit %d must be propagated unchanged" % apply_rc,
                )
                self.assertNotIn("verify", events)
                self.assertNotIn(
                    "de-core",
                    events,
                    "DE core must not run when the AQG client wrapper apply step fails",
                )
                stderr_lower = completed.stderr.lower()
                self.assertIn("apply", stderr_lower, completed.stderr)
                self.assertIn("install_aqg_clients.py", completed.stderr)
                self.assertIn("exit %d" % apply_rc, completed.stderr)
                self.assertIn("retry with:", completed.stderr)
                self.assertIn("--installed-supported --apply", completed.stderr)
                self.assertIn("--home", completed.stderr)
                self.assertIn("--aqg-root", completed.stderr)

    def test_r27_05_verify_failure_is_propagated_and_blocks_de_core(self):
        for verify_rc in (2, 4, 5):
            with self.subTest(verify_rc=verify_rc):
                completed, events, _ = self._run_install(
                    doctor_rc=0, apply_rc=0, verify_rc=verify_rc
                )
                self.assertEqual(
                    completed.returncode,
                    verify_rc,
                    "wrapper verify exit %d must be propagated unchanged" % verify_rc,
                )
                self.assertIn("apply", events)
                self.assertIn("verify", events)
                self.assertNotIn(
                    "de-core",
                    events,
                    "DE core must not run when the AQG client wrapper verify step fails",
                )
                stderr_lower = completed.stderr.lower()
                self.assertIn("verify", stderr_lower, completed.stderr)
                self.assertIn("install_aqg_clients.py", completed.stderr)
                self.assertIn("exit %d" % verify_rc, completed.stderr)
                self.assertIn("retry with:", completed.stderr)
                self.assertIn("--installed-supported --verify", completed.stderr)
                self.assertIn("--home", completed.stderr)
                self.assertIn("--aqg-root", completed.stderr)

    def test_r27_05_missing_wrapper_fails_closed_with_safe_recovery_command(self):
        completed, events, invocations = self._run_install(include_wrapper=False)

        self.assertEqual(completed.returncode, 1, msg=completed.stderr)
        self.assertEqual(
            events,
            [],
            "preparing/reusing the AQG checkout must not run Doctor as a "
            "pre-apply gate; the missing wrapper must fail before any Doctor "
            "or wrapper child runs",
        )
        self.assertEqual(invocations, [])
        self.assertIn("install_aqg_clients.py", completed.stderr)
        self.assertIn("git -C", completed.stderr)
        self.assertIn("pull --ff-only", completed.stderr)
        self.assertIn(r"aqg\ dest", completed.stderr)
        self.assertNotIn("de-core", events)

    def test_r27_02_unhealthy_host_surface_does_not_block_apply_or_verify(self):
        # A fresh/unhealthy host surface (e.g. a first-time host missing AQG
        # skills/hooks) must not gate the client wrapper apply/verify steps —
        # only the post-verify Doctor gate may fail closed. See the Slice B
        # deployment backfill note in the ledger (R27-02, R27-06).
        completed, events, invocations = self._run_install(
            doctor_rc=1, apply_rc=0, verify_rc=0
        )

        self.assertIn(
            "apply",
            events,
            "an unhealthy/missing host surface must not prevent installed-"
            "supported apply from running",
        )
        self.assertIn(
            "verify",
            events,
            "an unhealthy/missing host surface must not prevent installed-"
            "supported verify from running",
        )
        self.assertEqual(
            len(invocations),
            2,
            "apply and verify must both actually invoke the wrapper despite "
            "the unhealthy pre-existing Doctor state",
        )

    def test_r27_05_doctor_failure_blocks_wrapper_and_de_core(self):
        completed, events, _ = self._run_install(doctor_rc=1, apply_rc=0, verify_rc=0)

        self.assertNotEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(
            events,
            ["apply", "verify", "doctor"],
            "apply and verify must run before the post-verify Doctor gate; "
            "a Doctor failure there must still block DE core, got %r"
            % (events,),
        )
        self.assertNotIn(
            "de-core",
            events,
            "DE core must not run when the post-verify Doctor gate fails",
        )
        stderr_lower = completed.stderr.lower()
        self.assertIn("doctor", stderr_lower)
        self.assertIn("retry", stderr_lower)
        self.assertIn("decision engine was not installed", stderr_lower)

    def _make_scenario(self, *, include_wrapper=True):
        tmp = Path(tempfile.mkdtemp(prefix="aqg-orch-b-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        home.mkdir()
        aqg_dest = _make_usable_aqg_dest(tmp, include_wrapper=include_wrapper)
        fake_bin = tmp / "fake-bin"
        fake_bin.mkdir()
        fake_python = tmp / "fake-python"
        fake_python.write_text(FAKE_PYTHON_SCRIPT)
        fake_python.chmod(0o755)
        return tmp, home, aqg_dest, fake_bin, fake_python

    def _run_install_in_scenario(
        self,
        tmp,
        home,
        aqg_dest,
        fake_bin,
        fake_python,
        *,
        run_id,
        doctor_rc=0,
        apply_rc=0,
        verify_rc=0,
        target="de",
        extra_env=None,
    ):
        event_file = tmp / ("events-%s.log" % run_id)
        event_file.write_text("")
        argv_file = tmp / ("wrapper-argv-%s.log" % run_id)
        argv_file.write_text("")

        env = dict(os.environ)
        env.pop("DE_ENDPOINT", None)
        env.pop("DE_ACTIVATION_SECRET", None)
        env.update(
            {
                "HOME": str(home),
                "AQG_DEST": _bash_path(aqg_dest),
                "WITH_AQG": "1",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_PYTHON": _bash_path(fake_python),
                "EVENT_FILE": _bash_path(event_file),
                "ARGV_FILE": _bash_path(argv_file),
                "FAKE_DOCTOR_RC": str(doctor_rc),
                "FAKE_APPLY_RC": str(apply_rc),
                "FAKE_VERIFY_RC": str(verify_rc),
            }
        )
        if extra_env:
            env.update(extra_env)

        cmd = _bash_script_command(INSTALL_SH, fake_bin, target)
        completed = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=30
        )
        events = [line for line in event_file.read_text().splitlines() if line]
        invocations = [
            line.split("\t") for line in argv_file.read_text().splitlines() if line
        ]
        return completed, events, invocations

    def test_r27_06_unactivated_first_install_reports_honest_state(self):
        tmp, home, aqg_dest, fake_bin, fake_python = self._make_scenario()
        managed_root = home / ".deeppattern" / "decision-engine"
        completed, events, invocations = self._run_install_in_scenario(
            tmp,
            home,
            aqg_dest,
            fake_bin,
            fake_python,
            run_id="only",
            extra_env={"FAKE_DE_CORE_MANAGED_ROOT": _bash_path(managed_root)},
        )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(
            events,
            ["apply", "verify", "doctor", "de-core", "activate-probe"],
            "the activation-state probe must actually run against the synthetic "
            "managed root and answer unactivated, not short-circuit on a "
            "missing directory",
        )
        self.assertNotIn("activate", events)
        self.assertIn("AQG is installed and verified", completed.stdout)
        self.assertIn("not activated", completed.stdout)
        combined = (
            completed.stdout
            + completed.stderr
            + "".join(arg for invocation in invocations for arg in invocation)
        )
        self.assertNotIn("DE_ACTIVATION_SECRET", combined)
        self.assertNotIn("synthetic-owner-value", combined)

    def test_r27_07_repeated_formal_install_reruns_aqg_phases(self):
        # This only locks that install.sh's own orchestration always re-runs the
        # AQG host apply/verify phases rather than skipping them because Doctor
        # reports healthy; whether AQG's or DE's own internals are idempotent
        # component-by-component is covered by the existing AQG and DE suites.
        tmp, home, aqg_dest, fake_bin, fake_python = self._make_scenario()
        sentinel = home / "third-party-sentinel.txt"
        sentinel.write_text("keep-me")

        for run_id in ("first", "second"):
            completed, events, _ = self._run_install_in_scenario(
                tmp,
                home,
                aqg_dest,
                fake_bin,
                fake_python,
                run_id=run_id,
                target="all",
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stderr)
            self.assertEqual(events, ["apply", "verify", "doctor", "de-core"])
            self.assertNotIn("activate", events)

        self.assertEqual(sentinel.read_text(), "keep-me")

    def test_r27_08_failed_phase_then_explicit_retry_completes(self):
        for phase, rc in (("apply", 4), ("verify", 5)):
            with self.subTest(phase=phase, rc=rc):
                tmp, home, aqg_dest, fake_bin, fake_python = self._make_scenario()
                sentinel = home / "third-party-sentinel.txt"
                sentinel.write_text("keep-me")
                apply_rc = rc if phase == "apply" else 0
                verify_rc = rc if phase == "verify" else 0

                completed, events, _ = self._run_install_in_scenario(
                    tmp,
                    home,
                    aqg_dest,
                    fake_bin,
                    fake_python,
                    run_id="fail",
                    apply_rc=apply_rc,
                    verify_rc=verify_rc,
                )
                self.assertEqual(completed.returncode, rc, msg=completed.stderr)
                self.assertNotIn("de-core", events)
                self.assertNotIn("activate", events)
                if phase == "apply":
                    self.assertNotIn("verify", events)

                # The retry is the user's explicit second invocation: a brand
                # new process against the SAME scenario. install.sh must not
                # auto-retry a failed phase itself.
                completed2, events2, _ = self._run_install_in_scenario(
                    tmp,
                    home,
                    aqg_dest,
                    fake_bin,
                    fake_python,
                    run_id="retry",
                    apply_rc=0,
                    verify_rc=0,
                )
                self.assertEqual(completed2.returncode, 0, msg=completed2.stderr)
                self.assertEqual(events2, ["apply", "verify", "doctor", "de-core"])
                self.assertNotIn("activate", events2)
                self.assertEqual(sentinel.read_text(), "keep-me")


if __name__ == "__main__":
    unittest.main()
