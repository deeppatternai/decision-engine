import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from installer.client_hosts.hosts import (
    qoder,
    qoder_cn,
    qoder_cn_ide,
    qoder_ide,
    trae,
    trae_cn,
    trae_work,
    trae_work_cn,
    workbuddy_ai,
)
from installer.client_hosts.registry import CLIENTS, CLIENT_SPECS
from installer.config import ShellError


ROOT = Path(__file__).resolve().parents[2]
SETUP_DOCS = (
    ROOT / "AI_SETUP.md",
    ROOT / "AI_SETUP.zh-CN.md",
)
ROOT_READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
HOST_MATRIX_DOCS = SETUP_DOCS + (ROOT / "installer" / "README.md",)


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
            {
                "APPDATA": "%APPDATA%",
                "LOCALAPPDATA": "%LOCALAPPDATA%",
            },
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


def _darwin_contract_paths():
    actual_home = Path.home()
    with (
        mock.patch.object(Path, "home", return_value=Path("~")),
        mock.patch.object(sys, "platform", "darwin"),
        mock.patch.dict(os.environ, {}, clear=True),
    ):
        return {
            client: _doc_path(spec.config_path(), actual_home)
            for client, spec in CLIENT_SPECS.items()
        }


def _table_row(body: str, client: str) -> str:
    marker = f"| `{client}` |"
    rows = [line for line in body.splitlines() if marker in line]
    if len(rows) != 1:
        raise AssertionError(
            f"expected one installation-table row for {client}, found {len(rows)}"
        )
    return rows[0]


class InstallationDocsTestCase(unittest.TestCase):
    def test_dev_install_manual_client_hint_names_every_registered_host(self):
        body = (ROOT / "install.sh").read_text(encoding="utf-8")
        hints = [line for line in body.splitlines() if "--client <" in line]
        self.assertEqual(len(hints), 1)
        for client in CLIENTS:
            with self.subTest(client=client):
                self.assertIn(client, hints[0])

    def test_detailed_installation_docs_name_every_registered_host(self):
        for path in HOST_MATRIX_DOCS:
            body = path.read_text(encoding="utf-8")
            for client in CLIENTS:
                with self.subTest(path=path.name, client=client):
                    self.assertIn(f"`{client}`", _table_row(body, client))

    def test_setup_and_installer_tables_use_registered_paths(self):
        expected = _windows_contract_paths()
        for path in HOST_MATRIX_DOCS:
            body = path.read_text(encoding="utf-8")
            for client, contract in expected.items():
                row = _table_row(body, client)
                with self.subTest(path=path.name, client=client):
                    self.assertIn(contract["config"], row)
                    if contract["skills"] is None:
                        self.assertRegex(row, r"\| (?:none|无) \|")
                    else:
                        self.assertIn(contract["skills"], row)

    def test_claude_desktop_table_lists_every_platform_path(self):
        expected = _claude_desktop_config_paths()
        for path in HOST_MATRIX_DOCS:
            row = _table_row(path.read_text(encoding="utf-8"), "claude-desktop")
            with self.subTest(path=path.name):
                for value in expected:
                    self.assertIn(value, row)

    def test_setup_tables_list_macos_paths_for_desktop_hosts(self):
        expected = _darwin_contract_paths()
        for path in HOST_MATRIX_DOCS:
            body = path.read_text(encoding="utf-8")
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
                with self.subTest(path=path.name, client=client):
                    self.assertIn(expected[client], _table_row(body, client))

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
            self.assertNotIn("Linux", body)
            self.assertNotIn("WSL", body)
        self.assertNotIn("Expand all 16 products", english)
        self.assertNotIn("展开查看全部 16 个产品", chinese)

    def test_installer_launch_rows_match_host_specs(self):
        body = (ROOT / "installer" / "README.md").read_text(encoding="utf-8")
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

    def test_documented_version_floors_come_from_host_modules(self):
        floors = {
            "qoder-ide": qoder_ide.qoder_ide_common.MINIMUM_VERSION,
            "qoder-cn-ide": qoder_cn_ide.qoder_ide_common.MINIMUM_VERSION,
            "trae-work": trae_work._MINIMUM_VERSION,
            "trae-work-cn": trae_work_cn._MINIMUM_VERSION,
            "workbuddy-ai": workbuddy_ai._MINIMUM_VERSION,
        }
        for path in SETUP_DOCS:
            body = path.read_text(encoding="utf-8")
            for client, version in floors.items():
                with self.subTest(path=path.name, client=client):
                    self.assertIn(".".join(map(str, version)) + "+", _table_row(body, client))

        for path in SETUP_DOCS:
            row = _table_row(path.read_text(encoding="utf-8"), "qoder")
            self.assertIn("Windows Desktop 1.106.3+", row)
            self.assertIn("macOS Qoder.app 0.1.3+", row)
            body = path.read_text(encoding="utf-8")
            self.assertIn("macOS Qoder CN.app 0.1.4", _table_row(body, "qoder-cn"))
            self.assertIn("macOS Trae.app 3.5.81", _table_row(body, "trae"))
            self.assertIn("macOS Trae CN.app 3.3.95", _table_row(body, "trae-cn"))

    def test_desktop_only_rows_match_linux_write_guards(self):
        clients = (
            "qoder",
            "qoder-cn",
            "trae",
            "trae-work",
            "trae-cn",
            "trae-work-cn",
            "workbuddy",
            "workbuddy-ai",
        )
        for client in clients:
            spec = CLIENT_SPECS[client]
            with self.subTest(client=client, surface="guard"):
                with mock.patch.object(sys, "platform", "linux"):
                    with self.assertRaises(ShellError):
                        spec.config_write_guard_probe()
            for path in SETUP_DOCS:
                with self.subTest(client=client, path=path.name):
                    row = _table_row(path.read_text(encoding="utf-8"), client)
                    if client == "qoder":
                        self.assertIn("Windows Desktop", row)
                        self.assertIn("macOS Qoder.app", row)
                    elif client in {"qoder-cn", "trae", "trae-cn", "workbuddy-ai"}:
                        self.assertIn("macOS", row)
                    else:
                        self.assertIn("Windows and macOS Desktop", row)

    def test_bilingual_setup_guides_state_desktop_followup_boundary(self):
        english = (ROOT / "AI_SETUP.md").read_text(encoding="utf-8")
        chinese = (ROOT / "AI_SETUP.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn(
            "Graphic Explanation popup follow-up is provided through the API",
            english,
        )
        self.assertIn("does not require a\nhost CLI", english)
        self.assertIn("Graphic Explanation 弹窗右侧追问通过 API 提供", chinese)
        self.assertIn("不需要 host CLI", chinese)

        for client in (
            "qoder",
            "qoder-cn",
            "trae",
            "trae-work",
            "trae-cn",
            "trae-work-cn",
            "workbuddy",
            "workbuddy-ai",
            "codebuddy",
        ):
            self.assertFalse(CLIENT_SPECS[client].popup_followup)

    def test_bilingual_guides_document_workbuddy_gui_routing(self):
        english = (ROOT / "AI_SETUP.md").read_text(encoding="utf-8")
        chinese = (ROOT / "AI_SETUP.zh-CN.md").read_text(encoding="utf-8")
        english_normalized = " ".join(english.split())
        chinese_normalized = "".join(chinese.split())
        readme_english = " ".join(
            ROOT_READMES[0].read_text(encoding="utf-8").split()
        )
        readme_chinese = "".join(
            ROOT_READMES[1].read_text(encoding="utf-8").split()
        )

        self.assertIn("current host is WorkBuddy", english_normalized)
        self.assertIn("dangerouslyDisableSandbox", english_normalized)
        self.assertIn("DE_WORKBUDDY_SETUP=1", english_normalized)
        self.assertIn("masked Tk form", english_normalized)
        self.assertIn("user approval", english_normalized)
        self.assertNotIn("DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED=1", english_normalized)
        self.assertIn("normal desktop terminal", english_normalized)
        self.assertIn("every other host", english_normalized)
        self.assertIn(
            "Do not generate or open a `.command` wrapper through Finder",
            english_normalized,
        )
        self.assertIn("still with `dangerouslyDisableSandbox: true`", english_normalized)
        self.assertIn(
            "If that wrapper is launched from a sandboxed WorkBuddy action",
            english_normalized,
        )
        self.assertIn(
            "the Sandbox can block the handoff to Terminal before Python starts",
            english_normalized,
        )
        self.assertIn(
            "any resulting permission dialog is not the activation UI",
            english_normalized,
        )
        self.assertNotIn("Gatekeeper can block", english_normalized)
        self.assertNotIn("macOS may show a system dialog", english_normalized)
        self.assertIn("setup opens the masked pywebview form", english_normalized)
        self.assertIn("uses Tk only as a fallback", english_normalized)
        self.assertIn(
            "On macOS, permanent setup opens a masked pywebview", readme_english
        )
        self.assertIn(
            "WorkBuddy opens the supported masked Tk form directly", readme_english
        )

        self.assertIn("当前宿主是WorkBuddy", chinese_normalized)
        self.assertIn("dangerouslyDisableSandbox", chinese_normalized)
        self.assertIn("DE_WORKBUDDY_SETUP=1", chinese_normalized)
        self.assertIn("掩码Tk表单", chinese_normalized)
        self.assertIn("用户批准", chinese_normalized)
        self.assertNotIn("DE_WORKBUDDY_OUTSIDE_SANDBOX_APPROVED=1", chinese_normalized)
        self.assertIn("普通桌面终端", chinese_normalized)
        self.assertIn("其他宿主", chinese_normalized)
        self.assertIn("不得生成或通过Finder打开`.command`包装文件", chinese_normalized)
        self.assertIn("仍需按上文说明，在用户批准后设置`dangerouslyDisableSandbox:true`", chinese_normalized)
        self.assertIn(
            "如果该包装文件由处于sandbox内的WorkBuddy操作发起",
            chinese_normalized,
        )
        self.assertIn("Sandbox可能会在Python启动前阻止它转交给Terminal", chinese_normalized)
        self.assertIn("由此出现的权限对话框不是激活界面", chinese_normalized)
        self.assertNotIn("Gatekeeper可能", chinese_normalized)
        self.assertNotIn("macOS可能会在Python启动前显示系统对话框", chinese_normalized)
        self.assertIn("命令成功启动后会打开掩码pywebview表单", chinese_normalized)
        self.assertIn("才回退Tk", chinese_normalized)
        self.assertIn(
            "macOS上的永久配置会打开掩码pywebview桌面窗口", readme_chinese
        )
        self.assertIn(
            "WorkBuddy会直接打开受支持的掩码Tk表单", readme_chinese
        )

    def test_setup_guides_reuse_any_verified_python_at_or_above_floor(self):
        english = " ".join((ROOT / "AI_SETUP.md").read_text(encoding="utf-8").split())
        chinese = " ".join((ROOT / "AI_SETUP.zh-CN.md").read_text(encoding="utf-8").split())
        self.assertIn("Reuse any existing Python", english)
        self.assertIn(">=3.12 that passes those probes", english)
        self.assertIn("a newer version is valid and must not trigger installation or downgrade", english)
        self.assertIn("复用现有的 Python 3.12 或更高版本", chinese)
        self.assertIn("更高版本同样有效", chinese)
        self.assertIn("不得因此要求安装或", chinese)
        self.assertIn("This applies to native Windows and macOS/Linux", english)
        self.assertIn("本要求同时适用于原生 Windows 和 macOS/Linux", chinese)
        for body in (english, chinese):
            self.assertNotIn("3.13", body)
            self.assertNotIn("3.14", body)
            self.assertNotIn("3.15", body)

    def test_setup_guides_lock_path_discovery_and_localized_choice_fallback(self):
        english = " ".join((ROOT / "AI_SETUP.md").read_text(encoding="utf-8").split())
        chinese = " ".join((ROOT / "AI_SETUP.zh-CN.md").read_text(encoding="utf-8").split())
        self.assertIn("Do not treat the first `python3` on `PATH` as the complete machine check", english)
        self.assertIn("python3.*", english)
        self.assertIn("structured choice prompt", english)
        self.assertIn("English-language flow", english)
        self.assertIn("literal answers `Yes` or `No`", english)
        self.assertIn("不要把 `PATH` 中第一个 `python3` 当成整台电脑的唯一检查结果", chinese)
        self.assertIn("python3.*", chinese)
        self.assertIn("结构化选项弹窗", chinese)
        self.assertIn("“是”或“否”", chinese)

    def test_public_install_examples_do_not_put_activation_secret_in_argv_or_shell(self):
        for path in ROOT_READMES:
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("DE_ACTIVATION_SECRET=<", body)
                self.assertNotRegex(body, r"--api-key\s+<")
                self.assertIn("dp-install.sh", body)
                self.assertIn("dp-install.ps1", body)

        for path in (ROOT / "installer" / "README.md", ROOT / "install.sh"):
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("DE_ACTIVATION_SECRET=<", body)
                self.assertNotRegex(body, r"--api-key\s+<")
                self.assertIn(
                    '$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup',
                    body,
                )


if __name__ == "__main__":
    unittest.main()
