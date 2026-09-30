import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from installer.client_hosts.registry import CLIENTS, CLIENT_SPECS
from installer.config import ShellError


ROOT = Path(__file__).resolve().parents[2]
SETUP_DOCS = (
    ROOT / "AI_SETUP.md",
    ROOT / "AI_SETUP.zh-CN.md",
)
ROOT_READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
INSTALLER_README = ROOT / "installer" / "README.md"


def _doc_path(value: Path, actual_home: Path) -> str:
    rendered = str(value).replace("\\", "/")
    home = str(actual_home).replace("\\", "/").rstrip("/")
    if rendered == home:
        return "~"
    if rendered.startswith(home + "/"):
        return "~" + rendered[len(home):]
    return rendered


def _windows_contract_paths():
    actual_home = Path.home()
    with (
        mock.patch.object(Path, "home", return_value=Path("~")),
        mock.patch.object(sys, "platform", "win32"),
        mock.patch.dict(
            os.environ,
            {"APPDATA": "%APPDATA%", "LOCALAPPDATA": "%LOCALAPPDATA%"},
        ),
    ):
        return {
            client: {
                "config": _doc_path(spec.config_path(), actual_home),
                "skills": (
                    _doc_path(spec.skills_global_path(), actual_home)
                    if spec.skills_global_path is not None
                    else None
                ),
            }
            for client, spec in CLIENT_SPECS.items()
        }


def _claude_desktop_config_paths():
    actual_home = Path.home()
    spec = CLIENT_SPECS["claude-desktop"]
    rendered = []
    for platform in ("win32", "darwin", "linux"):
        with (
            mock.patch.object(Path, "home", return_value=Path("~")),
            mock.patch.object(sys, "platform", platform),
            mock.patch.dict(os.environ, {"APPDATA": "%APPDATA%"}),
        ):
            rendered.append(_doc_path(spec.config_path(), actual_home))
    return tuple(rendered)


def _table_row(body: str, client: str) -> str:
    marker = f"| `{client}` |"
    rows = [line for line in body.splitlines() if marker in line]
    if len(rows) != 1:
        raise AssertionError(
            f"expected one installation-table row for {client}, found {len(rows)}"
        )
    return rows[0]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class InstallationDocsTestCase(unittest.TestCase):
    def test_dev_install_manual_client_hint_names_every_registered_host(self):
        body = (ROOT / "install.sh").read_text(encoding="utf-8")
        hints = [line for line in body.splitlines() if "--client <" in line]
        self.assertEqual(len(hints), 1)
        for client in CLIENTS:
            with self.subTest(client=client):
                self.assertIn(client, hints[0])

    def test_installer_reference_names_every_registered_host_and_path(self):
        body = INSTALLER_README.read_text(encoding="utf-8")
        expected = _windows_contract_paths()
        for client, contract in expected.items():
            row = _table_row(body, client)
            with self.subTest(client=client):
                self.assertIn(contract["config"], row)
                if contract["skills"] is None:
                    self.assertRegex(row, r"\| (?:none|无) \|")
                else:
                    self.assertIn(contract["skills"], row)

    def test_claude_desktop_reference_lists_every_platform_path(self):
        row = _table_row(
            INSTALLER_README.read_text(encoding="utf-8"), "claude-desktop"
        )
        for value in _claude_desktop_config_paths():
            self.assertIn(value, row)

    def test_root_readmes_present_only_the_two_supported_install_methods(self):
        english = ROOT_READMES[0].read_text(encoding="utf-8")
        chinese = ROOT_READMES[1].read_text(encoding="utf-8")
        for body in (english, chinese):
            self.assertIn("AI_SETUP.md", body)
            self.assertIn("AI_SETUP.zh-CN.md", body)
            self.assertIn("dp-install.sh", body)
            self.assertIn("dp-install.ps1", body)
            self.assertNotIn("WITH_AQG", body)
            self.assertNotIn("WITH_DE", body)
            self.assertNotIn("DE_DEV_MODE", body)
            self.assertNotIn("python3 -m installer.permanent_setup", body)
            self.assertNotIn(
                "git clone https://github.com/deeppatternai/decision-engine.git",
                body,
            )
            self.assertIn("Linux", body)
            self.assertIn("WSL", body)
        self.assertIn("### Option 1:", english)
        self.assertIn("### Option 2:", english)
        self.assertIn("#### Linux desktop", english)
        self.assertIn("### 方式一：", chinese)
        self.assertIn("### 方式二：", chinese)
        self.assertIn("#### Linux 桌面", chinese)

    def test_installer_launch_rows_match_host_specs(self):
        body = INSTALLER_README.read_text(encoding="utf-8")
        policy_labels = {
            "direct-python-v1": "direct Python",
            "desktop-python-v1": "desktop Python",
            "windows-py-no-space-v1": "Windows space-safe Python",
            "absolute-bootstrap-v1": "installer/mcp_bootstrap.py",
        }
        for client, spec in CLIENT_SPECS.items():
            row = _table_row(body, client)
            with self.subTest(client=client):
                self.assertIn(policy_labels[spec.launch_policy], row)
                if spec.config_format == "json":
                    cwd_label = "includes `cwd`" if spec.json_include_cwd else "omits `cwd`"
                    type_label = "includes `type`" if spec.json_include_type else "omits `type`"
                    self.assertIn(cwd_label, row)
                    self.assertIn(type_label, row)

    def test_desktop_only_hosts_keep_linux_write_guards(self):
        for client in (
            "qoder",
            "qoder-cn",
            "trae",
            "trae-work",
            "trae-cn",
            "trae-work-cn",
            "workbuddy",
            "workbuddy-ai",
        ):
            with self.subTest(client=client):
                with mock.patch.object(sys, "platform", "linux"):
                    with self.assertRaises(ShellError):
                        CLIENT_SPECS[client].config_write_guard_probe()

    def test_setup_guides_lock_source_priority_and_do_not_search_locally(self):
        english = _normalized(SETUP_DOCS[0])
        chinese = _normalized(SETUP_DOCS[1])
        self.assertIn("Select exactly one source", english)
        self.assertIn("explicitly attaches one matching installer", english)
        self.assertIn("Only files explicitly listed as attachments", english)
        self.assertIn("Do not search workspaces, projects, home directories", english)
        self.assertIn("选择唯一安装来源", chinese)
        self.assertIn("当前消息明确上传了一份对应安装脚本", chinese)
        self.assertIn("只有当前消息附件清单明确列出的文件", chinese)
        self.assertIn("不得在工作区、项目、用户目录、最近文件", chinese)
        for body in (english, chinese):
            self.assertIn("INSTALLER_SOURCE_AMBIGUOUS", body)

    def test_setup_guides_lock_platform_commands_and_bsd_mktemp(self):
        for path in SETUP_DOCS:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn('bash "<', body)
                self.assertIn('>" --agent-terminal', body)
                self.assertIn('p="$(mktemp /tmp/dp-install.XXXXXX)"', body)
                self.assertNotIn("/tmp/dp-install.XXXXXX.sh", body)
                self.assertIn("curl -fsSL https://raw.githubusercontent.com/", body)
                self.assertIn("dp-install.sh | bash", body)
                self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass", body)

    def test_setup_guides_lock_macos_permission_and_fallback_state_machine(self):
        english = _normalized(SETUP_DOCS[0])
        chinese = _normalized(SETUP_DOCS[1])
        for body in (english, chinese):
            self.assertIn("AGENT_FULL_ACCESS_REQUIRED", body)
            self.assertIn("VISIBLE_TERMINAL_HANDOFF_UNSUPPORTED", body)
            self.assertIn("VISIBLE_TERMINAL_HANDOFF_BLOCKED", body)
            self.assertIn("WorkBuddy", body)
            self.assertIn("WorkBuddy AI", body)
            self.assertIn("--agent-terminal", body)
        self.assertIn("attempt the original automatic handoff exactly once", english)
        self.assertIn("include the one applicable manual command", english)
        self.assertIn("只执行原自动交接命令一次", chinese)
        self.assertIn("给出下述唯一人工命令", chinese)

    def test_setup_guides_lock_windows_permission_and_terminal_handoff(self):
        english = _normalized(SETUP_DOCS[0])
        chinese = _normalized(SETUP_DOCS[1])
        for body in (english, chinese):
            self.assertIn("AGENT_FULL_ACCESS_REQUIRED", body)
            self.assertIn("VISIBLE_TERMINAL_HANDOFF_BLOCKED", body)
            self.assertIn("-AgentTerminal", body)
            self.assertIn("-AgentActivate", body)
            self.assertIn("-Activate", body)
            self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass", body)
        self.assertIn(
            "On native Windows, use the Agent host's own Full Access mode",
            english,
        )
        self.assertIn(
            "Until the user explicitly confirms it in the current conversation",
            english,
        )
        self.assertIn("attempt the documented `-AgentTerminal` or `-AgentActivate` handoff exactly once", english)
        self.assertIn("原生 Windows 使用 Agent 宿主自身的“完全访问”模式", chinese)
        self.assertIn("用户尚未在当前会话明确确认", chinese)
        self.assertIn("确认后只自动尝试一次文档化的 `-AgentTerminal` 或 `-AgentActivate` 交接", chinese)

    def test_setup_guides_distinguish_managed_and_manual_activation(self):
        for path in SETUP_DOCS:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("--agent-activate", body)
                self.assertIn("--activate", body)
                self.assertIn("--agent-terminal", body)
                self.assertIn("python3 -m installer.permanent_setup", body)
        self.assertIn(
            "Do not invoke `python3 -m installer.permanent_setup` directly",
            SETUP_DOCS[0].read_text(encoding="utf-8"),
        )
        self.assertIn(
            "不得直接运行 `python3 -m installer.permanent_setup`",
            SETUP_DOCS[1].read_text(encoding="utf-8"),
        )

    def test_setup_guides_require_real_result_and_keep_secrets_out_of_commands(self):
        for path in SETUP_DOCS + ROOT_READMES:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("DE_ACTIVATION_SECRET=<", body)
                self.assertNotRegex(body, r"--api-key\s+<")
        english = SETUP_DOCS[0].read_text(encoding="utf-8")
        chinese = SETUP_DOCS[1].read_text(encoding="utf-8")
        for body in (english, chinese):
            self.assertIn("dp-install:", body)
        self.assertIn("numeric exit code", english)
        self.assertIn("数字退出码", chinese)


if __name__ == "__main__":
    unittest.main()
