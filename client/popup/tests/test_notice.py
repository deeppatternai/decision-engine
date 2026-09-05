from __future__ import annotations

import unittest

from client.popup.notice import render_ge_terminal_notice


class GeTerminalNoticeTestCase(unittest.TestCase):
    def test_zh_notice_renders_single_language_and_escapes_run_id(self):
        page = render_ge_terminal_notice("failed", 'aud_<bad>&"', locale="zh-CN")

        self.assertIn("图解生成失败", page)
        self.assertIn('<html lang="zh-CN"', page)
        # single-language render: the English copy is NOT stacked alongside the Chinese
        self.assertNotIn("Graphic generation failed", page)
        self.assertIn("aud_&lt;bad&gt;&amp;&quot;", page)
        self.assertNotIn('aud_<bad>&"', page)
        self.assertIn("window.pywebview.api.close()", page)

    def test_en_notice_renders_english_only(self):
        page = render_ge_terminal_notice("failed", "aud_1", locale="en-US")

        self.assertIn("Graphic generation failed", page)
        self.assertIn('<html lang="en-US"', page)
        self.assertNotIn("图解生成失败", page)

    def test_rejects_unknown_status_and_missing_run_id(self):
        with self.assertRaises(ValueError):
            render_ge_terminal_notice("unknown", "aud_1")
        with self.assertRaises(ValueError):
            render_ge_terminal_notice("failed", "")


if __name__ == "__main__":
    unittest.main()
