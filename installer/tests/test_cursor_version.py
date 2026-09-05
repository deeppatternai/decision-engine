from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path, PureWindowsPath
from unittest import mock

from installer import client_host_version, cursor_version, doctor


class ClientHostVersionTests(unittest.TestCase):
    def _evidence(
        self,
        *,
        selected: str | None,
        installed: tuple[str, ...],
        installations: int | None = None,
        complete: bool = True,
    ) -> client_host_version.HostVersionEvidence:
        return client_host_version.HostVersionEvidence(
            selected=(
                client_host_version.NumericVersion.parse(selected)
                if selected is not None
                else None
            ),
            installed=tuple(
                client_host_version.NumericVersion.parse(value)
                for value in installed
            ),
            installations=(
                len(installed) if installations is None else installations
            ),
            scope_complete=complete,
        )

    def test_candidate_floor_requires_complete_selected_and_installed_evidence(self):
        minimum = client_host_version.NumericVersion.parse("2.4")

        assessment = client_host_version.assess_version_floor(
            self._evidence(
                selected="3.3.30",
                installed=("3.3.30", "3.5.38"),
                installations=2,
            ),
            minimum,
        )

        self.assertEqual(assessment.state, "floor_met")

    def test_any_installed_version_below_floor_is_unsupported(self):
        assessment = client_host_version.assess_version_floor(
            self._evidence(
                selected="3.5.38",
                installed=("2.3.9", "3.5.38"),
                installations=2,
            ),
            client_host_version.NumericVersion.parse("2.4"),
        )

        self.assertEqual(assessment.state, "unsupported")

    def test_incomplete_or_unmatched_selected_evidence_is_unknown(self):
        minimum = client_host_version.NumericVersion.parse("2.4")
        cases = (
            self._evidence(
                selected=None,
                installed=("3.5.38",),
            ),
            self._evidence(
                selected="3.3.30",
                installed=("3.5.38",),
            ),
            self._evidence(
                selected="3.5.38",
                installed=("3.5.38",),
                complete=False,
            ),
        )

        for evidence in cases:
            with self.subTest(evidence=evidence):
                assessment = client_host_version.assess_version_floor(
                    evidence,
                    minimum,
                )
                self.assertEqual(assessment.state, "unknown")

    def test_known_below_floor_wins_over_incomplete_scope(self):
        assessment = client_host_version.assess_version_floor(
            self._evidence(
                selected="2.3.9",
                installed=("2.3.9",),
                complete=False,
            ),
            client_host_version.NumericVersion.parse("2.4"),
        )

        self.assertEqual(assessment.state, "unsupported")

    def test_inconsistent_installation_count_is_unknown(self):
        assessment = client_host_version.assess_version_floor(
            self._evidence(
                selected="3.5.38",
                installed=("3.3.30", "3.5.38"),
                installations=1,
            ),
            client_host_version.NumericVersion.parse("2.4"),
        )

        self.assertEqual(assessment.state, "unknown")

    def test_numeric_version_parser_rejects_ambiguous_or_unbounded_values(self):
        for value in (
            "",
            "2",
            "2.4.0" + ".1",
            "2.4-beta",
            "v2.4",
            "02.4",
            "2.04",
            "2.4\nprivate",
            "9999999.4",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    client_host_version.NumericVersion.parse(value)


class CursorVersionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _install(
        self,
        name: str,
        version: str,
        *,
        launcher_name: str = "cursor.cmd",
    ) -> tuple[Path, Path]:
        app_root = self.base / name / "resources" / "app"
        bin_dir = app_root / "bin"
        bin_dir.mkdir(parents=True)
        (app_root / "package.json").write_text(
            json.dumps({"name": "Cursor", "version": version}),
            encoding="utf-8",
        )
        launcher = bin_dir / launcher_name
        launcher.write_text("@echo off\n", encoding="utf-8")
        return app_root, launcher

    @unittest.skipUnless(os.name == "nt", "Windows fixed-root evidence")
    def test_two_known_installs_and_selected_cli_are_read_without_execution(self):
        user_root, selected = self._install("user", "3.3.30")
        system_root, _system_launcher = self._install("system", "3.5.38")
        before = {
            path.relative_to(self.base): path.read_bytes()
            for path in self.base.rglob("*")
            if path.is_file()
        }

        with mock.patch("subprocess.Popen") as popen, mock.patch(
            "subprocess.run"
        ) as run:
            evidence = cursor_version.inspect_cursor_version_evidence(
                (user_root, system_root),
                user_root,
            )

        self.assertEqual(str(evidence.selected), "3.3.30")
        self.assertEqual(
            tuple(str(version) for version in evidence.installed),
            ("3.3.30", "3.5.38"),
        )
        self.assertEqual(evidence.installations, 2)
        self.assertTrue(evidence.scope_complete)
        self.assertEqual(
            {
                path.relative_to(self.base): path.read_bytes()
                for path in self.base.rglob("*")
                if path.is_file()
            },
            before,
        )
        popen.assert_not_called()
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows fixed-root evidence")
    def test_unknown_selected_launcher_fails_closed(self):
        app_root, _launcher = self._install("user", "3.5.38")
        outside = self.base / "private-install" / "resources" / "app"
        outside.mkdir(parents=True)

        evidence = cursor_version.inspect_cursor_version_evidence(
            (app_root,),
            outside,
        )
        assessment = cursor_version.assess_cursor_version(evidence)

        self.assertFalse(evidence.scope_complete)
        self.assertIsNone(evidence.selected)
        self.assertEqual(assessment.state, "cursor_version_unknown")
        self.assertNotIn(str(outside), assessment.detail)

    @unittest.skipUnless(os.name == "nt", "Windows fixed-root evidence")
    def test_malformed_and_oversized_package_metadata_fail_closed_without_leak(self):
        selected_root, selected = self._install("selected", "3.5.38")
        invalid_root, _launcher = self._install("invalid", "3.4.0")
        secret = "private-package-value"
        (invalid_root / "package.json").write_bytes(
            b'{"name":"Cursor","version":"3.4.0","private":"'
            + secret.encode("ascii")
            + b'x' * cursor_version.MAX_CURSOR_PACKAGE_BYTES
        )

        evidence = cursor_version.inspect_cursor_version_evidence(
            (selected_root, invalid_root),
            selected_root,
        )
        assessment = cursor_version.assess_cursor_version(evidence)

        self.assertFalse(evidence.scope_complete)
        self.assertEqual(assessment.state, "cursor_version_unknown")
        self.assertNotIn(secret, assessment.detail)
        self.assertNotIn(str(invalid_root), assessment.detail)

    @unittest.skipUnless(os.name == "nt", "Windows fixed-root evidence")
    def test_ambiguous_package_metadata_is_not_version_evidence(self):
        payloads = {
            "malformed": b'{"name":"Cursor","version":',
            "duplicate": (
                b'{"name":"Cursor","version":"3.5.38",'
                b'"version":"9.9.9"}'
            ),
            "wrong-product": b'{"name":"Private","version":"3.5.38"}',
            "non-numeric": b'{"name":"Cursor","version":"3.5-beta"}',
        }

        for label, payload in payloads.items():
            with self.subTest(label=label):
                app_root, selected = self._install(label, "3.5.38")
                (app_root / "package.json").write_bytes(payload)

                evidence = cursor_version.inspect_cursor_version_evidence(
                    (app_root,),
                    app_root,
                )

                self.assertFalse(evidence.scope_complete)
                self.assertEqual(evidence.installed, ())
                self.assertIsNone(evidence.selected)

    @unittest.skipUnless(os.name == "nt", "Windows reparse handling")
    def test_linked_package_metadata_is_rejected(self):
        app_root, selected = self._install("selected", "3.5.38")
        package_path = app_root / "package.json"
        outside = self.base / "outside-package.json"
        outside.write_text(
            json.dumps({"name": "Cursor", "version": "9.9.9"}),
            encoding="utf-8",
        )
        package_path.unlink()
        try:
            package_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: %s" % type(exc).__name__)

        evidence = cursor_version.inspect_cursor_version_evidence(
            (app_root,),
            app_root,
        )

        self.assertFalse(evidence.scope_complete)
        self.assertEqual(evidence.installed, ())

    def test_non_windows_collector_returns_unknown_without_path_probe(self):
        with mock.patch.object(cursor_version.os, "name", "posix"), mock.patch.object(
            cursor_version,
            "_known_folder_path",
        ) as known_folder:
            evidence = cursor_version.collect_cursor_version_evidence()

        self.assertFalse(evidence.scope_complete)
        self.assertEqual(evidence.installed, ())
        known_folder.assert_not_called()

    def test_path_parser_rejects_unsafe_or_unbounded_values_without_fs_probe(self):
        approved = PureWindowsPath(r"C:\Program Files\cursor\resources\app")
        cases = (
            r"\\server\private-share;C:\Program Files\cursor\resources\app\bin",
            r"relative-private-root;C:\Program Files\cursor\resources\app\bin",
            r"\\?\C:\private-device-root;C:\Program Files\cursor\resources\app\bin",
            "x" * (cursor_version._MAX_WINDOWS_ENV_PATH_CHARS + 1),
            ";".join(
                r"C:\safe\entry-%d" % index
                for index in range(cursor_version._MAX_WINDOWS_PATH_ENTRIES + 1)
            ),
        )

        with mock.patch.object(cursor_version.os, "lstat") as lstat:
            for value in cases:
                with self.subTest(value_length=len(value)):
                    selected, complete = (
                        cursor_version._selected_standard_root_from_path(
                            (approved,),
                            value,
                        )
                    )
                    self.assertIsNone(selected)
                    self.assertFalse(complete)

        lstat.assert_not_called()

    def test_package_parser_runs_without_windows_filesystem(self):
        self.assertEqual(
            str(
                cursor_version._parse_package(
                    b'{"name":"Cursor","version":"3.5.38"}'
                )
            ),
            "3.5.38",
        )
        for payload in (
            b'{"name":"Private","version":"3.5.38"}',
            b'{"name":"Cursor","version":"3.5-beta"}',
            b'{"name":"Cursor","version":"3.5.38","version":"9.9.9"}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    cursor_version._parse_package(payload)

    @unittest.skipUnless(os.name == "nt", "Windows Known Folder collection")
    def test_collector_uses_known_folders_and_lexical_standard_path_only(self):
        user_programs = self.base / "user-programs"
        program_files = self.base / "program-files"
        program_files_x86 = self.base / "program-files-x86"
        user_root = user_programs / "cursor" / "resources" / "app"
        system_root = program_files / "cursor" / "resources" / "app"
        for root, version in ((user_root, "3.3.30"), (system_root, "3.5.38")):
            (root / "bin").mkdir(parents=True)
            (root / "package.json").write_text(
                json.dumps({"name": "Cursor", "version": version}),
                encoding="utf-8",
            )
            (root / "bin" / "cursor.cmd").write_text(
                "@echo off\n",
                encoding="utf-8",
            )
        known = {
            cursor_version._FOLDERID_USER_PROGRAM_FILES: user_programs,
            cursor_version._FOLDERID_PROGRAM_FILES: program_files,
            cursor_version._FOLDERID_PROGRAM_FILES_X86: program_files_x86,
        }
        environment = {
            "PATH": os.pathsep.join(
                (
                    str(system_root / "bin"),
                    str(user_root / "bin"),
                )
            ),
            "LOCALAPPDATA": str(self.base / "forged-local"),
            "ProgramFiles": str(self.base / "forged-system"),
        }

        with mock.patch.object(
            cursor_version,
            "_known_folder_path",
            side_effect=lambda folder_id: known[folder_id],
        ), mock.patch.dict(os.environ, environment, clear=False):
            evidence = cursor_version.collect_cursor_version_evidence()

        self.assertTrue(evidence.scope_complete)
        self.assertEqual(str(evidence.selected), "3.5.38")
        self.assertEqual(
            tuple(str(version) for version in evidence.installed),
            ("3.3.30", "3.5.38"),
        )

    @unittest.skipUnless(os.name == "nt", "Windows intermediate reparse handling")
    def test_intermediate_link_in_standard_root_is_rejected(self):
        outside_root, _launcher = self._install("outside", "3.5.38")
        linked_parent = self.base / "linked-parent"
        try:
            linked_parent.symlink_to(
                outside_root.parent.parent,
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest("symlink creation unavailable: %s" % type(exc).__name__)
        candidate = linked_parent / "resources" / "app"

        evidence = cursor_version.inspect_cursor_version_evidence(
            (candidate,),
            candidate,
        )

        self.assertFalse(evidence.scope_complete)
        self.assertEqual(evidence.installed, ())


class CursorVersionDoctorTests(unittest.TestCase):
    def _evidence(
        self,
        selected: str | None,
        installed: tuple[str, ...],
        *,
        complete: bool = True,
    ) -> client_host_version.HostVersionEvidence:
        return client_host_version.HostVersionEvidence(
            selected=(
                client_host_version.NumericVersion.parse(selected)
                if selected is not None
                else None
            ),
            installed=tuple(
                client_host_version.NumericVersion.parse(value)
                for value in installed
            ),
            installations=len(installed),
            scope_complete=complete,
        )

    def test_doctor_reports_candidate_floor_without_claiming_compatibility(self):
        evidence = self._evidence(
            "3.3.30",
            ("3.3.30", "3.5.38"),
        )

        with mock.patch.object(
            cursor_version,
            "collect_cursor_version_evidence",
            return_value=evidence,
        ):
            result = doctor.check_cursor_version()

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.name, "cursor-version")
        self.assertIn("candidate_standard_version_floor_met", result.detail)
        self.assertIn("scope=standard_windows_roots", result.detail)
        self.assertIn("exhaustive=false", result.detail)
        self.assertIn("path_preferred_standard_install=3.3.30", result.detail)
        self.assertIn("installed=3.3.30,3.5.38", result.detail)
        self.assertIn("minimum=2.4", result.detail)
        self.assertIn("compatibility=unverified", result.detail)
        self.assertNotIn("supported", result.detail)

    def test_doctor_reports_unknown_or_unsupported_as_warn(self):
        cases = (
            (
                self._evidence(None, ("3.5.38",)),
                "cursor_version_unknown",
            ),
            (
                self._evidence("2.3.9", ("2.3.9",)),
                "unsupported_cursor_version",
            ),
        )

        for evidence, state in cases:
            with self.subTest(state=state), mock.patch.object(
                cursor_version,
                "collect_cursor_version_evidence",
                return_value=evidence,
            ):
                result = doctor.check_cursor_version()
                self.assertEqual(result.status, "WARN")
                self.assertIn(state, result.detail)
                self.assertTrue(result.fix)

    def test_doctor_collector_exception_is_fixed_and_redacted(self):
        private = r"C:\private-user-path\package.json"
        output = io.StringIO()

        with mock.patch.object(
            cursor_version,
            "collect_cursor_version_evidence",
            side_effect=RuntimeError(private),
        ), redirect_stdout(output):
            result = doctor.check_cursor_version()
            rc = doctor.main(["--json", "--cursor-version"])

        self.assertEqual(result.status, "WARN")
        self.assertIn("cursor_version_unknown", result.detail)
        self.assertIn("evidence=collector_error", result.detail)
        self.assertNotIn(private, result.detail)
        self.assertEqual(rc, 0)
        self.assertNotIn(private, output.getvalue())
        self.assertIn("evidence=collector_error", output.getvalue())

    def test_cursor_version_cli_is_opt_in_and_machine_readable(self):
        evidence = self._evidence("3.5.38", ("3.5.38",))
        output = io.StringIO()
        unrelated_check = mock.Mock(
            return_value=doctor.CheckResult(
                "PASS",
                "unrelated",
                "private-user-path",
            )
        )

        with mock.patch.object(doctor, "CHECKS", (unrelated_check,)), mock.patch.object(
            cursor_version,
            "collect_cursor_version_evidence",
            return_value=evidence,
        ), redirect_stdout(output):
            rc = doctor.main(["--json", "--cursor-version"])

        payload = json.loads(output.getvalue())
        unrelated_check.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["name"], "cursor-version")
        self.assertNotIn("private-user-path", output.getvalue())


if __name__ == "__main__":
    unittest.main()
