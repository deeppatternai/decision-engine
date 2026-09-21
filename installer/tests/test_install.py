"""Behavior tests for the unified installer (installer.install)."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from installer import config, install


def _bash_executable() -> str:
    resolved = shutil.which("bash")
    if resolved:
        return resolved
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if bundled.is_file():
            return str(bundled)
    raise unittest.SkipTest("Bash is required for install.sh behavior tests")


def _bash_path(path: Path) -> str:
    resolved = Path(path).resolve()
    if os.name != "nt":
        return str(resolved)
    return "/%s%s" % (resolved.drive[0].lower(), resolved.as_posix()[2:])


def _bash_script_command(script: Path, fake_bin: Path, target: str) -> list[str]:
    return [
        _bash_executable(),
        "-c",
        'PATH="$1:$PATH"; exec "$2" "$3"',
        "install-test",
        _bash_path(fake_bin),
        _bash_path(script),
        target,
    ]


class InstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.deeppattern = base / "deeppattern"
        self.skills = base / "claude-skills"
        self.codex_skills = base / "codex-skills"
        self.cursor_skills = base / "cursor-skills"
        self.trae_cn_skills = base / "trae-cn-skills"
        self.bundle = base / "bundle"
        # Build a fake bundle. Only decision-engine — that is what a real bundle holds. It used to
        # seed an `aqg` body too, which no clone of this repo has ever contained: install.sh clones
        # AQG from its own repo and runs AQG's installer. Seeding it here made the suite prove that
        # a layout which cannot occur works.
        self._seed_component("decision-engine", "de-audit")

        self._env = {
            "DEEPPATTERN_HOME": str(self.deeppattern),
            "CLAUDE_SKILLS_DIR": str(self.skills),
            "CLAUDE_CODE_CONFIG": str(base / ".claude.json"),
            "CODEX_SKILLS_DIR": str(self.codex_skills),
            "CURSOR_CONFIG": str(base / "cursor-home" / "mcp.json"),
            "QODER_CONFIG": str(base / "no-qoder" / "mcp.json"),
            "QODER_SKILLS_DIR": str(base / "no-qoder" / "skills"),
            "QODER_APP_ROOT": str(base / "no-qoder" / "app"),
            "QODER_CN_CONFIG": str(base / "no-qoder-cn" / "settings.json"),
            "QODER_CN_SKILLS_DIR": str(base / "no-qoder-cn" / "skills"),
            "QODER_CN_APP_ROOT": str(base / "no-qoder-cn" / "app"),
            "QODER_IDE_CONFIG": str(base / "no-qoder-ide" / "mcp.json"),
            "QODER_IDE_SETTINGS": str(
                base / "no-qoder-ide" / "settings.json"
            ),
            "QODER_IDE_SKILLS_DIR": str(base / "no-qoder-ide" / "skills"),
            "QODER_IDE_APP_ROOT": str(base / "no-qoder-ide" / "app"),
            "QODER_CN_IDE_CONFIG": str(
                base / "no-qoder-cn-ide" / "mcp.json"
            ),
            "QODER_CN_IDE_SETTINGS": str(
                base / "no-qoder-cn-ide" / "settings.json"
            ),
            "QODER_CN_IDE_SKILLS_DIR": str(
                base / "no-qoder-cn-ide" / "skills"
            ),
            "QODER_CN_IDE_APP_ROOT": str(
                base / "no-qoder-cn-ide" / "app"
            ),
            "TRAE_CONFIG": str(base / "no-trae" / "mcp.json"),
            "TRAE_SKILLS_DIR": str(base / "no-trae" / "skills"),
            "TRAE_APP_ROOT": str(base / "no-trae" / "app"),
            "TRAE_CN_CONFIG": str(base / "no-trae-cn" / "mcp.json"),
            "TRAE_CN_SKILLS_DIR": str(base / "no-trae-cn" / "skills"),
            "TRAE_CN_APP_ROOT": str(base / "no-trae-cn" / "app"),
            "TRAE_WORK_CONFIG": str(
                base / "trae-work-home" / "User" / "mcp.json"
            ),
            "TRAE_WORK_SKILLS_DIR": str(base / "no-trae-work" / "skills"),
            "TRAE_WORK_APP_ROOT": str(base / "no-trae-work" / "app"),
            "TRAE_WORK_CN_CONFIG": str(
                base / "trae-work-cn-home" / "User" / "mcp.json"
            ),
            "TRAE_WORK_CN_SKILLS_DIR": str(
                base / "no-trae-work-cn" / "skills"
            ),
            "TRAE_WORK_CN_APP_ROOT": str(base / "no-trae-work-cn" / "app"),
            "WORKBUDDY_CONFIG": str(base / "no-workbuddy" / "mcp.json"),
            "WORKBUDDY_SKILLS_DIR": str(
                base / "no-workbuddy" / "skills"
            ),
            "WORKBUDDY_APP_ROOT": str(base / "no-workbuddy" / "app"),
            "CODEBUDDY_CONFIG": str(base / "no-codebuddy" / "mcp.json"),
            "CODEBUDDY_SKILLS_DIR": str(
                base / "no-codebuddy" / "skills"
            ),
            "CODEBUDDY_CLI": str(base / "no-codebuddy" / "codebuddy"),
            "WORKBUDDY_AI_CONFIG": str(
                base / "no-workbuddy-ai" / "mcp.json"
            ),
            "WORKBUDDY_AI_SKILLS_DIR": str(
                base / "no-workbuddy-ai" / "skills"
            ),
            "WORKBUDDY_AI_APP_ROOT": str(
                base / "no-workbuddy-ai" / "app"
            ),
            "DE_CONFIG_PATH": str(self.deeppattern / "decision-engine" / "config.json"),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        (base / ".claude.json").touch()

    def _seed_component(self, comp: str, skill: str) -> None:
        skill_dir = self.bundle / comp / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# %s\n" % skill, encoding="utf-8")

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _run(self, target: str, **kw):
        defaults = dict(
            bundle_root=self.bundle,
            server_endpoint="hub.example.com",
            api_key="inv_test",
            device_name="test-dev",
        )
        defaults.update(kw)
        return install.run_install(target, **defaults)

    def test_fixture_isolates_codebuddy_and_workbuddy_ai_skill_routes(self):
        from installer.client_hosts.hosts import codebuddy, workbuddy_ai

        with (
            mock.patch.object(codebuddy, "_codebuddy_installed", return_value=True),
            mock.patch.object(
                workbuddy_ai, "_workbuddy_ai_installed", return_value=True
            ),
        ):
            routes = install.mcp_config.active_skill_routes(setup_only=True)

        base = Path(self.tmp.name)
        self.assertTrue(routes["codebuddy"][0].is_relative_to(base))
        self.assertTrue(routes["workbuddy-ai"][0].is_relative_to(base))

    def test_summary_routes_users_to_agent_permanent_setup(self):
        output = io.StringIO()
        with redirect_stdout(output):
            install._print_summary(
                {
                    "target": "de",
                    "components": [],
                    "de_config": "/protected/config.json",
                }
            )
        rendered = output.getvalue()
        self.assertIn("python3 -m installer.permanent_setup", rendered)
        self.assertNotIn("python3 -m installer.activate", rendered)

    def test_managed_shell_keeps_activation_secret_out_of_argv(self):
        shell = (Path(__file__).resolve().parents[2] / "install.sh").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('--api-key "${DE_ACTIVATION_SECRET}"', shell)
        self.assertIn(
            'install_owner_endpoint="${DE_ENDPOINT:-}"',
            shell,
        )
        self.assertIn('install_owner_secret="${DE_ACTIVATION_SECRET:-}"', shell)
        self.assertIn(
            'DE_ENDPOINT="${owner_endpoint}" DE_ACTIVATION_SECRET="${owner_secret}"',
            shell,
        )
        self.assertIn("run_python -m installer.activate --from-env", shell)
        self.assertGreaterEqual(
            shell.count("unset DE_ENDPOINT DE_ACTIVATION_SECRET"),
            3,
        )
        self.assertLess(
            shell.index('install_owner_secret="${DE_ACTIVATION_SECRET:-}"'),
            shell.index('case "${target}" in'),
        )

    def test_shell_installs_the_host_stopper_agent_for_dev_and_managed_roots(self):
        shell = (Path(__file__).resolve().parents[2] / "install.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'run_python -m installer.stopper_launch_agent install --de-root "${de_home}"',
            shell,
        )
        self.assertIn(
            'run_python -m installer.stopper_launch_agent install --de-root "${managed_root}"',
            shell,
        )
        self.assertEqual(shell.count("installer.stopper_launch_agent install"), 2)

    def test_managed_shell_wires_and_verifies_each_detected_host_before_success(self):
        shell = (Path(__file__).resolve().parents[2] / "install.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("wire_managed_mcp", shell)
        self.assertIn(
            'installer.mcp_config --write --allow-unactivated --client "${client}"',
            shell,
        )
        self.assertIn("mcp_config.entry_status", shell)
        self.assertIn("print(mcp_config.entry_status", shell)
        self.assertIn(
            'print("\\n".join(mcp_config.detect_clients()))',
            shell,
        )
        self.assertNotIn(
            'print("\\\\n".join(mcp_config.detect_clients()))',
            shell,
        )
        self.assertIn('[ "${status}" != "ready" ]', shell)
        self.assertNotIn(
            'run_python -m installer.mcp_config --write ) \\\n'            '    || echo "install: NOTE — could not auto-wire the local DE Lite MCP;',
            shell,
        )

    def test_dev_shell_survives_a_host_stopper_agent_that_cannot_register(self):
        """End-to-end counterpart of the static contract lock: the real shell must COMPLETE.

        Registering the Stopper LaunchAgent is optional (it only lets a sandboxed host wake a
        panel it cannot launch itself) and can legitimately be refused by a locked-down launchd.
        This test used to assert the opposite — that a refusal aborts the install — which threw
        away an otherwise complete installation and told the user to reinstall something that had
        already worked. Inverted deliberately; the exit code and the note are the new contract."""
        repo_root = Path(__file__).resolve().parents[2]
        fake_home = Path(self.tmp.name) / "host-agent-home"
        fake_home.mkdir()
        fake_bin = Path(self.tmp.name) / "host-agent-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *installer.stopper_launch_agent*install*) exit 73 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "HOME": str(fake_home),
            "DEEPPATTERN_HOME": str(fake_home / ".deeppattern"),
            "WITH_AQG": "0",
            "WITH_MCP": "0",
            "DE_DEV_MODE": "1",
            "DE_PYTHON": _bash_path(fake_python),
            "PATH": "%s%s%s"
            % (fake_bin, os.pathsep, environment.get("PATH", "")),
        })

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(
            completed.returncode, 0,
            "an optional step must not fail the install: %s" % completed.stderr[-800:],
        )
        self.assertIn("could not register the Stopper host LaunchAgent", completed.stderr)
        # The user must be able to act on it: what is lost, and the command that retries it.
        self.assertIn("Everything", completed.stderr)
        self.assertIn("installer.stopper_launch_agent", completed.stderr)

    def test_shell_rejects_python_below_312_before_any_installer_module(self):
        repo_root = Path(__file__).resolve().parents[2]
        fake_bin = Path(self.tmp.name) / "old-python-bin"
        fake_bin.mkdir()
        marker = Path(self.tmp.name) / "installer-module-ran"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *sys.version_info*) exit 1 ;;\n"
            "  *installer.*) printf 'ran' > \"$DE_TEST_MARKER\"; exit 93 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "DE_PYTHON": _bash_path(fake_python),
                "DE_TEST_MARKER": str(marker),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Python 3.12+ is required.", completed.stderr)
        self.assertIn("Authorize your installation Agent", completed.stderr)
        self.assertFalse(marker.exists())

    def test_shell_accepts_existing_python_314_when_probes_pass(self):
        repo_root = Path(__file__).resolve().parents[2]
        fake_home = Path(self.tmp.name) / "python-314-home"
        fake_home.mkdir()
        fake_bin = Path(self.tmp.name) / "python-314-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *sys.version_info*) exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(fake_home),
                "DEEPPATTERN_HOME": str(fake_home / ".deeppattern"),
                "WITH_AQG": "0",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_PYTHON": _bash_path(fake_python),
                "PATH": "%s%s%s"
                % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("3.12+ is required", completed.stderr)

    def test_shell_discovers_later_compatible_python_on_path(self):
        """An old first PATH hit must not hide a usable later Python."""
        repo_root = Path(__file__).resolve().parents[2]
        old_bin = Path(self.tmp.name) / "old-python-path"
        new_bin = Path(self.tmp.name) / "new-python-path"
        old_bin.mkdir()
        new_bin.mkdir()
        old_python = old_bin / "python3"
        old_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in *sys.version_info*) exit 1 ;; *) exit 0 ;; esac\n",
            encoding="utf-8",
        )
        old_python.chmod(0o755)
        new_python = new_bin / "python3.12"
        new_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *sys.version_info*) exit 0 ;;\n"
            "  *installer.*) printf 'new' > \"$DE_TEST_MARKER\"; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        new_python.chmod(0o755)
        marker = Path(self.tmp.name) / "selected-python"
        environment = os.environ.copy()
        environment.pop("DE_PYTHON", None)
        environment.update(
            {
                "HOME": str(Path(self.tmp.name) / "home"),
                "DEEPPATTERN_HOME": str(Path(self.tmp.name) / "home" / ".deeppattern"),
                "WITH_AQG": "0",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_TEST_MARKER": str(marker),
                "PATH": "%s%s%s%s%s"
                % (old_bin, os.pathsep, new_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", old_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("3.12+ is required", completed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "new")

    def test_shell_skips_incomplete_python_for_later_ready_candidate(self):
        """Version alone is insufficient when a later interpreter passes every gate."""
        repo_root = Path(__file__).resolve().parents[2]
        incomplete_bin = Path(self.tmp.name) / "incomplete-python-path"
        ready_bin = Path(self.tmp.name) / "ready-python-path"
        incomplete_bin.mkdir()
        ready_bin.mkdir()
        incomplete_python = incomplete_bin / "python3"
        incomplete_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *'import tkinter'*) exit 1 ;;\n"
            "  *installer.*) printf 'incomplete' > \"$DE_TEST_MARKER\"; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        incomplete_python.chmod(0o755)
        ready_python = ready_bin / "python3.13"
        ready_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *installer.*) printf 'ready' > \"$DE_TEST_MARKER\"; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        ready_python.chmod(0o755)
        marker = Path(self.tmp.name) / "selected-ready-python"
        environment = os.environ.copy()
        environment.pop("DE_PYTHON", None)
        environment.update(
            {
                "HOME": str(Path(self.tmp.name) / "ready-home"),
                "DEEPPATTERN_HOME": str(Path(self.tmp.name) / "ready-home" / ".deeppattern"),
                "WITH_AQG": "0",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_TEST_MARKER": str(marker),
                "PATH": "%s%s%s%s%s"
                % (
                    incomplete_bin,
                    os.pathsep,
                    ready_bin,
                    os.pathsep,
                    environment.get("PATH", ""),
                ),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", incomplete_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "ready")

    def test_shell_does_not_execute_relative_path_python_candidates(self):
        repo_root = Path(__file__).resolve().parents[2]
        working_dir = Path(self.tmp.name) / "untrusted-working-directory"
        relative_bin = working_dir / "relative-bin"
        ready_bin = Path(self.tmp.name) / "absolute-ready-bin"
        launcher_bin = Path(self.tmp.name) / "neutral-launcher-bin"
        working_dir.mkdir()
        relative_bin.mkdir()
        ready_bin.mkdir()
        launcher_bin.mkdir()
        executed = Path(self.tmp.name) / "relative-candidate-executed"
        for candidate in (working_dir / "python3.12", relative_bin / "python3.13"):
            candidate.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'executed' > \"$DE_TEST_RELATIVE_MARKER\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            candidate.chmod(0o755)
        ready_python = ready_bin / "python3"
        ready_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        ready_python.chmod(0o755)
        environment = os.environ.copy()
        environment.pop("DE_PYTHON", None)
        environment.update(
            {
                "HOME": str(Path(self.tmp.name) / "relative-home"),
                "DEEPPATTERN_HOME": str(Path(self.tmp.name) / "relative-home" / ".deeppattern"),
                "WITH_AQG": "0",
                "WITH_MCP": "0",
                "DE_DEV_MODE": "1",
                "DE_TEST_RELATIVE_MARKER": str(executed),
                "PATH": "%s%srelative-bin%s%s%s%s"
                % (
                    os.pathsep,
                    os.pathsep,
                    os.pathsep,
                    ready_bin,
                    os.pathsep,
                    environment.get("PATH", ""),
                ),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", launcher_bin, "de"),
            cwd=str(working_dir),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(executed.exists())

    def test_shell_requires_tk_before_starting_de_install(self):
        repo_root = Path(__file__).resolve().parents[2]
        fake_bin = Path(self.tmp.name) / "no-tk-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in *'import tkinter'*) exit 1 ;; *) exit 0 ;; esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment["DE_PYTHON"] = _bash_path(fake_python)

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("masked activation window cannot open", completed.stderr)

    def test_shell_rejects_an_externally_managed_interpreter_before_installing(self):
        """PEP 668: pip is present but refuses every install, so nothing may be recorded.

        This interpreter clears the version, ssl/venv, and `pip --version` gates — it only
        fails when something is actually installed into it, which is far too late. The stub
        answers the marker probe the way Homebrew's python@3.13 does and is otherwise
        healthy, so a pass here would mean the gate is not the thing stopping the run.
        """
        repo_root = Path(__file__).resolve().parents[2]
        fake_bin = Path(self.tmp.name) / "externally-managed-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in *'EXTERNALLY-MANAGED'*) exit 1 ;; *) exit 0 ;; esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment["DE_PYTHON"] = _bash_path(fake_python)

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("externally managed (PEP 668)", completed.stderr)

        # The remedy is handed to a human or an installation Agent to RUN, so asserting that
        # the words appear is not enough — `&&` written as `\&\&`, or a stray connective
        # word, still reads fine and still breaks. `venv` takes ENV_DIR as nargs='+', so a
        # malformed connective becomes an extra target directory: the recovery command
        # silently creates junk directories instead of installing.
        remedy = completed.stderr[completed.stderr.index("-m venv"):].strip()
        self.assertIn("&&", remedy)
        self.assertNotIn("\\&", remedy)
        syntax = subprocess.run(
            [_bash_executable(), "-n"],
            input=remedy,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        # exactly one directory operand for venv, not a swallowed connective
        venv_args = remedy[remedy.index("-m venv") + len("-m venv"):remedy.index("&&")].split()
        self.assertEqual(len(venv_args), 1, venv_args)

    def test_aqg_first_path_does_not_inherit_owner_credentials(self):
        repo_root = Path(__file__).resolve().parents[2]
        aqg_root = Path(self.tmp.name) / "aqg"
        (aqg_root / ".git").mkdir(parents=True)
        (aqg_root / "scripts").mkdir()
        child = aqg_root / "scripts" / "install.sh"
        child.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"${DE_ENDPOINT+x}\" = x ] || "
            "[ \"${DE_ACTIVATION_SECRET+x}\" = x ]; then exit 42; fi\n"
            "cat > \"$(dirname \"$0\")/aqg_doctor.py\" <<'PY'\n"
            "raise SystemExit(0)\n"
            "PY\n",
            encoding="utf-8",
        )
        child.chmod(0o755)
        (aqg_root / "requirements.txt").write_text("", encoding="utf-8")
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "AQG_DEST": str(aqg_root),
                "DE_PYTHON": _bash_path(Path(sys.executable)),
                "DE_ENDPOINT": "https://owner.example",
                "DE_ACTIVATION_SECRET": "synthetic-owner-value",
                "PATH": "%s%s%s"
                % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "aqg"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_managed_shell_gives_credentials_only_to_bootstrap_environment(self):
        repo_root = Path(__file__).resolve().parents[2]
        fake_home = Path(self.tmp.name) / "home"
        fake_home.mkdir()
        fake_bin = Path(self.tmp.name) / "bin-managed"
        fake_bin.mkdir()
        capture = Path(self.tmp.name) / "children.txt"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *installer.bootstrap_managed_install*)\n"
            "    [ \"${DE_ACTIVATION_SECRET:-}\" = synthetic-owner-value ] || exit 41\n"
            "    case \"$*\" in *synthetic-owner-value*) exit 42 ;; esac\n"
            "    mkdir -p \"$HOME/.deeppattern/decision-engine\"\n"
            "    printf 'bootstrap\\n' >> \"$DE_TEST_CAPTURE\"\n"
            "    ;;\n"
            "  *)\n"
            "    [ \"${DE_ACTIVATION_SECRET+x}\" != x ] || exit 43\n"
            "    [ \"${DE_ENDPOINT+x}\" != x ] || exit 44\n"
            "    printf 'clean-child\\n' >> \"$DE_TEST_CAPTURE\"\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(fake_home),
                "WITH_AQG": "0",
                "DE_ENDPOINT": "https://owner.example",
                "DE_ACTIVATION_SECRET": "synthetic-owner-value",
                "DE_TEST_CAPTURE": str(capture),
                "PATH": "%s%s%s"
                % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        events = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(events.count("bootstrap"), 1)
        self.assertGreaterEqual(events.count("clean-child"), 2)

    def test_healthy_aqg_is_reused_without_pull_or_reinstall(self):
        repo_root = Path(__file__).resolve().parents[2]
        aqg_root = Path(self.tmp.name) / "healthy-aqg"
        (aqg_root / ".git").mkdir(parents=True)
        (aqg_root / "scripts").mkdir()
        marker = Path(self.tmp.name) / "unexpected-aqg-action"
        child = aqg_root / "scripts" / "install.sh"
        child.write_text(
            "#!/usr/bin/env bash\nprintf 'installer' > \"$DE_TEST_MARKER\"\nexit 91\n",
            encoding="utf-8",
        )
        child.chmod(0o755)
        (aqg_root / "scripts" / "aqg_doctor.py").write_text("# test stub\n", encoding="utf-8")
        (aqg_root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        fake_bin = Path(self.tmp.name) / "healthy-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/usr/bin/env bash\nprintf 'git' > \"$DE_TEST_MARKER\"\nexit 92\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "AQG_DEST": str(aqg_root),
                "DE_TEST_MARKER": str(marker),
                "PATH": "%s%s%s" % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "aqg"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("reusing it without pull or reinstall", completed.stdout)
        self.assertFalse(marker.exists())

    def test_unhealthy_aqg_repairs_existing_revision_without_git_pull(self):
        repo_root = Path(__file__).resolve().parents[2]
        aqg_root = Path(self.tmp.name) / "repairable-aqg"
        (aqg_root / ".git").mkdir(parents=True)
        (aqg_root / "scripts").mkdir()
        repaired = aqg_root / ".repaired"
        git_marker = Path(self.tmp.name) / "unexpected-git"
        child = aqg_root / "scripts" / "install.sh"
        child.write_text(
            "#!/usr/bin/env bash\nprintf 'ready' > \"$AQG_DEST/.repaired\"\n",
            encoding="utf-8",
        )
        child.chmod(0o755)
        (aqg_root / "scripts" / "aqg_doctor.py").write_text("# test stub\n", encoding="utf-8")
        (aqg_root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        fake_bin = Path(self.tmp.name) / "repair-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"${1:-}\" in\n"
            "  *aqg_doctor.py) [ -f \"$AQG_DEST/.repaired\" ] ; exit $? ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/usr/bin/env bash\nprintf 'git' > \"$DE_TEST_MARKER\"\nexit 92\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "AQG_DEST": str(aqg_root),
                "DE_TEST_MARKER": str(git_marker),
                "PATH": "%s%s%s" % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "aqg"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(repaired.exists())
        self.assertIn("without pulling a new revision", completed.stdout)
        self.assertFalse(git_marker.exists())

    def test_dev_shell_activates_from_environment_without_staging_secret(self):
        repo_root = Path(__file__).resolve().parents[2]
        fake_home = Path(self.tmp.name) / "dev-home"
        fake_home.mkdir()
        fake_bin = Path(self.tmp.name) / "bin-dev"
        fake_bin.mkdir()
        capture = Path(self.tmp.name) / "dev-children.txt"
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *installer.install*)\n"
            "    [ \"${DE_ENDPOINT:-}\" = https://owner.example ] || exit 51\n"
            "    [ \"${DE_ACTIVATION_SECRET+x}\" != x ] || exit 52\n"
            "    printf 'body\\n' >> \"$DE_TEST_CAPTURE\" ;;\n"
            "  *installer.activate*--from-env*)\n"
            "    [ \"${DE_ACTIVATION_SECRET:-}\" = synthetic-owner-value ] || exit 53\n"
            "    case \"$*\" in *synthetic-owner-value*) exit 54 ;; esac\n"
            "    printf 'activation\\n' >> \"$DE_TEST_CAPTURE\" ;;\n"
            "  *)\n"
            "    [ \"${DE_ACTIVATION_SECRET+x}\" != x ] || exit 55\n"
            "    printf 'clean-child\\n' >> \"$DE_TEST_CAPTURE\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(fake_home),
                "WITH_AQG": "0",
                "DE_DEV_MODE": "1",
                "DE_ENDPOINT": "https://owner.example",
                "DE_ACTIVATION_SECRET": "synthetic-owner-value",
                "DE_TEST_CAPTURE": str(capture),
                "PATH": "%s%s%s"
                % (fake_bin, os.pathsep, environment.get("PATH", "")),
            }
        )

        completed = subprocess.run(
            _bash_script_command(repo_root / "install.sh", fake_bin, "de"),
            cwd=str(repo_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        events = capture.read_text(encoding="utf-8").splitlines()
        significant = [event for event in events if event != "clean-child"]
        self.assertEqual(significant, ["body", "activation"])
        self.assertGreaterEqual(events.count("clean-child"), 3)
    def test_summary_reports_isolated_agent_routing_failure(self):
        output = io.StringIO()
        with redirect_stdout(output):
            install._print_summary(
                {
                    "target": "de",
                    "components": [
                        {
                            "component": "decision-engine",
                            "installed": True,
                            "body": "/managed/decision-engine",
                            "routed_skills": ["audit"],
                            "routed_skill_clients": {"claude": ["audit"]},
                            "routing_failures": {
                                "cursor": "foreign skill directory is preserved"
                            },
                        }
                    ],
                }
            )

        rendered = output.getvalue()
        self.assertIn("cursor", rendered)
        self.assertIn("foreign skill directory is preserved", rendered)
        self.assertIn("other agents remain installed", rendered)

    def test_summary_counts_unique_skills_across_successful_agents(self):
        output = io.StringIO()
        with redirect_stdout(output):
            install._print_summary(
                {
                    "target": "de",
                    "components": [
                        {
                            "component": "decision-engine",
                            "installed": True,
                            "body": "/managed/decision-engine",
                            "routed_skills": [],
                            "routed_skill_clients": {
                                "cursor": ["audit", "graphic-explanation"],
                                "codex": ["audit"],
                            },
                            "routing_failures": {},
                        }
                    ],
                }
            )

        self.assertIn("2 skills routed", output.getvalue())

    def test_in_place_install_preserves_the_clone(self):
        # Parallel-to-AQG layout: the clone lives AT ~/.deeppattern/decision-engine, so bundle_root
        # is its PARENT and src == dst. install_component MUST route in place and NEVER rmtree the
        # clone (which carries .git + installer/). This is the data-loss guard.
        clone = self.deeppattern / "decision-engine"
        (clone / "skills" / "de-audit").mkdir(parents=True)
        (clone / "skills" / "de-audit" / "SKILL.md").write_text("# de-audit\n", encoding="utf-8")
        (clone / ".git").mkdir()
        (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (clone / "installer").mkdir()
        (clone / "installer" / "__init__.py").write_text("", encoding="utf-8")

        result = install.install_component("decision-engine", self.deeppattern)  # parent → src == dst

        self.assertTrue(result["in_place"])
        self.assertTrue((clone / ".git" / "HEAD").exists(), "in-place must not delete the clone's .git")
        self.assertTrue((clone / "installer" / "__init__.py").exists(), "must not delete installer/")
        self.assertIn("de-audit", result["routed_skills"])              # routed FROM the clone
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        self.assertEqual(os.path.realpath(self.skills / "de-audit"),
                         str((clone / "skills" / "de-audit").resolve()))   # route points into the clone

    def test_legacy_copy_refuses_to_clobber_a_git_worktree(self):
        # Defense-in-depth (audit 50070df6): even if the shell wrongly routes a real clone through
        # the copy path (src != dst), install.py must REFUSE to rmtree a dst that has .git — so a
        # canonicalization false-negative in install.sh can never delete a checkout.
        dst = self.deeppattern / "decision-engine"
        (dst / ".git").mkdir(parents=True)
        (dst / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        with self.assertRaises(config.ShellError):
            install.install_component("decision-engine", self.bundle)   # bundle/de != dst → copy path
        self.assertTrue((dst / ".git" / "HEAD").exists(), "the git worktree must be untouched")

    def test_install_de_lays_down_de_and_routes_skills(self):
        summary = self._run("de")
        self.assertTrue((self.deeppattern / "decision-engine" / "skills" / "de-audit").is_dir())
        # AQG is NOT laid down here and must not be: install.sh clones it from its own repo to
        # ~/.deeppattern/agent-quality-gates and runs AQG's own installer. This module listing an
        # "aqg" component only ever produced a silent "not in bundle" skip.
        self.assertFalse((self.deeppattern / "aqg").exists())
        # Skills routed as symlinks (POSIX) or junctions (Windows).
        de_link = self.skills / "de-audit"
        self.assertTrue(install._is_skill_route(de_link))
        self.assertEqual(
            de_link.resolve(),
            (self.deeppattern / "decision-engine" / "skills" / "de-audit").resolve(),
        )
        codex_link = self.codex_skills / "de-audit"
        self.assertTrue(install._is_skill_route(codex_link))
        self.assertEqual(
            codex_link.resolve(),
            (self.deeppattern / "decision-engine" / "skills" / "de-audit").resolve(),
        )
        installed = {c["component"]: c for c in summary["components"]}
        self.assertTrue(installed["decision-engine"]["installed"])
        self.assertNotIn("aqg", installed)

    def test_de_config_written_securely_without_secrets(self):
        self._run("de")
        cfg_path = Path(self._env["DE_CONFIG_PATH"])
        self.assertTrue(cfg_path.exists())
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["server_endpoint"], "https://hub.example.com")
        self.assertEqual(cfg["api_key"], "inv_test")
        self.assertEqual(cfg["device_name"], "test-dev")
        self.assertTrue(cfg["device_fingerprint"])  # fingerprint present
        # No token is baked in by the installer — activation fills it later.
        self.assertEqual(cfg["access_token"], "")
        self.assertEqual(cfg["device_id"], "")
        # POSIX mode bits do not model Windows ACL confidentiality. On Windows
        # this config stays below the current user's profile and inherits that
        # profile's ACL; the production writer is unchanged by this portability fix.
        if os.name != "nt":
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_endpoint_normalized_and_required(self):
        # https:// is prepended when scheme missing.
        self._run("de", server_endpoint="myhost")
        cfg = json.loads(Path(self._env["DE_CONFIG_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(cfg["server_endpoint"], "https://myhost")

    def test_missing_required_component_raises_instead_of_installing_nothing(self):
        # install_component returns {"installed": False, "reason": "not in bundle"} for a body it
        # cannot find. For an OPTIONAL body (eaf) that is the design. For decision-engine it means
        # nothing was laid down, no skill routed and no config written — and run_install used to
        # return a clean summary anyway, so a bundle pointed at the wrong directory reported
        # success and installed nothing.
        import shutil
        shutil.rmtree(self.bundle / "decision-engine")
        with self.assertRaises(config.ShellError) as caught:
            self._run("de")
        self.assertIn("decision-engine", str(caught.exception))
        self.assertFalse(Path(self._env["DE_CONFIG_PATH"]).exists())

    def test_aqg_is_not_a_target_of_this_installer(self):
        # install.sh routes its own `aqg` target to a git clone of the AQG repo + AQG's own
        # installer; it never calls this module for AQG. The target here existed but installed
        # nothing (no aqg body in any bundle), so a caller reaching for it got a silent no-op.
        with self.assertRaises(KeyError):
            install.TARGETS["aqg"]
        self.assertNotIn("aqg", install.KNOWN_COMPONENTS)

    def test_install_all_reserves_eaf_but_skips_when_absent(self):
        summary = self._run("all")
        comps = {c["component"]: c for c in summary["components"]}
        self.assertIn("eaf", comps)
        self.assertFalse(comps["eaf"]["installed"])
        self.assertEqual(comps["eaf"]["reason"], "not in bundle")

    def test_idempotent_reinstall(self):
        self._run("de")
        # Second run must not raise and must keep a single route.
        self._run("de")
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        self.assertTrue(install._is_skill_route(self.codex_skills / "de-audit"))

    def test_existing_managed_install_can_repair_only_missing_codex_routes(self):
        root = self.deeppattern / "decision-engine"
        skill = root / "skills" / "de-audit"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# de-audit\n", encoding="utf-8")

        routed = install.repair_codex_skill_routes(root)

        self.assertEqual(routed, ["de-audit"])
        self.assertFalse((self.skills / "de-audit").exists())
        self.assertTrue(install._route_points_to(self.codex_skills / "de-audit", skill))

    def test_existing_install_repair_never_requests_a_blocking_install_lock(self):
        root = self.deeppattern / "decision-engine"
        (root / "skills" / "de-audit").mkdir(parents=True)

        with mock.patch.object(
            install, "install_lock", wraps=install.install_lock
        ) as lock:
            install.repair_codex_skill_routes(root)

        lock.assert_called_once_with(blocking=False)

    def test_nonblocking_install_lock_refuses_when_an_install_is_active(self):
        with install.install_lock():
            with self.assertRaisesRegex(config.ShellError, "install is active"):
                with install.install_lock(blocking=False):
                    self.fail("the nested nonblocking lock must not be acquired")

    def test_windows_install_lock_uses_native_nonblocking_lock(self):
        calls = []
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=lambda _fd, mode, size: calls.append((mode, size)),
        )
        with mock.patch.object(install, "fcntl", None), \
             mock.patch.object(install.os, "name", "nt"), \
             mock.patch.object(install, "deeppattern_home", return_value=self.deeppattern), \
             mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            with install.install_lock(blocking=False):
                pass

        self.assertEqual(calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])

    def test_claude_only_install_does_not_create_a_codex_home(self):
        with mock.patch.object(config, "codex_skills_in_use", return_value=False):
            summary = self._run("de")

        component = summary["components"][0]
        self.assertEqual(set(component["routed_skill_clients"]), {"claude"})
        self.assertFalse(self.codex_skills.exists())

    def test_absent_hosts_do_not_get_agent_configuration_roots(self):
        Path(os.environ["CLAUDE_CODE_CONFIG"]).unlink()
        with (
            mock.patch.dict(os.environ, {"CODEX_SKILLS_DIR": ""}),
            mock.patch.object(
                config, "DEFAULT_CODEX_SKILLS_DIR", Path(self.tmp.name) / ".codex" / "skills"
            ),
        ):
            summary = self._run("de")

        self.assertEqual(summary["components"][0]["routed_skill_clients"], {})
        self.assertFalse(self.skills.exists())
        self.assertFalse(self.codex_skills.exists())
        self.assertFalse((Path(self.tmp.name) / ".codex").exists())
        for name in (".claude", ".cursor", ".trae", ".qoder", ".workbuddy"):
            self.assertFalse((Path(self.tmp.name) / name).exists(), name)
        self.assertFalse(Path(os.environ["CLAUDE_CODE_CONFIG"]).exists())

    def test_empty_codex_skills_override_never_routes_into_cwd(self):
        os.environ["CODEX_SKILLS_DIR"] = ""
        with mock.patch.object(config, "DEFAULT_CODEX_SKILLS_DIR", self.codex_skills):
            self.assertEqual(config.codex_skills_dir(), self.codex_skills)

    def test_cursor_skills_path_honors_nonblank_override_and_safe_default(self):
        with mock.patch.dict(
            os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
        ):
            self.assertEqual(config.cursor_skills_dir(), self.cursor_skills)
        with (
            mock.patch.dict(os.environ, {"CURSOR_SKILLS_DIR": ""}),
            mock.patch.object(
                config, "DEFAULT_CURSOR_SKILLS_DIR", self.cursor_skills
            ),
        ):
            self.assertEqual(config.cursor_skills_dir(), self.cursor_skills)

    def test_codex_foreign_route_failure_is_isolated(self):
        self.codex_skills.mkdir(parents=True)
        foreign = Path(self.tmp.name) / "foreign-skill"
        foreign.mkdir()
        route = self.codex_skills / "de-audit"
        install._create_skill_route(route, foreign)

        summary = self._run("de")

        self.assertTrue(install._route_points_to(route, foreign))
        self.assertTrue((self.deeppattern / "decision-engine").exists())
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        self.assertIn("codex", summary["components"][0]["routing_failures"])

    def test_codex_real_skill_directory_failure_is_isolated(self):
        self.codex_skills.mkdir(parents=True)
        protected = self.codex_skills / "de-audit"
        protected.mkdir()
        sentinel = protected / "user-owned.txt"
        sentinel.write_text("keep", encoding="utf-8")

        summary = self._run("de")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue((self.deeppattern / "decision-engine").exists())
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        self.assertIn("codex", summary["components"][0]["routing_failures"])

    def test_cursor_routes_allowed_skills_only_without_changing_existing_clients(self):
        for skill in ("audit", "discussion-board", "graphic-explanation"):
            self._seed_component("decision-engine", skill)

        with mock.patch.dict(
            os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
        ):
            summary = self._run("de")

        component = summary["components"][0]
        self.assertEqual(
            set(component["routed_skill_clients"]),
            {"claude", "codex", "cursor"},
        )
        self.assertEqual(
            set(component["routed_skills"]),
            {"de-audit", "audit", "discussion-board", "graphic-explanation"},
        )
        for skill in (
            "de-audit",
            "audit",
            "discussion-board",
            "graphic-explanation",
        ):
            self.assertTrue(install._is_skill_route(self.cursor_skills / skill))
        for skill in ("discussion-board", "graphic-explanation"):
            self.assertTrue(install._is_skill_route(self.skills / skill))
            self.assertTrue(install._is_skill_route(self.codex_skills / skill))

    def test_cursor_skill_conflict_isolated_without_overwrite(self):
        protected = self.cursor_skills / "discussion-board"
        protected.mkdir(parents=True)
        sentinel = protected / "user-owned.txt"
        sentinel.write_text("keep", encoding="utf-8")
        self._seed_component("decision-engine", "discussion-board")

        with mock.patch.dict(
            os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
        ):
            summary = self._run("de")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        component = summary["components"][0]
        self.assertIn("cursor", component["routing_failures"])
        self.assertEqual(
            component["routing_failures"]["cursor"],
            "existing user-managed skill entry was preserved",
        )
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        self.assertTrue(install._is_skill_route(self.codex_skills / "de-audit"))

    def test_all_detected_skill_routes_failing_makes_install_fail_after_body_write(self):
        for destination in (self.skills, self.codex_skills):
            protected = destination / "de-audit"
            protected.mkdir(parents=True)
            (protected / "user-owned.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(
            config.ShellError, "no detected Agent skill route could be wired"
        ):
            self._run("de")

        self.assertTrue((self.deeppattern / "decision-engine").is_dir())
        self.assertTrue(
            (self.deeppattern / "decision-engine" / "config.json").is_file()
        )

    def test_verified_legacy_cursor_owned_copy_is_retired_before_link_routing(self):
        managed_root = self.deeppattern / "decision-engine"
        with (
            mock.patch.object(
                install.client_host_ownership,
                "record_file_sha256_if_present",
                return_value="a" * 64,
            ),
            mock.patch(
                "installer.doctor._legacy_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                install.cursor_activation, "uninstall_cursor_owned"
            ) as uninstall,
        ):
            failure = install.retire_legacy_cursor_owned_copy(managed_root)

        self.assertIsNone(failure)
        uninstall.assert_called_once_with(managed_root=managed_root)

    def test_unverifiable_legacy_cursor_owned_copy_is_preserved(self):
        managed_root = self.deeppattern / "decision-engine"
        with (
            mock.patch.object(
                install.client_host_ownership,
                "record_file_sha256_if_present",
                return_value="a" * 64,
            ),
            mock.patch(
                "installer.doctor._legacy_cursor_owned_skills_status",
                return_value="healthy",
            ),
            mock.patch.object(
                install.cursor_activation,
                "uninstall_cursor_owned",
                side_effect=config.ShellError("private third-state detail"),
            ),
        ):
            failure = install.retire_legacy_cursor_owned_copy(managed_root)

        self.assertEqual(
            failure, "legacy owned-copy could not be verified and was preserved"
        )

    def test_modified_legacy_cursor_copy_never_reaches_transactional_uninstall(self):
        managed_root = self.deeppattern / "decision-engine"
        with (
            mock.patch.object(
                install.client_host_ownership,
                "record_file_sha256_if_present",
                return_value="a" * 64,
            ),
            mock.patch(
                "installer.doctor._legacy_cursor_owned_skills_status",
                return_value="user_modified",
            ),
            mock.patch.object(
                install.cursor_activation, "uninstall_cursor_owned"
            ) as uninstall,
        ):
            failure = install.retire_legacy_cursor_owned_copy(managed_root)

        self.assertEqual(
            failure, "legacy owned-copy could not be verified and was preserved"
        )
        uninstall.assert_not_called()

    def test_single_cursor_repair_migrates_owned_copy_then_creates_managed_link(self):
        body = self.deeppattern / "decision-engine"
        source = body / "skills" / "audit"
        source.mkdir(parents=True)
        legacy = self.cursor_skills / "audit"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("legacy owned copy", encoding="utf-8")

        def retire(_root):
            self.assertTrue(legacy.is_dir())
            for child in legacy.iterdir():
                child.unlink()
            legacy.rmdir()
            return None

        with (
            mock.patch.dict(
                os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
            ),
            mock.patch.object(
                install,
                "retire_legacy_cursor_owned_copy",
                side_effect=retire,
            ) as migrate,
            mock.patch.object(
                install.mcp_config,
                "write_entry",
                return_value={"action": "updated"},
            ),
        ):
            result = install.repair_client_integration("cursor", body)

        migrate.assert_called_once_with(body)
        self.assertEqual(result["routed_skills"], ["audit"])
        self.assertTrue(install._route_points_to(legacy, source))

    def test_single_trae_work_cn_repair_creates_only_its_managed_skill_links(self):
        body = self.deeppattern / "decision-engine"
        source = body / "skills" / "audit"
        source.mkdir(parents=True)

        with (
            mock.patch.dict(
                os.environ,
                {"TRAE_WORK_CN_SKILLS_DIR": str(self.trae_cn_skills)},
            ),
            mock.patch.object(
                install.mcp_config,
                "write_entry",
                return_value={"action": "updated"},
            ) as write_entry,
        ):
            result = install.repair_client_integration("trae-work-cn", body)

        write_entry.assert_called_once_with("trae-work-cn", cwd=body)
        self.assertEqual(result["routed_skills"], ["audit"])
        self.assertTrue(
            install._route_points_to(self.trae_cn_skills / "audit", source)
        )
        self.assertFalse((self.skills / "audit").exists())
        self.assertFalse((self.cursor_skills / "audit").exists())

    def test_single_trae_work_cn_repair_preserves_user_owned_skill_directory(self):
        body = self.deeppattern / "decision-engine"
        (body / "skills" / "audit").mkdir(parents=True)
        protected = self.trae_cn_skills / "audit"
        protected.mkdir(parents=True)
        sentinel = protected / "user-owned.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with (
            mock.patch.dict(
                os.environ,
                {"TRAE_WORK_CN_SKILLS_DIR": str(self.trae_cn_skills)},
            ),
            mock.patch.object(install.mcp_config, "write_entry") as write_entry,
            self.assertRaisesRegex(
                config.ShellError, "existing non-symlink skill"
            ),
        ):
            install.repair_client_integration("trae-work-cn", body)

        write_entry.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_shared_trae_skill_destination_is_idempotent_for_both_products(self):
        body = self.deeppattern / "decision-engine"
        source = body / "skills" / "audit"
        source.mkdir(parents=True)
        shared = self.deeppattern / "shared-trae-skills"
        routes = {
            "trae": (shared, frozenset()),
            "trae-work": (shared, frozenset()),
        }
        with mock.patch.object(
            install.mcp_config, "active_skill_routes", return_value=routes
        ):
            first = install.repair_detected_skill_routes(body)
            second = install.repair_detected_skill_routes(body)

        self.assertEqual(set(first.routed), {"trae", "trae-work"})
        self.assertEqual(set(second.routed), {"trae", "trae-work"})
        self.assertEqual(first.failed, {})
        self.assertEqual(second.failed, {})
        self.assertTrue(install._route_points_to(shared / "audit", source))

    def test_shared_trae_skill_destination_preserves_user_directory(self):
        body = self.deeppattern / "decision-engine"
        (body / "skills" / "audit").mkdir(parents=True)
        shared = self.deeppattern / "shared-trae-skills"
        protected = shared / "audit"
        protected.mkdir(parents=True)
        sentinel = protected / "user-owned.txt"
        sentinel.write_text("keep", encoding="utf-8")
        routes = {
            "trae-cn": (shared, frozenset()),
            "trae-work-cn": (shared, frozenset()),
        }
        with mock.patch.object(
            install.mcp_config, "active_skill_routes", return_value=routes
        ):
            result = install.repair_detected_skill_routes(body)

        self.assertEqual(set(result.failed), {"trae-cn", "trae-work-cn"})
        self.assertEqual(result.routed, {})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_explicit_cursor_repair_reports_only_cursor_failure(self):
        body = self.deeppattern / "decision-engine"
        for skill in ("audit", "layer-check"):
            (body / "skills" / skill).mkdir(parents=True)
        protected = self.cursor_skills / "layer-check"
        protected.mkdir(parents=True)
        (protected / "user-owned.txt").write_text("keep", encoding="utf-8")

        with mock.patch.dict(
            os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
        ):
            result = install.repair_detected_skill_routes(body)

        self.assertIn("cursor", result.failed)
        self.assertIn("claude", result.routed)
        self.assertIn("codex", result.routed)
        self.assertFalse(
            (self.cursor_skills / "audit").exists(),
            "a later conflict must not leave earlier Cursor routes behind",
        )
        self.assertEqual(
            (protected / "user-owned.txt").read_text(encoding="utf-8"),
            "keep",
        )

    def test_explicit_repair_isolates_and_redacts_filesystem_failure(self):
        body = self.deeppattern / "decision-engine"
        (body / "skills" / "audit").mkdir(parents=True)
        destinations = {
            "claude": (self.skills, frozenset()),
            "cursor": (self.cursor_skills, frozenset()),
            "codex": (self.codex_skills, frozenset()),
        }

        def route(_component, _root, *, dest_root, excluded_skills):
            if dest_root == self.cursor_skills:
                raise OSError("C:/private/profile/cursor/skills: access denied")
            return ["audit"]

        with (
            mock.patch.object(
                install.mcp_config,
                "active_skill_routes",
                return_value=destinations,
            ),
            mock.patch.object(install, "_preflight_skill_routes"),
            mock.patch.object(install, "_route_skills", side_effect=route),
        ):
            result = install.repair_detected_skill_routes(body)

        self.assertEqual(set(result.routed), {"claude", "codex"})
        self.assertEqual(
            result.failed,
            {"cursor": "skill routing filesystem operation failed"},
        )

    def test_single_host_repair_preflights_skill_conflict_before_mcp_write(self):
        body = self.deeppattern / "decision-engine"
        (body / "skills" / "layer-check").mkdir(parents=True)
        protected = self.cursor_skills / "layer-check"
        protected.mkdir(parents=True)
        (protected / "user-owned.txt").write_text("keep", encoding="utf-8")

        with (
            mock.patch.dict(
                os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
            ),
            mock.patch.object(install.mcp_config, "write_entry") as write_mcp,
            self.assertRaises(config.ShellError),
        ):
            install.repair_client_integration("cursor", body)

        write_mcp.assert_not_called()
        self.assertEqual(
            (protected / "user-owned.txt").read_text(encoding="utf-8"),
            "keep",
        )

    def test_single_host_repair_reports_post_mcp_skill_route_race(self):
        body = self.deeppattern / "decision-engine"
        (body / "skills" / "audit").mkdir(parents=True)
        with (
            mock.patch.dict(
                os.environ, {"CURSOR_SKILLS_DIR": str(self.cursor_skills)}
            ),
            mock.patch.object(
                install.mcp_config,
                "write_entry",
                return_value={"action": "updated"},
            ),
            mock.patch.object(install, "_preflight_skill_routes"),
            mock.patch.object(
                install,
                "_route_skills",
                side_effect=OSError("C:/private/profile changed"),
            ),
        ):
            result = install.repair_client_integration("cursor", body)

        self.assertEqual(result["mcp"], {"action": "updated"})
        self.assertEqual(result["routed_skills"], [])
        self.assertEqual(
            result["failure"], "skill routing filesystem operation failed"
        )

    def test_reinstall_preserves_activation_token(self):
        self._run("de")
        cfg_path = Path(self._env["DE_CONFIG_PATH"])
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["access_token"] = "tok_after_activation"
        cfg["device_id"] = "dev_123"
        config.atomic_write_json(cfg_path, cfg)
        # Re-running the installer must not wipe the credential.
        self._run("de")
        cfg2 = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg2["access_token"], "tok_after_activation")
        self.assertEqual(cfg2["device_id"], "dev_123")

    def test_real_skill_dir_is_preserved_and_reported(self):
        # A pre-existing real (non-symlink) skill dir is never overwritten.
        self.skills.mkdir(parents=True, exist_ok=True)
        (self.skills / "de-audit").mkdir()
        summary = self._run("de")
        self.assertTrue((self.skills / "de-audit").is_dir())
        self.assertIn("claude", summary["components"][0]["routing_failures"])

    def test_token_survives_failure_of_later_component(self):
        # Activate, then force a LATER component's routing to fail. The DE token must already be
        # persisted on disk. (This used to drive the failure through `aqg`, which is no longer a
        # component of this installer — eaf is the later component now, and the invariant is the
        # same one: decision-engine runs first and re-persists the token before anything after it
        # can fail.)
        self._seed_component("eaf", "eaf-run")
        self._run("all")
        cfg_path = Path(self._env["DE_CONFIG_PATH"])
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["access_token"] = "tok_live"
        config.atomic_write_json(cfg_path, cfg)
        # Make eaf's routing clobber-guard trip on the next install.
        (self.skills / "eaf-run").unlink()   # remove our symlink
        (self.skills / "eaf-run").mkdir()    # real dir -> guard raises
        summary = self._run("all")
        # decision-engine ran first and re-persisted the token before eaf failed.
        cfg2 = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg2["access_token"], "tok_live")
        eaf = next(item for item in summary["components"] if item["component"] == "eaf")
        self.assertIn("claude", eaf["routing_failures"])

    def test_prunes_stale_skill_route(self):
        self._run("de")
        self.assertTrue(install._is_skill_route(self.skills / "de-audit"))
        # Drop the de-audit skill from the bundle, add a new one, reinstall.
        import shutil as _sh
        _sh.rmtree(self.bundle / "decision-engine" / "skills" / "de-audit")
        (self.bundle / "decision-engine" / "skills" / "de-brainstorming").mkdir(parents=True)
        (self.bundle / "decision-engine" / "skills" / "de-brainstorming" / "SKILL.md").write_text("x", encoding="utf-8")
        self._run("de")
        # Stale route removed; new route present.
        self.assertFalse((self.skills / "de-audit").exists())
        self.assertFalse(install._is_skill_route(self.skills / "de-audit"))
        self.assertTrue(install._is_skill_route(self.skills / "de-brainstorming"))

    def test_missing_bundle_root_errors(self):
        with self.assertRaises(config.ShellError):
            install.run_install(
                "de",
                bundle_root=Path(self.tmp.name) / "nope",
                server_endpoint="x",
                api_key="y",
                device_name="z",
            )


class SkillRouteHelpersTestCase(unittest.TestCase):
    """Cross-platform skill-route helpers. The Windows branch (directory
    junctions) cannot create a real junction on POSIX CI, so it is covered by
    mocking os.name / _winapi — locking the Windows path's native API argument
    order and error conversion. A Windows-only test also validates real junctions
    end to end; junction creation needs no Developer Mode / Administrator
    (WinError 1314)."""

    def test_create_route_posix_uses_symlink(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        target = base / "src"
        target.mkdir()
        link = base / "link"
        with mock.patch.object(install.os, "name", "posix"):
            install._create_skill_route(link, target)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), str(target.resolve()))

    def test_create_route_windows_invokes_native_junction_api(self):
        # Build Paths before patching os.name — Path() picks Windows/Posix flavour
        # from os.name at construction, and a WindowsPath can't instantiate on
        # POSIX. The helpers only ever str() these + call mocked _winapi.
        link, target = Path("C:/dst/de-audit"), Path("C:/src/de-audit")
        calls = []

        def create_junction(src, dst):
            calls.append((src, dst))

        fake_winapi = types.SimpleNamespace(CreateJunction=create_junction)
        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.dict(sys.modules, {"_winapi": fake_winapi}):
            install._create_skill_route(link, target)
        self.assertEqual(calls, [(str(target), str(link))])

    def test_create_route_windows_converts_native_api_failure(self):
        link, target = Path("C:/dst/x"), Path("C:/src/x")

        def create_junction(src, dst):
            raise OSError("Access is denied.")

        fake_winapi = types.SimpleNamespace(CreateJunction=create_junction)
        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.dict(sys.modules, {"_winapi": fake_winapi}):
            with self.assertRaises(config.ShellError):
                install._create_skill_route(link, target)

    def test_create_route_windows_converts_missing_native_api(self):
        link, target = Path("C:/dst/x"), Path("C:/src/x")
        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.dict(sys.modules, {"_winapi": None}):
            with self.assertRaisesRegex(config.ShellError, "requires CPython"):
                install._create_skill_route(link, target)

    def test_remove_route_windows_uses_rmdir_not_recursive_delete(self):
        path = Path("C:/dst/x")
        removed = []
        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.object(install.os, "rmdir", side_effect=lambda p: removed.append(p)):
            install._remove_skill_route(path)
        self.assertEqual(removed, [path])

    def test_is_route_detects_readable_windows_junction(self):
        # A junction is not reported by is_symlink(), but carries the
        # readable link target — _is_skill_route must still recognize it so an
        # idempotent re-run replaces it instead of hitting the "refuse to
        # clobber a real dir" guard.
        path = Path("C:/dst/x")

        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(install.os, "readlink", return_value=r"C:\src\x"):
            self.assertTrue(install._is_skill_route(path))

    def test_is_route_false_when_windows_reparse_target_is_unreadable(self):
        path = Path("C:/dst/plain")

        with mock.patch.object(install.os, "name", "nt"), \
             mock.patch.object(Path, "is_symlink", return_value=False), \
             mock.patch.object(install.os, "readlink", side_effect=OSError("not a route")):
            self.assertFalse(install._is_skill_route(path))

    @unittest.skipUnless(os.name == "nt", "real directory junction requires Windows")
    def test_real_windows_junction_handles_legal_path_characters_and_safe_removal(self):
        with tempfile.TemporaryDirectory(prefix="de-junction-e2e-") as temp:
            base = Path(temp)
            names = ("ampersand&skill", "bracket[skill]", "space unicode 技能")
            for index, name in enumerate(names):
                with self.subTest(name=name):
                    target = base / ("source-" + name)
                    link = base / ("routed-" + name)
                    target.mkdir()
                    sentinel = target / ("sentinel-%s.txt" % index)
                    sentinel.write_text("keep-target", encoding="utf-8")

                    install._create_skill_route(link, target)
                    self.assertTrue(install._is_skill_route(link))
                    self.assertTrue(install._route_points_to(link, target))
                    self.assertEqual(
                        (link / sentinel.name).read_text(encoding="utf-8"),
                        "keep-target",
                    )

                    install._remove_skill_route(link)
                    self.assertFalse(os.path.lexists(link))
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep-target")

    def test_route_skills_refuses_to_replace_foreign_route(self):
        with tempfile.TemporaryDirectory(prefix="de-route-owner-") as temp:
            base = Path(temp)
            body = base / "body"
            skill = body / "skills" / "de-audit"
            skill.mkdir(parents=True)
            dest = base / "dest"
            dest.mkdir()
            foreign = base / "foreign"
            foreign.mkdir()
            route = dest / skill.name
            install._create_skill_route(route, foreign)

            with mock.patch.object(install, "claude_skills_dir", return_value=dest):
                with self.assertRaisesRegex(config.ShellError, "outside this install"):
                    install._route_skills("decision-engine", body)

            self.assertTrue(install._route_points_to(route, foreign))

    def test_prune_does_not_remove_sibling_prefix_route(self):
        with tempfile.TemporaryDirectory(prefix="de-route-prefix-") as temp:
            base = Path(temp)
            skills_src = base / "skills"
            skills_src.mkdir()
            sibling_target = base / "skills-backup" / "retired"
            sibling_target.mkdir(parents=True)
            dest = base / "dest"
            dest.mkdir()
            route = dest / "retired"
            install._create_skill_route(route, sibling_target)

            install._prune_stale_routes(dest, skills_src, set())

            self.assertTrue(install._route_points_to(route, sibling_target))


if __name__ == "__main__":
    unittest.main()
