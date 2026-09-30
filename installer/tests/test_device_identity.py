from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import date
from unittest import mock

from client import device_identity
from client import runner
from installer import config


class DeviceNameDefaultTest(unittest.TestCase):
    def test_installer_and_runtime_share_one_default_name_implementation(self):
        self.assertIs(config.device_name_default, runner.device_name_default)

    def test_architecture_categories_cover_common_aliases_and_safe_fallback(self):
        cases = {
            "arm64": "arm",
            "aarch64": "arm",
            "AMD64": "x64",
            "x86_64": "x64",
            "i686": "x86",
            "riscv64-unknown": "riscv64.unknown",
            "": "unknown",
        }
        for machine, expected in cases.items():
            with self.subTest(machine=machine):
                self.assertEqual(
                    device_identity._architecture_category(machine), expected
                )

    def test_macos_arm_default_name_includes_os_version_arch_and_activation_date(self):
        with (
            mock.patch.object(config.platform, "system", return_value="Darwin"),
            mock.patch.object(config.platform, "mac_ver", return_value=("27.0", ("", "", ""), "")),
            mock.patch.object(config.platform, "machine", return_value="arm64"),
        ):
            activation_date = date(2026, 9, 24)
            expected = "macOS-27.0-arm-260924"
            self.assertEqual(
                config.device_name_default(activation_date=activation_date), expected
            )
            self.assertEqual(
                runner.device_name_default(activation_date=activation_date), expected
            )

    def test_windows_default_name_uses_release_display_version_and_x64_category(self):
        class RegistryKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=lambda *_args, **_kwargs: RegistryKey(),
            QueryValueEx=lambda _key, name: ("25H2", 1)
            if name == "DisplayVersion"
            else (_ for _ in ()).throw(FileNotFoundError(name)),
        )
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Windows"),
            mock.patch.object(device_identity.platform, "release", return_value="11"),
            mock.patch.object(device_identity.platform, "machine", return_value="AMD64"),
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            activation_date = date(2026, 9, 24)
            expected = "windows11-25H2-x64-260924"
            self.assertEqual(
                config.device_name_default(activation_date=activation_date), expected
            )
            self.assertEqual(
                runner.device_name_default(activation_date=activation_date), expected
            )

    def test_windows_11_is_detected_from_native_build_when_platform_reports_10(self):
        class RegistryKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        registry_values = {
            "DisplayVersion": "23H2",
            "CurrentBuildNumber": "22631",
        }
        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=lambda *_args, **_kwargs: RegistryKey(),
            QueryValueEx=lambda _key, name: (registry_values[name], 1)
            if name in registry_values
            else (_ for _ in ()).throw(FileNotFoundError(name)),
        )
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Windows"),
            mock.patch.object(device_identity.platform, "release", return_value="10"),
            mock.patch.object(device_identity.platform, "machine", return_value="AMD64"),
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "windows11-23H2-x64-260924",
            )

    def test_windows_release_id_is_used_when_display_version_is_missing(self):
        class RegistryKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=lambda *_args, **_kwargs: RegistryKey(),
            QueryValueEx=lambda _key, name: ("22H2", 1)
            if name == "ReleaseId"
            else (_ for _ in ()).throw(FileNotFoundError(name)),
        )
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Windows"),
            mock.patch.object(device_identity.platform, "release", return_value="10"),
            mock.patch.object(device_identity.platform, "machine", return_value="AMD64"),
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "windows10-22H2-x64-260924",
            )

    def test_windows_uses_native_architecture_environment_under_wow64(self):
        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=mock.Mock(side_effect=OSError("registry unavailable")),
        )
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Windows"),
            mock.patch.object(device_identity.platform, "release", return_value="10"),
            mock.patch.object(device_identity.platform, "version", return_value="10.0.19045"),
            mock.patch.object(device_identity.platform, "machine", return_value="x86"),
            mock.patch.dict(
                os.environ,
                {"PROCESSOR_ARCHITEW6432": "AMD64", "PROCESSOR_ARCHITECTURE": "x86"},
                clear=False,
            ),
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "windows10-10.0.19045-x64-260924",
            )

    def test_macos_without_product_version_uses_unknown_not_darwin_kernel(self):
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Darwin"),
            mock.patch.object(device_identity.platform, "mac_ver", return_value=("", ("", "", ""), "")),
            mock.patch.object(device_identity.platform, "release", return_value="25.0.0"),
            mock.patch.object(device_identity.platform, "machine", return_value="arm64"),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "macOS-unknown-arm-260924",
            )

    def test_macos_rosetta_reports_native_arm_architecture(self):
        translated = types.SimpleNamespace(returncode=0, stdout="1\n")
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Darwin"),
            mock.patch.object(
                device_identity.platform,
                "mac_ver",
                return_value=("27.0", ("", "", ""), ""),
            ),
            mock.patch.object(device_identity.platform, "machine", return_value="x86_64"),
            mock.patch.object(
                device_identity.subprocess,
                "run",
                return_value=translated,
            ) as run,
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "macOS-27.0-arm-260924",
            )
        run.assert_called_once_with(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            stdout=device_identity.subprocess.PIPE,
            stderr=device_identity.subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=1,
            check=False,
        )

    def test_linux_version_separators_are_normalized_without_concatenating_tokens(self):
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Linux"),
            mock.patch.object(device_identity.platform, "release", return_value="6.8.0-rc1-generic"),
            mock.patch.object(device_identity.platform, "machine", return_value="x86_64"),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "linux-6.8.0.rc1.generic-x64-260924",
            )

    def test_windows_registry_failure_falls_back_to_kernel_version(self):
        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            KEY_READ=1,
            KEY_WOW64_64KEY=2,
            OpenKey=mock.Mock(side_effect=OSError("registry unavailable")),
        )
        with (
            mock.patch.object(device_identity.platform, "system", return_value="Windows"),
            mock.patch.object(device_identity.platform, "release", return_value="11"),
            mock.patch.object(
                device_identity.platform,
                "version",
                return_value="10.0.26100",
            ),
            mock.patch.object(device_identity.platform, "machine", return_value="AMD64"),
            mock.patch.dict(sys.modules, {"winreg": fake_winreg}),
        ):
            self.assertEqual(
                config.device_name_default(activation_date=date(2026, 9, 24)),
                "windows11-10.0.26100-x64-260924",
            )


if __name__ == "__main__":
    unittest.main()
