"""The stop panel never appeared for agent-driven audits because the client ships a stopper binary
(`desktop/macos/bin/decision-engine-stopper-arm64`) but the installer never places it at any of the
runtime launch locations (launch agent / .app / ~/.local/bin), so `launch_stopper_if_available` found
nothing and silently no-op'd. The fix adds a last-resort fallback to the shipped-in-body binary.

Run:  python3 -m unittest installer.tests.test_stopper_fallback
"""

from __future__ import annotations

import contextlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import runner


class ShippedStopperBinaryTests(unittest.TestCase):
    """`shipped_stopper_binary()` resolves the prebuilt binary that ships in the client body."""

    def test_resolves_the_committed_repo_binary(self):
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner.platform, "machine", return_value="arm64"):
            p = runner.shipped_stopper_binary()
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "decision-engine-stopper-arm64")
        self.assertEqual(p.parent.as_posix()[-len("desktop/macos/bin"):], "desktop/macos/bin")
        self.assertTrue(p.exists(), p)
        self.assertTrue(os.access(p, os.X_OK), "committed binary must keep its +x bit (git mode 100755)")

    def test_none_off_macos(self):
        with mock.patch.object(runner.platform, "system", return_value="Linux"):
            self.assertIsNone(runner.shipped_stopper_binary())

    def test_the_menu_bar_icon_sits_beside_the_binary(self):
        """The panel loads it with ``Bundle.main.url(forResource: "stopper_icon")``, and Bundle.main
        for a BARE Mach-O executable — which is what ships — is the directory holding the
        executable, with no search upwards. The file used to live one level up in desktop/macos/,
        which the panel could never see, so it silently drew a lettered badge instead and the menu
        bar showed a purple "A" for months.

        Pinned here rather than in the icon suite because the invariant is a RELATIONSHIP to the
        binary: moving either one alone re-breaks it, and nothing else would notice."""
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner.platform, "machine", return_value="arm64"):
            binary = runner.shipped_stopper_binary()
        self.assertIsNotNone(binary)
        icon = binary.parent / "stopper_icon.png"
        self.assertTrue(icon.exists(),
                        "stopper_icon.png must sit beside %s — Bundle.main looks nowhere else"
                        % binary.name)
        self.assertEqual(icon.read_bytes()[:8], b"\x89PNG\r\n\x1a\n", "%s is not a PNG" % icon)


class LaunchFallbackTests(unittest.TestCase):
    """`launch_stopper_if_available` precedence: installed binary first, then the shipped-in-body binary,
    then a silent (now logged) no-op. All tests force Darwin + stub the earlier launch paths + stub Popen,
    so nothing is actually spawned and the tests are platform-independent."""

    def _run(self, *, installed: Path, shipped, env: dict):
        """Drive launch_stopper_if_available with the two launch precursors forced False, a controlled
        installed-binary path + shipped resolver, and Popen/config stubbed. Returns the recorded Popen
        args list (or None if Popen was never called)."""
        recorded = {"args": None}

        def fake_popen(args, **kwargs):
            recorded["args"] = args
            return mock.Mock()

        cm = [
            mock.patch.object(runner.platform, "system", return_value="Darwin"),
            mock.patch.object(runner, "kickstart_stopper_launch_agent", return_value=False),
            mock.patch.object(runner, "launch_stopper_app", return_value=False),
            mock.patch.object(runner, "stopper_binary_path", return_value=installed),
            mock.patch.object(runner, "shipped_stopper_binary", return_value=shipped),
            mock.patch.object(runner.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(runner, "config_path", return_value=Path(_tmp_cfg())),
            mock.patch.object(runner, "active_run_path", return_value=Path("/tmp/de-active-run.json")),
            mock.patch.object(runner, "active_runs_path", return_value=Path("/tmp/de-active-runs.json")),
            mock.patch.object(
                runner, "active_runs_lock_path", return_value=Path("/tmp/de-active-runs.lock")
            ),
            mock.patch.object(
                runner,
                "active_runs_lock_paths",
                return_value=(Path("/tmp/legacy.lock"), Path("/tmp/de-active-runs.lock")),
            ),
            mock.patch.object(
                runner,
                "_active_runs_write_paths",
                return_value=(Path("/tmp/legacy.json"), Path("/tmp/de-active-runs.json")),
            ),
            mock.patch.object(
                runner,
                "_active_run_write_paths",
                return_value=(Path("/tmp/legacy-one.json"), Path("/tmp/de-active-run.json")),
            ),
            mock.patch.dict(os.environ, env, clear=False),
        ]
        with contextlib.ExitStack() as stack:
            for c in cm:
                stack.enter_context(c)
            if "DE_SKIP_STOPPER_LAUNCH" not in env:
                os.environ.pop("DE_SKIP_STOPPER_LAUNCH", None)  # restored when the patched dict exits
            runner.launch_stopper_if_available()
        return recorded["args"]

    def test_prefers_installed_binary_when_present(self):
        # an installed, executable ~/.local/bin binary must win over the shipped fallback (regression lock)
        with tempfile.TemporaryDirectory() as d:
            installed = Path(d) / "decision-engine-stopper"
            installed.write_bytes(b"#!/bin/sh\n")
            installed.chmod(0o755)
            shipped = Path(d) / "shipped-should-not-be-used"
            shipped.write_bytes(b"#!/bin/sh\n")
            shipped.chmod(0o755)
            args = self._run(installed=installed, shipped=shipped, env={})
        self.assertEqual(args, [str(installed)], "installed binary must take precedence over shipped")

    def test_falls_back_to_shipped_when_installed_absent(self):
        with tempfile.TemporaryDirectory() as d:
            shipped = Path(d) / "decision-engine-stopper-arm64"
            shipped.write_bytes(b"#!/bin/sh\n")
            shipped.chmod(0o755)
            args = self._run(installed=Path("/nonexistent/decision-engine-stopper"),
                             shipped=shipped, env={})
        self.assertEqual(args, [str(shipped)], "must spawn the shipped binary when nothing is installed")

    def test_no_spawn_when_nothing_available(self):
        args = self._run(installed=Path("/nonexistent/decision-engine-stopper"), shipped=None, env={})
        self.assertIsNone(args, "no installed and no shipped binary → no spawn")

    def test_skip_env_short_circuits(self):
        # force Darwin + stub the launch precursors so the ONLY thing that can prevent a spawn is the
        # DE_SKIP_STOPPER_LAUNCH short-circuit (otherwise this passes for the wrong reason off-Darwin).
        with tempfile.TemporaryDirectory() as d:
            installed = Path(d) / "decision-engine-stopper"
            installed.write_bytes(b"#!/bin/sh\n")
            installed.chmod(0o755)
            args = self._run(installed=installed, shipped=None, env={"DE_SKIP_STOPPER_LAUNCH": "1"})
        self.assertIsNone(args, "DE_SKIP_STOPPER_LAUNCH=1 must prevent any spawn even with a binary present")

    def test_runtime_env_exports_the_runner_selected_lock_path(self):
        with mock.patch.object(
            runner, "active_runs_lock_path", return_value=Path("/tmp/exact-runner.lock")
        ), mock.patch.object(
            runner,
            "active_runs_lock_paths",
            return_value=(Path("/tmp/legacy.lock"), Path("/tmp/exact-runner.lock")),
        ):
            env = runner._stopper_runtime_env()
        self.assertEqual(Path(env["DE_ACTIVE_RUNS_LOCK"]), Path("/tmp/exact-runner.lock"))
        self.assertEqual(
            [Path(path) for path in json.loads(env["DE_ACTIVE_RUNS_LOCK_PATHS"])],
            [Path("/tmp/legacy.lock"), Path("/tmp/exact-runner.lock")],
        )


def _tmp_cfg() -> str:
    d = tempfile.mkdtemp(prefix="de-stopper-test-")
    return os.path.join(d, "config.json")



class MenuBarIconShape(unittest.TestCase):
    """The menu-bar image is the one asset that is deliberately NOT square."""

    def _dimensions(self):
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner.platform, "machine", return_value="arm64"):
            binary = runner.shipped_stopper_binary()
        icon = binary.parent / "stopper_icon.png"
        head = icon.read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n", "%s is not a PNG" % icon
        return struct.unpack(">II", head[16:24])

    def test_it_is_wider_than_tall(self):
        """Square means the braces were dropped — the small-size variant — which is exactly what
        the menu bar used to show. A menu bar constrains height only, so the framed 1.5:1 mark
        costs nothing there and squaring it would shrink the brain by a third for empty margin."""
        width, height = self._dimensions()
        self.assertGreater(width / height, 1.3,
                           "the menu-bar icon is square — the framing braces are missing")

    def test_it_is_drawn_at_about_twice_its_point_size(self):
        """Both readers load it by explicit path, so neither picks up an @2x companion; the one
        bitmap has to carry Retina on its own. Drawn at 18pt."""
        _width, height = self._dimensions()
        self.assertGreaterEqual(height, 32, "too few pixels to stay crisp at 18pt on Retina")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
