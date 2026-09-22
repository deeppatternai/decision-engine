"""Contract locks for the shared macOS/Linux lifecycle entrypoints."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "dp-install.sh"
UNINSTALL = ROOT / "dp-uninstall.sh"


def shell_function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{next_name}() {{", start)
    return source[start:end]


def bash_executable() -> str:
    candidates: list[Path] = []
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for parent in Path(git).resolve().parents:
                candidate = parent / "usr" / "bin" / "bash.exe"
                if candidate.is_file():
                    candidates.append(candidate)
                    break
        candidates.extend(
            Path(entry) / "bash.exe"
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry
        )
    elif resolved := shutil.which("bash"):
        candidates.append(Path(resolved).resolve())
    probe = 'f() { test "$1" = "probe"; }; f probe'
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [str(candidate), "--noprofile", "--norc", "-c", probe],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return str(candidate)
    raise RuntimeError("a Bash with function argument support is required")


class LinuxInstallContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INSTALL.read_text(encoding="utf-8")
        cls.bash = bash_executable()

    def test_supported_linux_floors_and_desktop_only(self) -> None:
        preflight = shell_function(
            self.source, "preflight_linux_platform", "regular_app_has_bundle_id"
        )
        for floor in (
            "ubuntu) linux_release_at_least 22.04",
            "debian) linux_release_at_least 12",
            "fedora) linux_release_at_least 44",
        ):
            self.assertIn(floor, preflight)
        self.assertIn("x86_64|amd64|aarch64|arm64", preflight)
        self.assertIn("this prototype requires glibc", preflight)
        self.assertIn('user_id" != "0"', preflight)
        self.assertIn('DISPLAY:-', preflight)
        self.assertIn('WAYLAND_DISPLAY:-', preflight)

    def test_newer_releases_enter_core_install_but_malformed_and_older_fail(self) -> None:
        version_check = shell_function(
            self.source, "linux_release_at_least", "preflight_linux_platform"
        )
        cases = (
            ("ubuntu", "22.04", "22.04", True),
            ("ubuntu", "26.04", "22.04", True),
            ("ubuntu", "28.04", "22.04", True),
            ("debian", "12", "12", True),
            ("debian", "13", "12", True),
            ("fedora", "44", "44", True),
            ("fedora", "45", "44", True),
            ("ubuntu", "22.03", "22.04", False),
            ("debian", "11", "12", False),
            ("fedora", "43", "44", False),
            ("ubuntu", "26.04.1", "22.04", False),
            ("ubuntu", "26.04bad", "22.04", False),
            ("ubuntu", "9999", "22.04", False),
        )
        for distro, version, floor, accepted in cases:
            with self.subTest(distro=distro, version=version):
                result = subprocess.run(
                    [self.bash, "--noprofile", "--norc", "-c", version_check + "\n"
                     + f"LINUX_VERSION_ID={version!r}\nlinux_release_at_least {floor}"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_package_managers_are_consent_gated_and_repository_neutral(self) -> None:
        apt = shell_function(
            self.source, "install_linux_apt_packages", "print_linux_dnf_manual_command"
        )
        dnf = shell_function(
            self.source, "install_linux_dnf_packages", "linux_core_dependencies_ready"
        )
        self.assertIn('confirm_dependency_install "$prompt"', apt)
        self.assertIn('"$DPKG_BIN" --audit', self.source)
        self.assertIn('"$SUDO_BIN" "$APT_GET_BIN" update', apt)
        self.assertIn(
            "APT:"
            ":Get:"
            ":Allow"
            "Unauthenticated=false",
            apt,
        )
        self.assertIn("--no-install-recommends", apt)
        self.assertIn('confirm_dependency_install "$prompt"', dnf)
        self.assertIn('"$RPM_BIN" --verifydb', self.source)
        self.assertIn("--setopt=install_weak_deps=False", dnf)
        for forbidden in (
            "add-apt-repository",
            "do-release-upgrade",
            "apt-get upgrade",
            "apt-get dist-upgrade",
            "dnf system-upgrade",
        ):
            self.assertNotIn(forbidden, apt + dnf)

    def test_linux_private_python_assets_are_pinned_for_both_architectures(self) -> None:
        runtime = shell_function(
            self.source, "select_private_runtime_spec", "runtime_marker_matches"
        )
        self.assertIn('PRIVATE_RUNTIME_PLATFORM="unknown-linux-gnu"', runtime)
        self.assertIn(
            'PRIVATE_RUNTIME_SHA256="'
            '76ed18125286d7dc96ce24023d1e319d'
            'bd55a89a767102411b1ea23846113f69"',
            runtime,
        )
        self.assertIn(
            'PRIVATE_RUNTIME_SHA256="'
            '0651dd7157d3debf769e15a52c1de9d'
            'e7fbcdc36ba72faf79fde3c44f14d9461"',
            runtime,
        )
        self.assertIn('PRIVATE_RUNTIME_ASSET_SIZE="91086530"', runtime)
        self.assertIn('PRIVATE_RUNTIME_ASSET_SIZE="119758082"', runtime)
        self.assertIn('PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 91 MB"', runtime)
        self.assertIn('PRIVATE_RUNTIME_DOWNLOAD_LABEL="about 120 MB"', runtime)

    def test_gui_backends_are_fixed_per_verified_linux_platform(self) -> None:
        popup = shell_function(
            self.source,
            "prepare_linux_popup_backend",
            "repair_managed_skill_routes",
        )
        for package in (
            '"pywebview": "6.2.1"',
            '"QtPy": "2.4.3"',
            '"PyQt6": "6.11.0"',
            '"PyQt6-Qt6": "6.11.2"',
            '"PyQt6-WebEngine": "6.11.0"',
            '"PyQt6-WebEngine-Qt6": "6.11.2"',
        ):
            self.assertIn(package, popup)
        gtk = shell_function(
            self.source,
            "prepare_debian12_arm64_gtk_popup_backend",
            "prepare_linux_popup_backend",
        )
        self.assertIn("PYWEBVIEW_GUI=gtk", gtk)
        self.assertIn("pycairo-1.27.0.tar.gz#sha256=", gtk)
        self.assertIn("pygobject-3.50.0.tar.gz#sha256=", gtk)
        self.assertIn("pywebview-6.2.1-py3-none-any.whl#sha256=", gtk)

    def test_ubuntu_2604_uses_the_t64_qt_path(self) -> None:
        popup_support = shell_function(
            self.source, "linux_native_popup_supported", "linux_debian_arm64_gtk_supported"
        )
        apt_packages = shell_function(
            self.source, "linux_apt_dependency_packages", "linux_dnf_dependency_packages"
        )
        popup_setup = shell_function(
            self.source, "prepare_linux_popup_backend", "repair_managed_skill_routes"
        )
        self.assertIn("ubuntu:24.04|ubuntu:26.04|fedora:44", popup_support)
        self.assertIn('ubuntu:24.04|ubuntu:26.04) minizip_package="libminizip1t64"', apt_packages)
        self.assertIn('ubuntu:24.04|ubuntu:26.04)', popup_setup)
        self.assertIn('xcb_cursor_package="libxcb-cursor0"', popup_setup)

        shell = "\n".join(
            (
                popup_support,
                apt_packages,
                'try_git() { return 1; }',
                'linux_ca_bundle_available() { return 1; }',
                'linux_shared_library_available() { return 1; }',
                'linux_debian_arm64_gtk_dependency_packages() { :; }',
                'LINUX_DISTRO_ID=ubuntu LINUX_VERSION_ID=26.04 LINUX_MACHINE_ARCH=x86_64',
                'linux_native_popup_supported || exit 1',
                'linux_apt_dependency_packages',
            )
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", shell],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("libminizip1t64 libxcb-cursor0", result.stdout)
        self.assertNotIn("libminizip1 ", result.stdout)

        restricted = subprocess.run(
            [
                self.bash, "--noprofile", "--norc", "-c",
                popup_support + "\n"
                "LINUX_DISTRO_ID=ubuntu LINUX_VERSION_ID=22.04 "
                "LINUX_MACHINE_ARCH=aarch64\n"
                "linux_native_popup_supported",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(restricted.returncode, 1)

    def test_fedora_compatibility_runtime_covers_both_architectures(self) -> None:
        fedora = shell_function(
            self.source,
            "install_fedora_minizip_compat",
            "prepare_debian12_arm64_gtk_popup_backend",
        )
        self.assertIn("linux-gui-minizip-1.3-noble-aarch64", fedora)
        self.assertIn("linux-gui-minizip-1.3-noble-x86_64", fedora)
        self.assertEqual(len(re.findall(r'asset_sha="[0-9a-f]{64}"', fedora)), 2)
        self.assertEqual(len(re.findall(r'library_sha="[0-9a-f]{64}"', fedora)), 2)
        self.assertIn("COPYRIGHT-libminizip", fedora)
        self.assertIn('clean_exec /bin/ln "$runtime_library" "$target"', fedora)

    def test_codex_forwards_the_active_linux_desktop_session(self) -> None:
        forwarding = shell_function(
            self.source, "ensure_linux_codex_gui_environment", "cmp_verified_file"
        )
        for variable in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "XAUTHORITY",
        ):
            self.assertIn(f'"{variable}"', forwarding)
        self.assertIn("Linux Codex GUI environment forwarding did not persist", forwarding)

    def test_absent_claude_isolated_from_legacy_signed_stable(self) -> None:
        exec_wrapper = shell_function(self.source, "de_exec", "try_git")
        self.assertIn('CLAUDE_SKILLS_DIR="$tmp_root/absent-claude-skills"', exec_wrapper)
        self.assertIn('line_list_contains "$source_detected_clients" "claude-code"', self.source)
        converge = shell_function(
            self.source, "converge_managed_claude_hooks", "reconcile_codex_hooks_after_aqg_migration"
        )
        self.assertIn('comma_list_contains "${aqg_selected_clients:-}" "claude-code" || return 0', converge)
        shell = "\n".join(
            (
                'clean_exec() { "$@"; }',
                exec_wrapper,
                'workbuddy_variant=""',
                'tmp_root=/tmp/de-route-test',
                'absent_claude_route=1',
                'de_exec /usr/bin/env',
            )
        )
        environment = os.environ.copy()
        environment["LC_ALL"] = "C.UTF-8"
        environment["DE_TEST_UTF8"] = "编码检查"
        if os.name == "nt":
            environment["PATH"] = (
                str(Path(self.bash).parent) + os.pathsep + environment.get("PATH", "")
            )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", shell],
            capture_output=True,
            env=environment,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("CLAUDE_SKILLS_DIR=/tmp/de-route-test/absent-claude-skills", result.stdout)
        self.assertIn("DE_TEST_UTF8=编码检查", result.stdout)

    def test_linux_uses_in_process_stopper_without_installing_a_service(self) -> None:
        self.assertIn(
            "Linux uses the signed Decision Engine in-process Stopper fallback; "
            "no system service or root privilege was installed.",
            self.source,
        )
        self.assertIn('if [ "$PLATFORM_FAMILY" = "macos" ]; then', self.source)


class LinuxUninstallContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = UNINSTALL.read_text(encoding="utf-8")

    def test_shared_uninstaller_is_isolated_and_supports_only_macos_linux(self) -> None:
        self.assertTrue(self.source.startswith("#!/usr/bin/env bash\n"))
        self.assertIn('exec "${python_bin}" -I - "$@"', self.source)
        self.assertIn('sys.platform not in {"darwin", "linux"}', self.source)
        for network_command in ("git clone", "git fetch", "curl ", "wget "):
            self.assertNotIn(network_command, self.source)

    def test_linux_process_identity_comes_from_proc_and_is_revalidated(self) -> None:
        self.assertIn('root = Path("/proc") / pid', self.source)
        self.assertIn('(root / "stat").read_text', self.source)
        self.assertIn('(root / "status").read_text', self.source)
        self.assertIn('(root / "cmdline").read_bytes', self.source)
        self.assertIn('executable = os.readlink(root / "exe")', self.source)
        self.assertIn('environment.get("PYTHONPATH") != str(self.de)', self.source)
        self.assertIn('host not in REGISTERED_DE_HOST_LABELS', self.source)

    def test_quarantine_manifest_is_batched_visible_and_interruptible(self) -> None:
        self.assertIn("MANIFEST_PROGRESS_ENTRY_INTERVAL = 250", self.source)
        self.assertIn("MANIFEST_PROGRESS_SECONDS = 5.0", self.source)
        self.assertIn("QUARANTINE INTEGRITY: indexing", self.source)
        self.assertIn("QUARANTINE INTEGRITY: hashing and recording", self.source)
        self.assertIn("QUARANTINE INTEGRITY: complete", self.source)
        self.assertIn("def add_many(self, entries:", self.source)
        self.assertIn('status="interrupted" if isinstance(exc, KeyboardInterrupt)', self.source)
        self.assertIn("UNINSTALL_INTERRUPTED", self.source)


if __name__ == "__main__":
    unittest.main()
