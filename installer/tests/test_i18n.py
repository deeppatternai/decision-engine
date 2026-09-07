"""Behavior tests for the single language-resolution point (client.i18n) and the split catalog.

Two contracts are pinned here:
  * resolve_locale follows exactly one order — explicit → $DE_UI_LOCALE → system → en-US — and an
    unrecognized candidate is SKIPPED (never swallows the next one). detect_system_locale is
    monkeypatched to None so the default path does not follow the CI machine's locale.
  * C / POSIX / C.UTF-8 are treated as "no language signal", not English.
  * macOS UI language probing honors AppleLanguages first, then AppleLocale, so a Chinese desktop
    still resolves zh-CN even when Codex launches Python with C.UTF-8 env vars.
  * the per-language files (client/locales/en_US.py, zh_CN.py) expose identical key sets for every
    table — the parity guard against a string being added to one language but not the other.
"""

from __future__ import annotations

import unittest
from unittest import mock
import locale

from client import i18n
from client.locales import en_US, zh_CN


class ResolveLocaleTests(unittest.TestCase):
    def setUp(self):
        # Pin the system-language step OFF so the default path is deterministic on any machine.
        self._sys = mock.patch.object(i18n, "detect_system_locale", return_value=None)
        self._sys.start()
        self.addCleanup(self._sys.stop)
        self._env = mock.patch.dict("os.environ", {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        import os
        os.environ.pop("DE_UI_LOCALE", None)

    def test_explicit_wins(self):
        self.assertEqual(i18n.resolve_locale("zh-CN"), "zh-CN")
        self.assertEqual(i18n.resolve_locale("en_US"), "en-US")
        self.assertEqual(i18n.resolve_locale("zh-Hant-TW"), "zh-CN")  # any zh-* → zh-CN

    def test_unrecognized_explicit_is_skipped_not_defaulted(self):
        # "fr-FR" is not en/zh; with system pinned off and no env, it must fall through to en-US.
        self.assertEqual(i18n.resolve_locale("fr-FR"), "en-US")

    def test_c_like_locale_tags_are_skipped(self):
        for value in ("C", "POSIX", "C.UTF-8"):
            with self.subTest(value=value):
                self.assertIsNone(i18n._normalize_tag(value))

    def test_env_used_when_no_explicit(self):
        with mock.patch.dict("os.environ", {"DE_UI_LOCALE": "zh-CN"}):
            self.assertEqual(i18n.resolve_locale(None), "zh-CN")

    def test_explicit_overrides_env(self):
        with mock.patch.dict("os.environ", {"DE_UI_LOCALE": "zh-CN"}):
            self.assertEqual(i18n.resolve_locale("en-US"), "en-US")

    def test_system_used_when_no_explicit_no_env(self):
        with mock.patch.object(i18n, "detect_system_locale", return_value="zh-CN"):
            self.assertEqual(i18n.resolve_locale(None), "zh-CN")

    def test_final_default_is_en_us(self):
        self.assertEqual(i18n.resolve_locale(None), "en-US")

    def test_is_zh(self):
        self.assertTrue(i18n.is_zh("zh-CN"))
        self.assertFalse(i18n.is_zh("en-US"))

    def test_accept_language_is_the_resolved_tag(self):
        # The Accept-Language header value is exactly the resolved locale: a single BCP-47
        # tag, no q-weights. Explicit wins; an unrecognized explicit falls through the chain.
        self.assertEqual(i18n.accept_language("zh-CN"), "zh-CN")
        self.assertEqual(i18n.accept_language("en_US"), "en-US")
        self.assertEqual(i18n.accept_language("zh-Hant-TW"), "zh-CN")
        # System pinned off + no env: unrecognized explicit and no-arg both land on en-US.
        self.assertEqual(i18n.accept_language("fr-FR"), "en-US")
        self.assertEqual(i18n.accept_language(), "en-US")

    def test_accept_language_follows_env_when_no_explicit(self):
        with mock.patch.dict("os.environ", {"DE_UI_LOCALE": "zh-CN"}):
            self.assertEqual(i18n.accept_language(), "zh-CN")


class DetectSystemLocaleTests(unittest.TestCase):
    """detect_system_locale precedence, especially the Windows-vs-POSIX-env order.

    On Windows the OS UI language is the operator's real preference; Git Bash / MSYS inject
    LANG=en_US.UTF-8 on a zh-CN machine, so the POSIX env group must NOT outrank the Windows
    UI-language probe. Off Windows the POSIX env order is authoritative and unchanged.
    """

    def _clear_env(self):
        # Wipe every locale var this function inspects so each case starts from a known blank.
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import os
        for var in ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"):
            os.environ.pop(var, None)

    def _defaults_run(self, **values):
        def run(command, capture_output=True, text=True, check=False):
            key = command[-1]
            return mock.Mock(
                returncode=0 if key in values else 1,
                stdout=values.get(key, ""),
            )

        return run

    def test_macos_apple_languages_beats_c_utf8_env(self):
        # Codex may launch Python with C.UTF-8, but on macOS the real UI language is still the
        # machine's AppleLanguages preference, not the process locale.
        self._clear_env()
        with mock.patch.object(i18n.sys, "platform", "darwin"), \
             mock.patch.dict("os.environ", {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}), \
             mock.patch.object(i18n.subprocess, "run", side_effect=self._defaults_run(
                 AppleLanguages='(\n    "zh-Hans-CN",\n    "en-US"\n)\n',
             )):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_macos_apple_locale_fallback_is_normalized(self):
        self._clear_env()
        with mock.patch.object(i18n.sys, "platform", "darwin"), \
             mock.patch.object(i18n.subprocess, "run", side_effect=self._defaults_run(
                 AppleLocale="zh_CN\n",
             )):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_real_lc_all_language_wins_even_on_macos(self):
        self._clear_env()
        for lc_all, expected in (("en_US.UTF-8", "en-US"), ("zh_CN.UTF-8", "zh-CN")):
            with self.subTest(lc_all=lc_all), \
                 mock.patch.object(i18n.sys, "platform", "darwin"), \
                 mock.patch.dict("os.environ", {"LC_ALL": lc_all}), \
                 mock.patch.object(i18n.subprocess, "run") as run:
                self.assertEqual(i18n.detect_system_locale(), expected)
                run.assert_not_called()

    def test_windows_ui_language_beats_posix_lang(self):
        # REGRESSION: zh-CN Windows + Git Bash LANG=en_US.UTF-8 must resolve zh-CN, not en-US.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LANG": "en_US.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value="zh-CN"):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_windows_falls_back_to_env_when_ui_probe_none(self):
        # Windows probe unreadable (returns None) → the POSIX env group is still honored.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LANG": "zh_CN.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value=None):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_detect_other_windows_lang_falls_through_to_env(self):
        # A ja-JP Windows (probe returns None for non-zh/non-en) must not swallow a usable env tag.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LANG": "en_US.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value=None):
            self.assertEqual(i18n.detect_system_locale(), "en-US")

    def test_detect_non_windows_keeps_env_first(self):
        # Off Windows the Windows probe is never consulted; POSIX env stays authoritative.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "posix"), \
             mock.patch.dict("os.environ", {"LANG": "en_US.UTF-8"}):
            self.assertEqual(i18n.detect_system_locale(), "en-US")

    def test_linux_c_utf8_env_does_not_become_english(self):
        # Linux / WSL: C.UTF-8 is still "no language signal", so if every locale env is C-like the
        # resolver must stop before the stdlib fallback can manufacture English.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "posix"), \
             mock.patch.dict("os.environ", {
                 "LC_ALL": "C.UTF-8",
                 "LC_CTYPE": "C.UTF-8",
                 "LANG": "C.UTF-8",
                 "LANGUAGE": "C.UTF-8",
             }), \
             mock.patch.object(locale, "getlocale", return_value=("en_US", "UTF-8")) as getlocale:
            self.assertIsNone(i18n.detect_system_locale())
            getlocale.assert_not_called()

    def test_linux_language_list_skips_a_c_like_lead_in(self):
        # LANGUAGE is a priority list; a C-like first entry must not block a later supported tag.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "posix"), \
             mock.patch.dict("os.environ", {"LANGUAGE": "C.UTF-8:zh_CN.UTF-8"}):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_lc_all_overrides_windows_ui(self):
        # LC_ALL is the deliberate "override everything" hammer (Git Bash never injects it) and must
        # beat the Windows UI probe, so an operator can still force English on a zh-CN Windows.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LC_ALL": "en_US.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value="zh-CN"):
            self.assertEqual(i18n.detect_system_locale(), "en-US")

    def test_lc_ctype_does_not_override_windows_ui(self):
        # LC_CTYPE is injectable (Git Bash sets it), so it must NOT beat the Windows UI probe —
        # otherwise the injected-tag bug this reorder fixes would reappear via LC_CTYPE.
        self._clear_env()
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LC_CTYPE": "en_US.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value="zh-CN"):
            self.assertEqual(i18n.detect_system_locale(), "zh-CN")

    def test_accept_language_reaches_header_on_zh_windows_with_injected_lang(self):
        # End-of-chain: the value that graphic-explanation sends as its Accept-Language header is
        # accept_language() → resolve_locale() → detect_system_locale(). Pin the exact reported
        # scenario (zh-CN Windows + Git Bash LANG=en_US.UTF-8, no DE_UI_LOCALE) and assert the
        # header value the popup render would receive is zh-CN, not en-US.
        self._clear_env()
        import os
        os.environ.pop("DE_UI_LOCALE", None)
        with mock.patch.object(i18n.os, "name", "nt"), \
             mock.patch.dict("os.environ", {"LANG": "en_US.UTF-8"}), \
             mock.patch.object(i18n, "_detect_windows_ui_language", return_value="zh-CN"):
            self.assertEqual(i18n.accept_language(), "zh-CN")


class CatalogParityTests(unittest.TestCase):
    def _assert_same_shape(self, en, zh, path):
        self.assertEqual(type(en), type(zh), f"{path}: type differs")
        if isinstance(en, dict):
            self.assertEqual(set(en), set(zh), f"{path}: key sets differ")
            for k in en:
                self._assert_same_shape(en[k], zh[k], f"{path}.{k}")

    def test_key_sets_match_across_languages(self):
        # Recurse into nested tables (PANEL has tier/action/reason/de_lite/local_line/hub sub-dicts;
        # PERMANENT_SETUP has a nested `errors` dict) so a key added to one language but not the other
        # is caught at any depth.
        for name in ("NOTICE", "CHAT_DEFAULTS", "ACTIVATION", "PANEL", "GE_CHAT", "GE_CHROME",
                     "SHELL", "PERMANENT_SETUP", "MCP_TOOLS", "MCP_ERRORS"):
            self._assert_same_shape(getattr(en_US, name), getattr(zh_CN, name), name)

    def test_lookup_returns_the_right_language(self):
        self.assertEqual(i18n.notice("zh-CN")["close"], "关闭")
        self.assertEqual(i18n.notice("en-US")["close"], "Close")
        self.assertEqual(i18n.activation("zh-CN")["submit"], "激活")
        self.assertEqual(i18n.activation("en-US")["submit"], "Activate")
        # Permanent-setup form: a top-level string and a nested error slug resolve per language.
        self.assertEqual(i18n.permanent_setup("en-US")["activate"], "Activate")
        self.assertEqual(i18n.permanent_setup("zh-CN")["activate"], "激活")
        self.assertEqual(
            i18n.permanent_setup("en-US")["errors"]["secret_required"],
            "Activation key is required.",
        )
        self.assertEqual(
            i18n.permanent_setup("fr-FR")["errors"]["secret_required"],
            "Activation key is required.",
        )  # unknown locale falls back to en-US
        # MCP tool descriptions: a tool description and a nested param description resolve per language.
        self.assertEqual(
            i18n.mcp_tools("en-US")["activation_required"]["description"],
            "Activate this device before using Decision Engine tools.",
        )
        self.assertEqual(
            i18n.mcp_tools("zh-CN")["activation_required"]["description"],
            "在使用 Decision Engine 工具前，请先激活此设备。",
        )
        self.assertEqual(i18n.mcp_tools("zh-CN")["open_ge_popup"]["params"]["run_id"],
                         "visual_render 的 run_id。")
        self.assertEqual(i18n.mcp_tools("fr-FR")["open_ge_popup"]["params"]["run_id"],
                         "The visual_render run_id.")  # unknown locale → en-US
        # JSON-RPC -32001 prose: translated per language, with the protocol prefix kept verbatim.
        self.assertEqual(i18n.mcp_errors("en-US")["service_unavailable"],
                         "Decision Engine service unavailable — reconnect and retry")
        self.assertEqual(i18n.mcp_errors("zh-CN")["service_unavailable"],
                         "Decision Engine 服务不可用 — 请重新连接后重试")
        for locale in ("en-US", "zh-CN", "fr-FR"):
            self.assertTrue(
                i18n.mcp_errors(locale)["activation_required"].startswith("activation_required:"))

    def test_unknown_locale_falls_back_to_en(self):
        self.assertEqual(i18n.notice("fr-FR")["close"], "Close")
        self.assertEqual(i18n.chat_defaults("fr-FR")["timeout"], en_US.CHAT_DEFAULTS["timeout"])

    def test_chat_defaults_is_a_copy(self):
        d = i18n.chat_defaults("en-US")
        d["timeout"] = "MUTATED"
        self.assertNotEqual(i18n.chat_defaults("en-US")["timeout"], "MUTATED")


if __name__ == "__main__":
    unittest.main()
