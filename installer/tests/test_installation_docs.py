import os
from pathlib import Path
import re
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

    def test_root_readmes_use_only_managed_doctor_entrypoints(self):
        english = ROOT_READMES[0].read_text(encoding="utf-8")
        chinese = ROOT_READMES[1].read_text(encoding="utf-8")
        for body in (english, chinese):
            self.assertIn("dp-doctor.sh", body)
            self.assertIn("dp-doctor.ps1", body)
            self.assertNotIn("python3 -m installer.doctor", body)
            self.assertNotIn("python3 -m installer.leak_scan", body)
            self.assertNotIn("python3 -m unittest", body)
            self.assertIn("installer/README.md", body)

    def test_python_commands_remain_in_the_source_checkout_section(self):
        body = INSTALLER_README.read_text(encoding="utf-8")
        sections = re.split(r"(?m)^(?=## )", body)
        source_sections = [
            section
            for section in sections
            if re.search(r"(?im)^## .*\bsource checkout\b", section)
        ]
        self.assertEqual(len(source_sections), 1)
        source_section = source_sections[0]
        self.assertRegex(
            source_section,
            re.compile(r"\bnot\s+installed-user diagnostic commands\b", re.I),
        )
        non_source_body = body.replace(source_section, "", 1)
        for command in (
            "python3 -m installer.leak_scan",
            "python3 -m unittest",
        ):
            self.assertIn(command, source_section)
            self.assertNotIn(command, non_source_body)
        for root_readme in ROOT_READMES:
            root_body = root_readme.read_text(encoding="utf-8")
            self.assertNotIn("python3 -m installer.leak_scan", root_body)
            self.assertNotIn("python3 -m unittest", root_body)

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

    def test_deepseek_harness_docs_describe_shared_display_capabilities(self):
        installer_row = _table_row(
            INSTALLER_README.read_text(encoding="utf-8"),
            "deepseek-harness",
        )
        self.assertIn("Cordis YAML", installer_row)
        self.assertIn("Audit Stop Panel", installer_row)
        self.assertIn("native macOS app bundle", installer_row)
        english = ROOT_READMES[0].read_text(encoding="utf-8")
        chinese = ROOT_READMES[1].read_text(encoding="utf-8")
        for body in (english, chinese):
            self.assertIn("DeepSeek Harness", body)
            self.assertIn("cordis.patch.yml", body)
            self.assertIn("Stop Panel", body)
        self.assertIn("DeepSeek Harness.app", english)
        self.assertIn("DeepSeek Harness.app", chinese)

    def test_xiaomi_mimo_docs_describe_jsonc_and_shared_display_capabilities(self):
        installer_row = _table_row(
            INSTALLER_README.read_text(encoding="utf-8"),
            "xiaomi-mimo",
        )
        self.assertIn("byte-preserving JSONC", installer_row)
        self.assertIn("Audit Stop Panel", installer_row)
        english = ROOT_READMES[0].read_text(encoding="utf-8")
        chinese = ROOT_READMES[1].read_text(encoding="utf-8")
        for body in (english, chinese):
            self.assertIn("Xiaomi MiMo", body)
            self.assertIn("mimocode.jsonc", body)
            self.assertIn("MIMOCODE_HOME", body)
            self.assertIn("Stop Panel", body)

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
        for body in (path.read_text(encoding="utf-8") for path in SETUP_DOCS):
            self.assertIn("dp-install:", body)
        exit_code_contracts = (
            (
                SETUP_DOCS[0],
                r"\bExit code\s+`0`\s+means\s+success\b",
                r"\bExit code\s+`4`\s+is\s+partial completion,\s+not full success\b",
            ),
            (
                SETUP_DOCS[1],
                r"退出码\s+`0`\s+表示成功",
                r"退出码\s+`4`\s+是部分完成，\s*不属于完整成功",
            ),
        )
        for path, success_pattern, partial_pattern in exit_code_contracts:
            with self.subTest(path=path.name):
                body = path.read_text(encoding="utf-8")
                self.assertRegex(body, re.compile(success_pattern, re.I))
                self.assertRegex(body, re.compile(partial_pattern, re.I))


if __name__ == "__main__":
    unittest.main()
