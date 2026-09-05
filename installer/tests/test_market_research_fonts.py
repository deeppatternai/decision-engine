"""Regression tests for the client-side market-research font resolver."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


_ROOT = Path(__file__).resolve().parents[2]
_RESOLVER_PATH = (
    _ROOT / "skills" / "audit-market-research" / "scripts" / "font_resolver.py"
)
_SPEC = importlib.util.spec_from_file_location("audit_market_research_font_resolver", _RESOLVER_PATH)
font_resolver = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(font_resolver)


class FontResolverTests(unittest.TestCase):
    STYLE = {"body_latin": "Georgia", "body_cjk": "LiSong Pro"}

    def test_sc_does_not_accept_a_japanese_region_suffix(self):
        picked = font_resolver.resolve_fonts(
            "zh-CN",
            self.STYLE,
            cache_path=None,
            installed={"georgia", "pingfangjp", "songtijp", "msyh"},
        )
        self.assertEqual(picked["body_cjk"], "Microsoft YaHei")

    def test_bcp47_cjk_region_mapping(self):
        cases = {
            "zh": "sc",
            "zh-Hans-CN": "sc",
            "zh-Hant": "tc",
            "zh-TW": "tc",
            "zh-HK-x-private": "tc",
            "ja-JP": "ja",
            "ko-KR": "ko",
        }
        for lang, expected in cases.items():
            with self.subTest(lang=lang):
                self.assertEqual(font_resolver._cjk_region_for_lang(lang), expected)

    def test_generic_collection_token_can_still_select_regional_family(self):
        picked = font_resolver.resolve_fonts(
            "zh-CN",
            self.STYLE,
            cache_path=None,
            installed={"georgia", "songti"},
        )
        self.assertEqual(picked["body_cjk"], "Songti SC")

    def test_cached_family_revalidates_through_its_filename_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fonts.json"
            first = font_resolver.resolve_fonts(
                "zh-CN",
                self.STYLE,
                cache_path=cache,
                installed={"georgia", "msyh"},
            )
            second = font_resolver.resolve_fonts(
                "zh-CN",
                self.STYLE,
                cache_path=cache,
                installed={"georgia", "msyh"},
            )
        self.assertEqual(first["body_cjk"], "Microsoft YaHei")
        self.assertTrue(second["from_cache"])

    def test_cache_invalidates_when_the_recorded_font_is_uninstalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fonts.json"
            font_resolver.resolve_fonts(
                "zh-CN", self.STYLE, cache_path=cache, installed={"georgia", "msyh"}
            )
            picked = font_resolver.resolve_fonts(
                "zh-CN", self.STYLE, cache_path=cache, installed={"georgia", "songti"}
            )
        self.assertFalse(picked["from_cache"])
        self.assertEqual(picked["body_cjk"], "Songti SC")

    def test_unresolved_fallback_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fonts.json"
            picked = font_resolver.resolve_fonts(
                "zh-CN", self.STYLE, cache_path=cache, installed=set()
            )
            self.assertFalse(cache.exists())
        self.assertEqual(picked["body_cjk"], "LiSong Pro")

    def test_directory_scan_recognizes_compound_ttf_gz_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "NotoSansCJK-Regular.ttf.gz").write_bytes(b"not-a-real-font")
            with mock.patch.object(font_resolver, "_font_dirs", return_value=[root]):
                tokens = font_resolver._tokens_from_dirs()
        self.assertIn("notosanscjkregular", tokens)

    def test_concurrent_cache_writes_always_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fonts.json"
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(
                    lambda i: font_resolver._write_cache(cache, {"entry": i}),
                    range(32),
                ))
            loaded = json.loads(cache.read_text(encoding="utf-8"))
        self.assertIn("entry", loaded)
        self.assertIsInstance(loaded["entry"], int)

    def test_concurrent_resolves_preserve_each_language_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "fonts.json"
            jobs = [
                ("zh-CN", {"georgia", "songti"}),
                ("ja-JP", {"georgia", "yumincho"}),
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(
                    lambda item: font_resolver.resolve_fonts(
                        item[0], self.STYLE, cache_path=cache, installed=item[1]
                    ),
                    jobs,
                ))
            loaded = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded), 2)

    def test_windows_never_executes_an_unqualified_fc_list(self):
        with mock.patch.object(font_resolver.platform, "system", return_value="Windows"), \
             mock.patch.object(font_resolver.subprocess, "run") as run:
            self.assertEqual(font_resolver._tokens_from_fc_list(), set())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
