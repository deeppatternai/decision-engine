"""Native window-shell localization: control tooltips, tray tooltip, chat error fallback.

The popup HTML that launcher renders already carries the resolver's chosen language as
``<html lang>``. native_shell reads that back (the resolver's OUTPUT, not a re-sniff) so its
frameless-header controls, the macOS tray tooltip, and the chat error fallback all match the
language the page already shows. These tests lock that read-back + the script-safe JS injection.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from client import i18n
from client.popup import launcher
from client.popup import native_shell as ns


def _render(tag):
    spec = launcher.PopupSpec(kind="ge", title="t", payload={"ui_locale": tag},
                              artifact={"kind": "svg", "data": "<svg/>"})
    p = Path(tempfile.mktemp(suffix=".html"))
    p.write_text(launcher.render_artifact_html(spec), encoding="utf-8")
    return str(p)


class ReadHtmlLangTests(unittest.TestCase):
    def test_reads_resolver_output_from_rendered_popup(self):
        self.assertEqual(ns._read_html_lang(_render("zh-CN")), "zh-CN")
        self.assertEqual(ns._read_html_lang(_render("en-US")), "en-US")

    def test_missing_lang_falls_back_to_resolver(self):
        p = Path(tempfile.mktemp(suffix=".html"))
        p.write_text("<!doctype html><html><body>no lang</body></html>", encoding="utf-8")
        # Falls back to resolve_locale(None) — a valid locale, never a crash.
        self.assertIn(ns._read_html_lang(str(p)), ("en-US", "zh-CN"))

    def test_unreadable_path_falls_back(self):
        self.assertIn(ns._read_html_lang("does-not-exist.html"), ("en-US", "zh-CN"))


class ChromeInjectionTests(unittest.TestCase):
    def test_sentinel_replaced_and_roundtrips_both_locales(self):
        for loc in ("zh-CN", "en-US"):
            sh = i18n.shell(loc)
            js = launcher._inject_js_strings(ns._WINDOW_CHROME_JS, "__SHELL_STRINGS__", sh)
            self.assertNotIn("__SHELL_STRINGS__", js)
            m = re.search(r"var _CHROME_STRINGS = (\{.*?\});", js)
            self.assertIsNotNone(m)
            self.assertEqual(json.loads(m.group(1)), sh)

    def test_injected_literal_is_script_safe(self):
        # ensure_ascii + <>& escaping means no raw angle bracket can terminate the <script>.
        js = launcher._inject_js_strings(ns._WINDOW_CHROME_JS, "__SHELL_STRINGS__",
                                         i18n.shell("zh-CN"))
        literal = re.search(r"var _CHROME_STRINGS = (\{.*?\});", js).group(1)
        self.assertNotIn("<", literal)
        self.assertNotIn(">", literal)


class ShellCatalogTests(unittest.TestCase):
    def test_tray_toggle_has_title_slot(self):
        for loc in ("zh-CN", "en-US"):
            self.assertIn("%s", i18n.shell(loc)["tray_toggle"])

    def test_error_fallback_present_per_locale(self):
        self.assertTrue(i18n.chat_defaults("zh-CN")["error"])
        self.assertTrue(i18n.chat_defaults("en-US")["error"])

    def test_shell_keys_match_across_locales(self):
        self.assertEqual(set(i18n.shell("en-US")), set(i18n.shell("zh-CN")))


if __name__ == "__main__":
    unittest.main()
