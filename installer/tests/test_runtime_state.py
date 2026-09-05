"""Behavior locks for managed-install runtime state placement and compatibility."""

from __future__ import annotations

import json
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from client import runner


class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "decision-engine"
        self.config = self.root / "config.json"
        self.env = mock.patch.dict(
            os.environ,
            {"DE_CONFIG_PATH": str(self.config)},
            clear=False,
        )
        self.env.start()
        for name in ("DE_ACTIVE_RUN", "DE_ACTIVE_RUNS"):
            os.environ.pop(name, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_managed_defaults_live_below_runtime_directory(self):
        self.assertEqual(runner.runtime_dir(), self.root / ".runtime")
        self.assertEqual(runner.active_run_path(), self.root / ".runtime" / "active-run.json")
        self.assertEqual(runner.active_runs_path(), self.root / ".runtime" / "active-runs.json")
        self.assertEqual(
            runner.config_activation_lock_path(),
            self.root / ".runtime" / "locks" / "config.activation.lock",
        )
        self.assertEqual(
            runner.active_runs_lock_path(),
            self.root / ".runtime" / "locks" / "active-runs.lock",
        )
        self.assertEqual(runner.stopper_log_path(), self.root / ".runtime" / "logs" / "stopper.log")

    def test_either_active_path_override_keeps_both_files_in_the_same_sandbox(self):
        custom = self.root / "test-state" / "custom-runs.json"
        with mock.patch.dict(os.environ, {"DE_ACTIVE_RUNS": str(custom)}, clear=False):
            os.environ.pop("DE_ACTIVE_RUN", None)
            self.assertEqual(runner.active_runs_path(), custom)
            self.assertEqual(runner.active_run_path(), custom.with_name("active-run.json"))
            runner.save_active_run({"run_id": "r1", "status": "queued"})

        self.assertTrue(custom.exists())
        self.assertTrue(custom.with_name("active-run.json").exists())
        self.assertFalse((self.root / ".runtime" / "active-run.json").exists())
        self.assertFalse((self.root / "active-run.json").exists())

    def test_forwarded_managed_defaults_are_not_misclassified_as_user_overrides(self):
        managed_run = self.root / ".runtime" / "active-run.json"
        managed_runs = self.root / ".runtime" / "active-runs.json"
        with mock.patch.dict(
            os.environ,
            {"DE_ACTIVE_RUN": str(managed_run), "DE_ACTIVE_RUNS": str(managed_runs)},
            clear=False,
        ):
            self.assertFalse(runner._active_paths_are_overridden())
            self.assertEqual(
                runner._active_runs_write_paths(),
                (self.root / "active-runs.json", managed_runs),
            )
            self.assertEqual(
                runner.active_runs_lock_path(),
                self.root / ".runtime" / "locks" / "active-runs.lock",
            )

    def test_active_run_only_override_keeps_both_files_in_its_sandbox(self):
        custom = self.root / "other-state" / "custom-run.json"
        with mock.patch.dict(os.environ, {"DE_ACTIVE_RUN": str(custom)}, clear=False):
            os.environ.pop("DE_ACTIVE_RUNS", None)
            self.assertEqual(runner.active_run_path(), custom)
            self.assertEqual(runner.active_runs_path(), custom.with_name("active-runs.json"))
            self.assertTrue(runner._active_paths_are_overridden())


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "decision-engine"
        self.root.mkdir(parents=True)
        self.config = self.root / "config.json"
        self.env = mock.patch.dict(os.environ, {"DE_CONFIG_PATH": str(self.config)}, clear=False)
        self.env.start()
        for name in ("DE_ACTIVE_RUN", "DE_ACTIVE_RUNS"):
            os.environ.pop(name, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _registry(run_id: str, updated_at: float) -> dict:
        return {
            "schema_version": runner.ACTIVE_RUNS_SCHEMA_VERSION,
            "runs": {run_id: {"run_id": run_id, "status": "running"}},
            "updated_at": updated_at,
        }

    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_legacy_registry_is_loaded_before_first_new_layout_write(self):
        self._write(self.root / "active-runs.json", self._registry("legacy", 10.0))

        loaded = runner.load_active_runs_registry()

        self.assertIn("legacy", loaded["runs"])
        self.assertEqual(loaded["schema_version"], runner.ACTIVE_RUNS_SCHEMA_VERSION)

    def test_interrupted_dual_write_recovers_the_newer_valid_copy(self):
        legacy = self.root / "active-runs.json"
        current = self.root / ".runtime" / "active-runs.json"
        for older, newer in ((legacy, current), (current, legacy)):
            with self.subTest(newer=newer):
                self._write(older, self._registry("older", 10.0))
                self._write(newer, self._registry("newer", 20.0))
                loaded = runner.load_active_runs_registry()
                self.assertIn("newer", loaded["runs"])
                self.assertNotIn("older", loaded["runs"])
                older.unlink()
                newer.unlink()

    def test_timestamp_tie_prefers_legacy_only_writer(self):
        legacy = self.root / "active-runs.json"
        current = self.root / ".runtime" / "active-runs.json"
        self._write(legacy, self._registry("legacy-only", 20.0))
        self._write(current, self._registry("stale-primary", 20.0))
        loaded = runner.load_active_runs_registry()
        self.assertIn("legacy-only", loaded["runs"])
        self.assertNotIn("stale-primary", loaded["runs"])

    def test_corrupt_new_copy_falls_back_to_legacy(self):
        self._write(self.root / "active-runs.json", self._registry("legacy", 10.0))
        current = self.root / ".runtime" / "active-runs.json"
        current.parent.mkdir(parents=True)
        current.write_text("{broken", encoding="utf-8")

        loaded = runner.load_active_runs_registry()

        self.assertIn("legacy", loaded["runs"])

    def test_semantically_corrupt_new_copy_falls_back_to_legacy(self):
        self._write(self.root / "active-runs.json", self._registry("legacy", 10.0))
        corrupt = self._registry("lost", 20.0)
        corrupt["runs"] = ["not", "a", "mapping"]
        self._write(self.root / ".runtime" / "active-runs.json", corrupt)

        loaded = runner.load_active_runs_registry()

        self.assertIn("legacy", loaded["runs"])

    def test_non_integer_schema_copy_falls_back_but_newer_schema_fails_closed(self):
        legacy = self.root / "active-runs.json"
        current = self.root / ".runtime" / "active-runs.json"
        self._write(legacy, self._registry("legacy", 10.0))
        malformed = self._registry("malformed", 20.0)
        malformed["schema_version"] = "2"
        self._write(current, malformed)
        self.assertIn("legacy", runner.load_active_runs_registry()["runs"])

        future = self._registry("future", 30.0)
        future["schema_version"] = runner.ACTIVE_RUNS_SCHEMA_VERSION + 1
        self._write(current, future)
        with self.assertRaises(runner.AuditError):
            runner.load_active_runs_registry()

    def test_save_dual_writes_for_previous_stable_and_heals_both_copies(self):
        self._write(self.root / "active-runs.json", self._registry("legacy", 10.0))

        runner.save_active_run({"run_id": "new", "status": "queued"})

        paths = (self.root / "active-runs.json", self.root / ".runtime" / "active-runs.json")
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0]["schema_version"], runner.ACTIVE_RUNS_SCHEMA_VERSION)
        self.assertEqual(set(payloads[0]["runs"]), {"legacy", "new"})
        self.assertTrue((self.root / "active-run.json").exists())
        self.assertTrue((self.root / ".runtime" / "active-run.json").exists())

    def test_compatibility_lock_sets_include_legacy_and_runtime_paths(self):
        self.assertEqual(
            runner.active_runs_lock_paths(),
            (
                self.root / "active-runs.json.lock",
                self.root / ".runtime" / "locks" / "active-runs.lock",
            ),
        )

    def test_clear_removes_both_singular_compatibility_copies(self):
        runner.save_active_run({"run_id": "r1", "status": "queued"})
        runner.clear_active_run("r1")
        self.assertFalse((self.root / "active-run.json").exists())
        self.assertFalse((self.root / ".runtime" / "active-run.json").exists())

    def test_forget_removes_matching_singular_compatibility_copies(self):
        runner.save_active_run({"run_id": "r1", "status": "queued"})

        runner.forget_active_run("r1")

        self.assertFalse((self.root / "active-run.json").exists())
        self.assertFalse((self.root / ".runtime" / "active-run.json").exists())
        self.assertEqual(runner.load_active_runs_registry()["runs"], {})

    def test_forget_does_not_remove_a_different_singular_projection(self):
        runner.save_active_run({"run_id": "r1", "status": "queued"})
        runner.save_active_run({"run_id": "r2", "status": "queued"})

        runner.forget_active_run("r1")

        for path in (self.root / "active-run.json", self.root / ".runtime" / "active-run.json"):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run_id"], "r2")
        self.assertEqual(set(runner.load_active_runs_registry()["runs"]), {"r2"})

    def test_projection_cleanup_refuses_a_linked_copy(self):
        runner.save_active_run({"run_id": "r1", "status": "queued"})
        current = self.root / ".runtime" / "active-run.json"
        target = self.root / "outside.json"
        target.write_text('{"run_id": "r1"}', encoding="utf-8")
        current.unlink()
        try:
            current.symlink_to(target)
        except OSError:
            self.skipTest("file symlinks are unavailable on this host")

        with self.assertRaisesRegex(runner.AuditError, "symlink or reparse point"):
            runner.clear_active_run("r1")

        self.assertTrue(target.exists())

    def test_runtime_path_blocked_by_a_file_has_an_actionable_error(self):
        blocked = self.root / ".runtime"
        blocked.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(runner.AuditError, "not a directory"):
            runner.save_active_run({"run_id": "r1", "status": "queued"})

    def test_managed_runtime_refuses_a_linked_directory(self):
        target = self.root / "redirected"
        target.mkdir()
        runtime = self.root / ".runtime"
        try:
            runtime.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable on this host")
        with self.assertRaisesRegex(runner.AuditError, "symlink or reparse point"):
            runner.save_active_run({"run_id": "r1", "status": "queued"})
        self.assertEqual(list(target.iterdir()), [])

    def test_override_parent_is_not_permission_managed(self):
        custom = self.root / "external-state" / "active-runs.json"
        with mock.patch.dict(os.environ, {"DE_ACTIVE_RUNS": str(custom)}, clear=False), \
             mock.patch.object(runner, "_ensure_private_dir", wraps=runner._ensure_private_dir) as ensure:
            os.environ.pop("DE_ACTIVE_RUN", None)
            runner.save_active_run({"run_id": "r1", "status": "queued"})
        ensure.assert_not_called()

    def test_invalid_or_far_future_copy_cannot_permanently_win_selection(self):
        legacy = self.root / "active-runs.json"
        current = self.root / ".runtime" / "active-runs.json"
        self._write(legacy, self._registry("legacy", 10.0))
        future = self._registry("future", runner.time.time() + 86400.0)
        self._write(current, future)
        self.assertIn("legacy", runner.load_active_runs_registry()["runs"])

        legacy.write_text("{broken", encoding="utf-8")
        current.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(runner.AuditError, "no valid"):
            runner.load_active_runs_registry()

    def test_new_client_waits_for_previous_stable_legacy_lock(self):
        legacy_lock = self.root / "active-runs.json.lock"
        old_code = r"""
import os, sys
from pathlib import Path
path = Path(sys.argv[1]); path.parent.mkdir(parents=True, exist_ok=True)
handle = open(path, "a+")
if os.name == "nt":
    import msvcrt
    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("READY", flush=True)
sys.stdin.readline()
if os.name == "nt":
    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
handle.close()
"""
        new_code = """
from client import runner
print("STARTED", flush=True)
with runner.active_runs_lock():
    print("ACQUIRED", flush=True)
"""
        env = os.environ.copy()
        env["DE_CONFIG_PATH"] = str(self.config)
        env.pop("DE_ACTIVE_RUN", None)
        env.pop("DE_ACTIVE_RUNS", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        old = subprocess.Popen(
            [os.sys.executable, "-c", old_code, str(legacy_lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        new = None
        try:
            self.assertEqual(old.stdout.readline().strip(), "READY")
            new = subprocess.Popen(
                [os.sys.executable, "-c", new_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(new.stdout.readline().strip(), "STARTED")
            with self.assertRaises(subprocess.TimeoutExpired):
                new.wait(timeout=1)
            old.stdin.write("\n")
            old.stdin.flush()
            self.assertEqual(old.wait(timeout=5), 0, old.stderr.read())
            stdout, stderr = new.communicate(timeout=5)
            self.assertEqual(new.returncode, 0, stderr)
            self.assertIn("ACQUIRED", stdout)
        finally:
            for process in (new, old):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process is not None:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    def test_partial_dual_lock_failure_releases_the_first_handle(self):
        first = io.StringIO()
        paths = (self.root / "legacy.lock", self.root / ".runtime" / "locks" / "new.lock")
        with mock.patch.object(runner, "_open_lock_file", side_effect=[first, OSError("second failed")]), \
             mock.patch.object(runner, "_exclusive_file_lock", side_effect=lambda _h: contextlib.nullcontext()):
            with self.assertRaisesRegex(OSError, "second failed"):
                with runner._exclusive_path_locks(paths):
                    pass
        self.assertTrue(first.closed)

    def test_concurrent_clear_keeps_registry_copies_identical(self):
        runner.save_active_run({"run_id": "r1", "status": "queued"})
        runner.save_active_run({"run_id": "r2", "status": "queued"})
        workers = [threading.Thread(target=runner.clear_active_run, args=(run_id,)) for run_id in ("r1", "r2")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        legacy = json.loads((self.root / "active-runs.json").read_text(encoding="utf-8"))
        current = json.loads((self.root / ".runtime" / "active-runs.json").read_text(encoding="utf-8"))
        self.assertEqual(legacy, current)
        self.assertEqual(legacy["runs"], {})
        self.assertEqual(
            runner.config_activation_lock_paths(),
            (
                self.root / "config.json.activation.lock",
                self.root / ".runtime" / "locks" / "config.activation.lock",
            ),
        )
        self.assertEqual(
            runner.stopper_panel_lock_paths(),
            (
                self.root / "stopper-panel.lock",
                self.root / ".runtime" / "locks" / "stopper-panel.lock",
            ),
        )

    def test_activation_lock_paths_can_follow_an_explicit_config(self):
        explicit = self.root / "alternate" / "config.json"

        self.assertEqual(
            runner.config_activation_lock_paths(explicit),
            (
                explicit.with_suffix(".json.activation.lock"),
                explicit.parent / ".runtime" / "locks" / "config.activation.lock",
            ),
        )


@unittest.skipUnless(shutil.which("git"), "Git is required for managed-install behavior")
class RuntimeGitCleanTests(unittest.TestCase):
    def test_generated_runtime_platform_and_editor_files_are_ignored_but_unknown_files_are_visible(self):
        source_ignore = Path(__file__).resolve().parents[2] / ".gitignore"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)
            shutil.copyfile(source_ignore, root / ".gitignore")
            subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, timeout=10)
            subprocess.run(
                [
                    "git", "-C", str(root), "-c", "user.name=Runtime Test",
                    "-c", "user.email=runtime@example.invalid", "commit", "-qm", "baseline",
                ],
                check=True,
                timeout=10,
            )
            generated = (
                "config.json",
                "nested/config.json",
                "active-run.json",
                "active-runs.json",
                "active-runs.json.lock",
                "config.json.activation.lock",
                "stopper-panel.lock",
                "stopper.log",
                ".runtime/active-runs.json",
                "active-run.json.123.tmp",
                "active-runs.json.123.tmp",
                "config.json.123.tmp",
                "config.json.tmp",
                "Thumbs.db",
                "desktop.ini",
                ".DS_Store",
                "scratch.swp",
                "scratch.swo",
                "scratch~",
            )
            for relative in generated:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated", encoding="utf-8")
            (root / "unknown.keep").write_text("user data", encoding="utf-8")
            nested_metadata = (
                "nested/Thumbs.db",
                "nested/desktop.ini",
                "nested/.DS_Store",
                "nested/scratch.swp",
                "nested/scratch.swo",
                "nested/scratch~",
            )
            for relative in nested_metadata:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("user-authored or nested", encoding="utf-8")

            result = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

        reported = {line[3:] for line in result.stdout.splitlines() if line.startswith("?? ")}
        self.assertIn("unknown.keep", reported)
        for relative in generated:
            self.assertNotIn(relative, reported)
        for relative in nested_metadata:
            self.assertIn(relative, reported)


if __name__ == "__main__":
    unittest.main()
