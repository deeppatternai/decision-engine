"""Behavior locks for macOS desktop host integration."""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import mcp_config
from installer.client_hosts import launchers
from installer.client_hosts.hosts import (
    qoder,
    qoder_cn,
    trae,
    trae_cn,
    trae_work,
    trae_work_cn,
    workbuddy,
)
from installer.config import ShellError


class MacOSDesktopHostTestCase(unittest.TestCase):
    def test_desktop_python_policy_is_direct_on_macos(self):
        request = launchers.LaunchRequest(
            command="/Applications/Python 3.13/bin/python3",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", "/managed root"),
            current_interpreter="/Applications/Python 3.13/bin/python3",
            python_version=(3, 13),
            platform="darwin",
        )

        resolved = launchers.resolve_launch("desktop-python-v1", request)

        self.assertEqual(resolved.command, request.command)
        self.assertEqual(resolved.launcher_args, request.launcher_args)
        self.assertEqual(
            resolved.cwd_independent_args,
            request.cwd_independent_args,
        )

    def test_desktop_python_policy_preserves_windows_space_workaround(self):
        request = launchers.LaunchRequest(
            command=r"C:\Program Files\Python313\python.exe",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", r"C:\managed root"),
            current_interpreter=r"C:\Program Files\Python313\python.exe",
            python_version=(3, 13),
            platform="win32",
        )
        with (
            mock.patch.object(
                launchers,
                "_find_windows_py_launcher",
                return_value=r"C:\Windows\py.exe",
            ),
            mock.patch.object(
                launchers,
                "_py_launcher_resolves_to_interpreter",
                return_value=True,
            ),
        ):
            resolved = launchers.resolve_launch("desktop-python-v1", request)

        self.assertEqual(resolved.command, r"C:\Windows\py.exe")
        self.assertEqual(resolved.launcher_args[0], "-3.13")

    def test_desktop_python_policy_rejects_unverified_platforms(self):
        request = launchers.LaunchRequest(
            command="/usr/bin/python3",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=("-c", "bootstrap", "/managed"),
            current_interpreter="/usr/bin/python3",
            python_version=(3, 13),
            platform="linux",
        )
        with self.assertRaisesRegex(ShellError, "Windows or macOS"):
            launchers.resolve_launch("desktop-python-v1", request)

    def test_absolute_bootstrap_uses_posix_paths_on_macos(self):
        root = "/Users/test/Decision Engine"
        request = launchers.LaunchRequest(
            command="/usr/bin/python3",
            launcher_args=("-m", "installer.launcher"),
            cwd_independent_args=(
                "-c",
                launchers.CWD_INDEPENDENT_BOOTSTRAP,
                root,
                "--managed-root",
                root,
            ),
            current_interpreter="/usr/bin/python3",
            python_version=(3, 13),
            platform="darwin",
        )

        resolved = launchers.resolve_launch("absolute-bootstrap-v1", request)

        self.assertEqual(
            resolved.launcher_args[0],
            "/Users/test/Decision Engine/installer/mcp_bootstrap.py",
        )

    def test_default_macos_paths_match_desktop_bundles_and_profiles(self):
        environment = {
            "WORKBUDDY_APP_ROOT": "",
            "TRAE_WORK_APP_ROOT": "",
            "TRAE_WORK_CN_APP_ROOT": "",
            "TRAE_APP_ROOT": "",
            "TRAE_CN_APP_ROOT": "",
            "QODER_APP_ROOT": "",
            "QODER_CN_APP_ROOT": "",
            "APPDATA": "",
            "LOCALAPPDATA": "",
        }
        home = Path("/Users/test")
        with (
            mock.patch.object(Path, "home", return_value=home),
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(workbuddy.sys, "platform", "darwin"),
            mock.patch.object(trae_work.sys, "platform", "darwin"),
            mock.patch.object(trae_work_cn.sys, "platform", "darwin"),
            mock.patch.object(trae.sys, "platform", "darwin"),
            mock.patch.object(trae_cn.sys, "platform", "darwin"),
            mock.patch.object(qoder.sys, "platform", "darwin"),
            mock.patch.object(qoder_cn.sys, "platform", "darwin"),
        ):
            self.assertEqual(
                workbuddy._workbuddy_app_root(),
                Path("/Applications/WorkBuddy.app"),
            )
            self.assertEqual(
                trae_work._trae_work_app_root(),
                Path("/Applications/TRAE SOLO.app"),
            )
            self.assertEqual(
                trae_work_cn._trae_work_cn_app_root(),
                Path("/Applications/TRAE SOLO CN.app"),
            )
            self.assertEqual(
                qoder._qoder_app_root(),
                Path("/Applications/Qoder.app"),
            )
            self.assertEqual(
                qoder_cn._qoder_cn_app_root(),
                Path("/Applications/Qoder CN.app"),
            )
            self.assertEqual(trae._app_root(), Path("/Applications/Trae.app"))
            self.assertEqual(
                trae_cn._app_root(), Path("/Applications/Trae CN.app")
            )
            self.assertEqual(
                trae_work._trae_work_config_path(),
                home
                / "Library"
                / "Application Support"
                / "TRAE SOLO"
                / "User"
                / "mcp.json",
            )
            self.assertEqual(
                trae_work_cn._trae_work_cn_config_path(),
                home
                / "Library"
                / "Application Support"
                / "TRAE SOLO CN"
                / "User"
                / "mcp.json",
            )
            self.assertEqual(
                workbuddy._workbuddy_config_path(),
                home / ".workbuddy" / "mcp.json",
            )
            self.assertEqual(
                qoder._qoder_config_path(),
                home / ".qoder" / "settings.json",
            )

    def test_macos_guards_accept_exact_installed_product_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbuddy_root = root / "WorkBuddy.app"
            workbuddy_executable = workbuddy_root / "Contents" / "MacOS" / "Electron"
            workbuddy_executable.parent.mkdir(parents=True)
            workbuddy_executable.write_bytes(b"signed-product-probe")

            products = (
                (
                    trae_work,
                    root / "TRAE SOLO.app",
                    "trae-solo",
                    "appVersion",
                    "0.1.48",
                    "com.trae.solo.app",
                ),
                (
                    trae_work_cn,
                    root / "TRAE SOLO CN.app",
                    "trae-solo-cn",
                    "appVersion",
                    "0.1.49",
                    "cn.trae.solo.app",
                ),
                (
                    trae,
                    root / "Trae.app",
                    "trae",
                    "appVersion",
                    "3.5.81",
                    "com.trae.app",
                ),
                (
                    trae_cn,
                    root / "Trae CN.app",
                    "trae-cn",
                    "appVersion",
                    "3.3.95",
                    "cn.trae.app",
                ),
                (
                    qoder,
                    root / "Qoder.app",
                    "qoder",
                    "version",
                    "0.1.4",
                    "com.qoder.app",
                ),
                (
                    qoder_cn,
                    root / "Qoder CN.app",
                    "qoder-cn",
                    "version",
                    "0.1.4",
                    "com.qodercn.app",
                ),
            )
            for (
                _module,
                app_root,
                application_name,
                version_field,
                version,
                bundle_id,
            ) in products:
                if _module in {qoder, qoder_cn}:
                    product = app_root / "Contents" / "Resources" / "product.json"
                    product.parent.mkdir(parents=True)
                    product.write_text(
                        json.dumps({"productId": application_name}),
                        encoding="utf-8",
                    )
                    info = app_root / "Contents" / "Info.plist"
                    info.parent.mkdir(parents=True, exist_ok=True)
                    info.write_bytes(
                        plistlib.dumps(
                            {
                                "CFBundleIdentifier": bundle_id,
                                "CFBundleShortVersionString": version,
                                "CFBundleVersion": version,
                            }
                        )
                    )
                    continue
                product = app_root / "Contents" / "Resources" / "app" / "product.json"
                product.parent.mkdir(parents=True)
                product.write_text(
                    json.dumps(
                        {
                            "applicationName": application_name,
                            version_field: version,
                        }
                    ),
                    encoding="utf-8",
                )
                info = app_root / "Contents" / "Info.plist"
                info.parent.mkdir(parents=True, exist_ok=True)
                info.write_bytes(
                    plistlib.dumps(
                        {
                            "CFBundleIdentifier": bundle_id,
                            "CFBundleShortVersionString": version,
                        }
                    )
                )

            environment = {
                "WORKBUDDY_APP_ROOT": str(workbuddy_root),
                "TRAE_WORK_APP_ROOT": str(products[0][1]),
                "TRAE_WORK_CN_APP_ROOT": str(products[1][1]),
                "TRAE_APP_ROOT": str(products[2][1]),
                "TRAE_CN_APP_ROOT": str(products[3][1]),
                "QODER_APP_ROOT": str(products[4][1]),
                "QODER_CN_APP_ROOT": str(products[5][1]),
                "WORKBUDDY_CONFIG": str(root / "profiles" / "workbuddy.json"),
                "TRAE_WORK_CONFIG": str(root / "profiles" / "trae-work.json"),
                "TRAE_WORK_CN_CONFIG": str(root / "profiles" / "trae-work-cn.json"),
                "TRAE_CONFIG": str(root / "profiles" / "trae.json"),
                "TRAE_CN_CONFIG": str(root / "profiles" / "trae-cn.json"),
                "QODER_CONFIG": str(root / "profiles" / "qoder.json"),
                "QODER_CN_CONFIG": str(root / "profiles" / "qoder-cn.json"),
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch.object(workbuddy.sys, "platform", "darwin"),
                mock.patch.object(trae_work.sys, "platform", "darwin"),
                mock.patch.object(trae_work_cn.sys, "platform", "darwin"),
                mock.patch.object(trae.sys, "platform", "darwin"),
                mock.patch.object(trae_cn.sys, "platform", "darwin"),
                mock.patch.object(qoder.sys, "platform", "darwin"),
                mock.patch.object(qoder_cn.sys, "platform", "darwin"),
            ):
                self.assertIsNone(workbuddy._workbuddy_config_write_guard())
                self.assertIsNone(trae_work._trae_work_config_write_guard())
                self.assertIsNone(trae_work_cn._trae_work_cn_config_write_guard())
                self.assertIsNone(trae._config_write_guard())
                self.assertIsNone(trae_cn._config_write_guard())
                self.assertIsNone(qoder._qoder_config_write_guard())
                self.assertIsNone(qoder_cn._qoder_cn_config_write_guard())
                self.assertTrue(workbuddy._workbuddy_skills_in_use())
                self.assertTrue(trae_work._trae_work_skills_in_use())
                self.assertTrue(trae_work_cn._trae_work_cn_skills_in_use())
                self.assertTrue(trae._skills_in_use())
                self.assertTrue(trae_cn._skills_in_use())
                self.assertTrue(qoder._qoder_skills_in_use())
                self.assertTrue(qoder_cn._qoder_cn_skills_in_use())
                for client in (
                    "workbuddy",
                    "trae",
                    "trae-work",
                    "trae-cn",
                    "trae-work-cn",
                    "qoder",
                    "qoder-cn",
                ):
                    with self.subTest(client=client):
                        self.assertTrue(mcp_config.client_present(client))

    def test_fresh_macos_bundle_is_detected_before_profile_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "WorkBuddy.app"
            executable = app_root / "Contents" / "MacOS" / "Electron"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"signed-product-probe")
            with (
                mock.patch.object(workbuddy.sys, "platform", "darwin"),
                mock.patch.dict(
                    os.environ,
                    {
                        "WORKBUDDY_APP_ROOT": str(app_root),
                        "WORKBUDDY_CONFIG": str(root / "profile" / "mcp.json"),
                    },
                    clear=False,
                ),
            ):
                self.assertFalse((root / "profile").exists())
                self.assertTrue(mcp_config.client_present("workbuddy"))

    def test_desktop_host_specs_use_cross_platform_launch_policies(self):
        self.assertEqual(
            mcp_config.CLIENT_SPECS["workbuddy"].launch_policy,
            "desktop-python-v1",
        )
        self.assertEqual(
            mcp_config.CLIENT_SPECS["trae-work"].launch_policy,
            "desktop-python-v1",
        )
        self.assertEqual(
            mcp_config.CLIENT_SPECS["trae-work-cn"].launch_policy,
            "desktop-python-v1",
        )
        self.assertEqual(
            mcp_config.CLIENT_SPECS["qoder"].launch_policy,
            "absolute-bootstrap-v1",
        )

    def test_installation_probe_contract_rejects_noncallables(self):
        invalid = replace(
            mcp_config.CLIENT_SPECS["workbuddy"],
            installation_probe="not-callable",
        )
        with self.assertRaisesRegex(ShellError, "installation probe"):
            mcp_config.validate_host_specs({"workbuddy": invalid})

    def test_linux_write_guards_remain_fail_closed(self):
        for module, guard in (
            (workbuddy, workbuddy._workbuddy_config_write_guard),
            (trae_work, trae_work._trae_work_config_write_guard),
            (trae_work_cn, trae_work_cn._trae_work_cn_config_write_guard),
            (qoder, qoder._qoder_config_write_guard),
        ):
            with self.subTest(module=module.__name__), mock.patch.object(
                module.sys, "platform", "linux"
            ), self.assertRaisesRegex(ShellError, "unsupported_.*_platform"):
                guard()


if __name__ == "__main__":
    unittest.main()
