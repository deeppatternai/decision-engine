"""Contract locks for the shared macOS/Linux lifecycle entrypoints."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


def isolated_home_environment(home: Path, *, path: str) -> dict[str, str]:
    environment = {
        (key.upper() if os.name == "nt" else key): value
        for key, value in os.environ.items()
    }
    for key in (
        "CLAUDE_CODE_CONFIG",
        "CLAUDE_DESKTOP_CONFIG",
        "CLAUDE_DESKTOP_3P_CONFIG",
        "CLAUDE_SKILLS_DIR",
        "CODEBUDDY_CLI",
        "CODEBUDDY_CONFIG",
        "CODEBUDDY_SKILLS_DIR",
        "CODEX_AGENTS_MD",
        "CODEX_CONFIG",
        "CODEX_SKILLS_DIR",
        "CURSOR_CONFIG",
        "CURSOR_SKILLS_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        environment.pop(key, None)
    environment.update(HOME=str(home), PATH=path)
    if os.name == "nt":
        environment.update(
            USERPROFILE=str(home),
            APPDATA=str(home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(home / "AppData" / "Local"),
        )
    return environment


class AgentTerminalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INSTALL.read_text(encoding="utf-8")
        cls.bash = bash_executable()

    def test_argument_contract_rejects_unknown_options_before_mutation(self) -> None:
        result = subprocess.run(
            [self.bash, str(INSTALL), "--not-supported"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported arguments", result.stderr)

        parser = self.source[
            self.source.index('case "$#:${1:-}" in') :
            self.source.index('[ -x /usr/bin/uname ]')
        ]
        for option in ("--agent-terminal", "--agent-activate", "--activate"):
            self.assertIn(option, parser)
        self.assertIn("0:) ;;", parser)

    def test_no_argument_path_remains_outside_agent_handoff(self) -> None:
        handoff_gate = self.source.index('if [ "$AGENT_TERMINAL_MODE" -eq 1 ]; then')
        handoff_call = self.source.index(
            "handoff_interactive_install_to_macos_terminal", handoff_gate
        )
        normal_preflight = self.source.index("read_linux_os_release()", handoff_call)
        core_install = self.source.index(
            'tty_print "Installing the signed Decision Engine stable release..."',
            normal_preflight,
        )
        self.assertLess(handoff_gate, handoff_call)
        self.assertLess(handoff_call, normal_preflight)
        self.assertLess(normal_preflight, core_install)

    def test_macos_handoff_runs_one_staged_flow_and_returns_child_status(self) -> None:
        handoff = shell_function(
            self.source,
            "handoff_interactive_install_to_macos_terminal",
            "read_linux_os_release",
        )
        self.assertEqual(handoff.count("/usr/bin/open -a Terminal"), 1)
        self.assertIn("/bin/cp \"$source_path\" \"$staged_script\"", handoff)
        self.assertIn("/bin/bash %q\\n", handoff)
        self.assertIn("/bin/bash %q --activation-only\\n", handoff)
        self.assertIn('while [ ! -f "$status_file" ]; do', handoff)
        self.assertIn('/bin/cat "$output_log"', handoff)
        self.assertIn('exit "$child_status"', handoff)
        self.assertIn('/bin/rm -rf "$handoff_root"', handoff)

    def test_agent_handoff_is_macos_only_and_blocks_recursive_terminal_use(self) -> None:
        handoff = shell_function(
            self.source,
            "handoff_interactive_install_to_macos_terminal",
            "read_linux_os_release",
        )
        self.assertIn('"$PLATFORM_FAMILY" = "macos"', handoff)
        self.assertIn("supported only on macOS", handoff)
        self.assertIn("TERM_PROGRAM:-", self.source)
        self.assertIn("com.apple.Terminal", self.source)
        self.assertIn(
            "--agent-activate was invoked inside Terminal.app", self.source
        )
        self.assertIn(
            "--agent-terminal was invoked inside Terminal.app", self.source
        )

    def test_activation_only_requires_clean_verified_managed_install(self) -> None:
        validation = shell_function(
            self.source, "validate_activation_only_install", "activation_only_state"
        )
        for required in (
            ".managed-install.json",
            ".runtime/update-state.json",
            ".runtime/update-protocol.json",
            "installer/activate.py",
            "installer/permanent_setup.py",
        ):
            self.assertIn(required, validation)
        self.assertIn("status --porcelain=v1 --untracked-files=all", validation)
        self.assertIn("validate_managed_identity", validation)
        self.assertIn("_require_protocol_ready", validation)
        self.assertIn("state.last_release_commit", validation)
        self.assertIn("state.last_version", validation)

    def test_activation_only_preserves_host_configuration_and_supports_old_release(self) -> None:
        activation = shell_function(
            self.source, "run_activation_only", "confirm_dependency_install"
        )
        self.assertIn("existing Agent configuration was preserved", activation)
        self.assertIn('arguments = ["--activation-only"]', activation)
        self.assertIn("inspect.signature(run_setup)", activation)
        self.assertIn("permanent_setup._configure_agent_hosts", activation)
        self.assertIn("permanent_setup._doctor_has_blocking_failure", activation)
        self.assertIn("recovery-required", activation)
        self.assertIn("device remains unactivated", activation)


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
        self.assertIn('line_list_contains "$selected_source_clients" "claude-code"', self.source)
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

    def test_preconfiguration_snapshot_ignores_de_only_host_residue(self) -> None:
        detector = shell_function(self.source, "detect_source_clients", "try_git")
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            (home / ".claude").mkdir()
            (home / ".codex").mkdir()
            (home / ".cursor").mkdir()
            (home / ".codebuddy").mkdir()
            fixtures = {
                home / ".claude.json": '{"mcpServers":{"decision-engine":{"command":"python3"}}}\n',
                home / ".codex" / "config.toml": '[mcp_servers.decision-engine]\ncommand = "python3"\n',
                home / ".cursor" / "mcp.json": '{"mcpServers":{"decision-engine":{"command":"python3"}}}\n',
                home / ".codebuddy" / "mcp.json": '{"mcpServers":{"decision-engine":{"command":"python3"}}}\n',
            }
            for path, content in fixtures.items():
                path.write_text(content, encoding="utf-8")
            script = "\n".join(
                (
                    'de_exec() { "$@"; }',
                    f"catalog_root={str(ROOT)!r}",
                    f"PYTHON_BIN={sys.executable!r}",
                    "PLATFORM_FAMILY=linux",
                    detector,
                    "detect_source_clients",
                )
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CODEBUDDY_CLI": str(home / "host-codebuddy.exe"),
                    "CODEX_CONFIG": str(home / "host-codex.toml"),
                    "XDG_CONFIG_HOME": str(home / "host-xdg"),
                },
                clear=False,
            ):
                environment = isolated_home_environment(home, path="/usr/bin:/bin")
            for inherited_key in ("CODEBUDDY_CLI", "CODEX_CONFIG", "XDG_CONFIG_HOME"):
                self.assertNotIn(inherited_key, environment)
            result = subprocess.run(
                [self.bash, "--noprofile", "--norc", "-c", script],
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for client in ("claude-code", "codex", "cursor", "codebuddy"):
                self.assertNotIn(client, result.stdout.splitlines())
            for path, content in fixtures.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse(any(home.rglob("*.de-bak.*")))

    def test_preconfiguration_snapshot_keeps_hosts_with_cli_evidence(self) -> None:
        detector = shell_function(self.source, "detect_source_clients", "try_git")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            binaries = home / ".local" / "bin" if os.name == "nt" else root / "bin"
            for path in (
                home / ".claude",
                home / ".codex",
                home / ".cursor",
                home / ".codebuddy",
                binaries,
            ):
                path.mkdir(parents=True)
            for name in ("claude", "codex", "cursor", "codebuddy"):
                executable = binaries / (
                    "codebuddy.exe" if os.name == "nt" and name == "codebuddy" else name
                )
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            script = "\n".join(
                (
                    'de_exec() { "$@"; }',
                    f"catalog_root={str(ROOT)!r}",
                    f"PYTHON_BIN={sys.executable!r}",
                    "PLATFORM_FAMILY=linux",
                    detector,
                    "detect_source_clients",
                )
            )
            environment = isolated_home_environment(
                home,
                path=f"{binaries}:/usr/bin:/bin",
            )
            environment["CODEBUDDY_CLI"] = str(
                binaries / ("codebuddy.exe" if os.name == "nt" else "codebuddy")
            )
            result = subprocess.run(
                [self.bash, "--noprofile", "--norc", "-c", script],
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            detected = result.stdout.splitlines()
            for client in ("claude-code", "codex", "cursor", "codebuddy"):
                self.assertIn(client, detected)

    def test_agent_selection_parser_preserves_default_all_and_catalog_order(self) -> None:
        functions = "\n".join(
            (
                shell_function(self.source, "append_client_line", "line_list_contains"),
                shell_function(self.source, "line_list_contains", "filter_client_lines_by_snapshot"),
                shell_function(self.source, "merge_client_lines_by_catalog", "comma_list_contains"),
                shell_function(self.source, "client_display_name", "print_client_lines_for_user"),
                shell_function(self.source, "shared_skill_pair_selection_valid", "select_install_clients"),
                shell_function(self.source, "select_install_clients", "regular_app_has_bundle_id")
                .replace(">/dev/tty", ">/dev/null")
                .replace("</dev/tty", ""),
            )
        )
        available = "claude-code\ncodex\ncursor"
        cases = (
            ("\n", ["claude-code", "codex", "cursor"]),
            ("ALL\n", ["claude-code", "codex", "cursor"]),
            ("3,,   1,3\n", ["claude-code", "cursor"]),
        )
        for answer, expected in cases:
            with self.subTest(answer=answer):
                result = subprocess.run(
                    [
                        self.bash,
                        "--noprofile",
                        "--norc",
                        "-c",
                        functions
                        + "\ntty_print() { :; }\n"
                        + "PLATFORM_DISPLAY_NAME=fixture\n"
                        + 'select_install_clients "$AVAILABLE" "${CONFIGURED:-}"',
                    ],
                    input=answer,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "AVAILABLE": available, "CONFIGURED": ""},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_agent_selection_retains_configured_hosts_and_rejects_split_skill_pair(self) -> None:
        functions = "\n".join(
            (
                shell_function(self.source, "append_client_line", "line_list_contains"),
                shell_function(self.source, "line_list_contains", "filter_client_lines_by_snapshot"),
                shell_function(self.source, "merge_client_lines_by_catalog", "comma_list_contains"),
                shell_function(self.source, "client_display_name", "print_client_lines_for_user"),
                shell_function(self.source, "shared_skill_pair_selection_valid", "select_install_clients"),
                shell_function(self.source, "select_install_clients", "regular_app_has_bundle_id")
                .replace(">/dev/tty", ">/dev/null")
                .replace("</dev/tty", ""),
            )
        )
        script = (
            functions
            + "\ntty_print() { :; }\n"
            + "PLATFORM_DISPLAY_NAME=fixture\n"
            + 'requested="$(select_install_clients "$AVAILABLE" "$CONFIGURED")"\n'
            + 'merge_client_lines_by_catalog "$AVAILABLE" "$CONFIGURED" "$requested"'
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", script],
            input="1\n1,2\n",
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "AVAILABLE": "qoder\nqoder-ide\ncodex",
                "CONFIGURED": "codex",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["qoder", "qoder-ide", "codex"])

    def test_aqg_and_managed_routing_are_bounded_by_effective_selection(self) -> None:
        append_function = shell_function(
            self.source, "append_client_line", "line_list_contains"
        )
        contains_function = shell_function(
            self.source, "line_list_contains", "filter_client_lines_by_snapshot"
        )
        filter_function = shell_function(
            self.source, "filter_client_lines_by_snapshot", "comma_list_contains"
        )
        script = "\n".join(
            (
                append_function,
                contains_function,
                filter_function,
                "filter_client_lines_by_snapshot $'claude-code\\ncodex\\ncursor' $'codex\\ncursor'",
            )
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["codex", "cursor"])
        source_snapshot = self.source.index(
            'source_detected_clients="$(detect_source_clients'
        )
        selection = self.source.index(
            'requested_source_clients="$(\n  select_install_clients', source_snapshot
        )
        aqg_apply = self.source.index('run_aqg_clients "apply" --apply')
        self.assertLess(source_snapshot, aqg_apply)
        self.assertLess(selection, aqg_apply)
        self.assertIn(
            'line_list_contains "$selected_source_clients" "$aqg_client" || continue',
            self.source,
        )
        self.assertIn(
            'bootstrap_clients="$(printf \'%s\' "$selected_source_clients"',
            self.source,
        )
        self.assertIn(
            'filter_client_lines_by_snapshot \\\n    "$managed_detected_clients" "$source_clients_for_catalog"',
            self.source,
        )

    def test_no_agent_preflight_stops_before_aqg_or_core_mutation(self) -> None:
        preflight = self.source.index('if [ -z "$source_detected_clients" ]; then')
        blocked = self.source.index(
            'dependency_pending "Agent preflight found no supported installed host;',
            preflight,
        )
        aqg_apply = self.source.index('run_aqg_clients "apply" --apply')
        core_install = self.source.index(
            'tty_print "Installing the signed Decision Engine stable release..."'
        )
        self.assertLess(preflight, blocked)
        self.assertLess(blocked, aqg_apply)
        self.assertLess(blocked, core_install)

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
