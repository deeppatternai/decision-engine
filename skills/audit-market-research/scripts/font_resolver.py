#!/usr/bin/env python3
"""Language-aware, machine-aware font resolution for the /audit-market-research DOCX report.

WHY THIS EXISTS. `assets/report_style.json` ships a house style whose CJK face is `LiSong Pro`
(and Latin `Georgia`). `LiSong Pro` was dropped from recent macOS and is absent on most Linux /
Windows machines, so a report rendered there names a font Word cannot find and the CJK text falls
back to a face with no Han glyphs → 方块 (tofu). The renderer used to hard-name the style font
regardless of what the machine actually has.

WHAT THIS DOES (Owner 2026-07-24). On the FIRST report, pick a font that is ACTUALLY INSTALLED on
this machine and matches the user's language, RECORD the choice in a small per-user cache, and reuse
the record on later reports as long as it stays valid. The house-style font is the top preference
for Chinese (so an Owner machine that HAS Georgia / a resident LiSong Pro keeps them); a missing one
falls through to the next installed candidate for the script.

Determinism note: the report's textual CONTENT and every non-font style setting stay identical across
machines; only the glyph FAMILY adapts to what the machine can render, so font metrics / wrapping /
pagination and the DOCX bytes may differ — which is the whole point of the fix.

Matching is EXACT (family name + a curated alias set per family, e.g. msyh→Microsoft YaHei), with a
narrow suffix relaxation for style/region-tagged filenames — NOT open substring, which would name a
Latin "Noto Serif" just because "Noto Serif CJK SC" is installed (audit 37901b81, 4/4 convergent).

Scope: Latin + the four CJK scripts (SC/TC/JA/KO). A non-CJK/non-Latin script (Arabic, Thai, …) gets
the Latin roster and relies on Word's own substitution — the report LABELS localize to en/zh only.

Pure stdlib, cross-platform, fail-safe: any probe / cache error degrades to the style default (never
worse than before, never an exception into the render).
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

# --- candidate rosters: (Word family name, [alias tokens]) ordered best → last-resort per script ---
# Alias tokens are NORMALIZED (lowercase, alnum-only) forms of the family's real filenames / fc-list
# names on the platforms where the file STEM differs from the family (Windows msyh.ttc → Microsoft
# YaHei; Linux NotoSansCJK-Regular.ttc → Noto Sans CJK *; wqy-microhei → WenQuanYi Micro Hei). The
# family name itself is always an implicit alias. Matching is exact-or-suffix (see `_is_installed`),
# so a bare/short token can't spoof a family.
_LATIN = [
    ("Georgia", []),
    ("Times New Roman", ["times", "timesnewromanpsmt"]),
    ("Cambria", ["cambria"]),
    ("Constantia", []),
    ("Palatino Linotype", ["pala"]),
    ("Palatino", []),
    ("DejaVu Serif", ["dejavuserif"]),
    ("Liberation Serif", ["liberationserif"]),
    ("Noto Serif", ["notoserif"]),
    ("Nimbus Roman", ["nimbusroman"]),
]
_CJK_BY_REGION = {
    "sc": [
        ("Songti SC", ["songti", "stsong"]), ("PingFang SC", ["pingfang"]), ("STSong", ["stsong"]),
        ("Heiti SC", ["heiti", "stheiti"]), ("STHeiti", ["stheiti"]),
        ("Microsoft YaHei", ["msyh"]), ("SimHei", ["simhei"]), ("SimSun", ["simsun"]),
        ("Noto Sans CJK SC", ["notosanscjksc", "notosanscjk"]),
        ("Noto Serif CJK SC", ["notoserifcjksc", "notoserifcjk"]),
        ("Source Han Sans SC", ["sourcehansanssc", "sourcehansans", "sourcehansanscn"]),
        ("Source Han Serif SC", ["sourcehanserifsc", "sourcehanserif", "sourcehanserifcn"]),
        ("WenQuanYi Micro Hei", ["wenquanyimicrohei", "wqymicrohei"]),
        ("WenQuanYi Zen Hei", ["wenquanyizenhei", "wqyzenhei"]),
    ],
    "tc": [
        ("PingFang TC", ["pingfang"]), ("Songti TC", ["songti"]), ("LiSong Pro", ["lisongpro", "lisong"]),
        ("BiauKai", ["biaukai", "kaiu"]), ("Heiti TC", ["heiti"]),
        ("Microsoft JhengHei", ["msjh"]), ("PMingLiU", ["pmingliu"]), ("MingLiU", ["mingliu"]),
        ("Noto Sans CJK TC", ["notosanscjktc", "notosanscjk"]),
        ("Noto Serif CJK TC", ["notoserifcjktc", "notoserifcjk"]),
        ("Source Han Sans TC", ["sourcehansanstc", "sourcehansans", "sourcehansanstw"]),
    ],
    "ja": [
        ("Hiragino Mincho ProN", ["hiraginominchopron", "hiraminpron", "hiraginomincho"]),
        ("Hiragino Sans", ["hiraginosans", "hiraginokakugothic", "hirakakupron"]),
        ("Yu Mincho", ["yumincho", "yumin"]), ("Yu Gothic", ["yugothic", "yugoth"]),
        ("MS Mincho", ["msmincho", "msmin"]), ("MS Gothic", ["msgothic", "msgoth"]),
        ("Noto Sans CJK JP", ["notosanscjkjp", "notosanscjk"]),
        ("Noto Serif CJK JP", ["notoserifcjkjp", "notoserifcjk"]),
        ("Source Han Sans", ["sourcehansansjp", "sourcehansans"]),
    ],
    "ko": [
        ("Apple SD Gothic Neo", ["applesdgothicneo", "applegothicneo"]), ("AppleGothic", ["applegothic"]),
        ("Malgun Gothic", ["malgungothic", "malgun"]), ("Batang", ["batang", "batangche"]),
        ("Noto Sans CJK KR", ["notosanscjkkr", "notosanscjk"]),
        ("Noto Serif CJK KR", ["notoserifcjkkr", "notoserifcjk"]),
        ("Source Han Sans K", ["sourcehansanskr", "sourcehansans"]),
    ],
}

# Style / weight words that may legitimately trail a family name in a FILE STEM.
# Regional faces depend on exact family tokens or explicit curated aliases; a region
# suffix is never relaxed generically.
_STYLE_SUFFIXES = frozenset({
    "regular", "normal", "book", "roman", "text",
    "bold", "italic", "oblique", "light", "medium", "semibold", "demibold", "demi",
    "black", "heavy", "thin", "extralight", "ultralight", "extrabold", "ultrabold",
})

_FONT_EXTS = {".ttf", ".ttc", ".otf", ".otc", ".ttf.gz"}
_FC_LIST_TIMEOUT_S = 4.0

# macOS ships many CJK faces (incl. PingFang / LiSong Pro) as ON-DEMAND downloadable assets under
# these paths — fc-list reports them, but they are NOT resident and may be purged or absent on the
# machine that renders and locally opens the DOCX, so naming one can render 方块 (tofu). We treat
# ONLY resident fonts as installed for local reliability. A font the user
# installs into a normal font dir is resident and still counts. NOTE: this makes "keep a present
# LiSong Pro" effectively unreachable on stock recent macOS (LiSong Pro lives only here), so the
# expected behaviour there is fall-through to a resident face — see resolve_fonts' docstring.
# The markers are Apple's current AssetsV2 / MobileAsset layout; a future rename would need updating.
_ON_DEMAND_MARKERS = ("/assetsv2/", "/assets/com_apple", "/mobileasset")


def _norm(s: str) -> str:
    """A family/filename reduced to a comparable token: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _cjk_region_for_lang(lang: str) -> str:
    """Map a BCP-47 tag to the CJK region whose faces to PREFER. Traditional-Chinese, Japanese and
    Korean each get their own ordering; everything else (incl. bare 'zh' / Simplified) → 'sc'."""
    t = (lang or "").strip().lower().replace("_", "-")
    if t.startswith("ja"):
        return "ja"
    if t.startswith("ko"):
        return "ko"
    if t.startswith("zh"):
        if "hant" in t or any(t.endswith(f"-{r}") or f"-{r}-" in t for r in ("tw", "hk", "mo")):
            return "tc"
        return "sc"
    return "sc"


def _font_dirs() -> list[Path]:
    sysname = platform.system()
    home = Path.home()
    if sysname == "Darwin":
        cands = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                 "/Library/Fonts", home / "Library/Fonts"]
    elif sysname == "Windows":
        cands = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "Microsoft/Windows/Fonts")
    else:  # Linux / *nix
        xdg = os.environ.get("XDG_DATA_HOME")
        cands = ["/usr/share/fonts", "/usr/local/share/fonts",
                 home / ".fonts", home / ".local/share/fonts"]
        if xdg:
            cands.append(Path(xdg) / "fonts")
    return [Path(c) for c in cands]


def _is_on_demand(path: str) -> bool:
    p = path.lower()
    return any(m in p for m in _ON_DEMAND_MARKERS)


def _tokens_from_fc_list() -> set[str]:
    """Best-effort family names from fontconfig's `fc-list` (accurate where present — Linux always,
    macOS via Homebrew). Queries the FILE path too so on-demand downloadable assets (macOS AssetsV2)
    are excluded — only resident faces count. Missing binary / timeout / any error → empty set (the
    dir scan still runs)."""
    if platform.system() == "Windows":
        return set()
    try:
        out = subprocess.run(["fc-list", "--format=%{file}::%{family}\n"],
                             capture_output=True, text=True, timeout=_FC_LIST_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    toks: set[str] = set()
    for line in out.stdout.splitlines():
        path, _, families = line.partition("::")
        if not families or _is_on_demand(path):
            continue                        # on-demand asset → not reliably present, skip
        for name in families.split(","):    # fc-list emits localized aliases comma-separated
            tok = _norm(name)
            if tok:
                toks.add(tok)
    return toks


def _tokens_from_dirs() -> set[str]:
    """Normalized filename stems of every font file in the platform font directories. A coarse but
    dependency-free signal that works when fc-list is absent (typical Windows / vanilla macOS). The
    on-demand asset dirs are NOT in `_font_dirs()`, so they are excluded here by construction."""
    toks: set[str] = set()
    for d in _font_dirs():
        try:
            if not d.is_dir():
                continue
            for root, _dirs, files in os.walk(d, onerror=lambda _error: None):
                for name in files:
                    f = Path(root) / name
                    lower_name = name.lower()
                    if (any(lower_name.endswith(ext) for ext in _FONT_EXTS)
                            and not _is_on_demand(str(f))):
                        stem = name[:-7] if lower_name.endswith(".ttf.gz") else f.stem
                        tok = _norm(stem)
                        if tok:
                            toks.add(tok)
        except OSError:
            continue        # unreadable dir → skip; never fail the whole probe
    return toks


_INSTALLED_CACHE: Optional[frozenset[str]] = None
_CACHE_WRITE_LOCK = threading.Lock()


def installed_font_tokens(refresh: bool = False) -> frozenset[str]:
    """The set of normalized font tokens present on this machine (fc-list ∪ directory scan). Probed
    once per process (the OS font set does not change mid-run) unless `refresh`."""
    global _INSTALLED_CACHE
    if _INSTALLED_CACHE is None or refresh:
        _INSTALLED_CACHE = frozenset(_tokens_from_fc_list() | _tokens_from_dirs())
    return _INSTALLED_CACHE


def _token_matches(tok: str, installed: Iterable[str]) -> bool:
    """One candidate token vs the installed set: exact, or one side is the other plus a known
    style/weight suffix (directory-scan file stems carry ``-Regular`` etc.). Regional collection
    filenames are supported only through curated aliases on each candidate. A four-character
    floor on the shorter side blocks spurious short-token hits."""
    if len(tok) < 4:
        return False
    for it in installed:
        if it == tok:
            return True
        if len(it) > len(tok) and it.startswith(tok) and it[len(tok):] in _STYLE_SUFFIXES:
            return True
        if len(tok) > len(it) >= 4 and tok.startswith(it) and tok[len(it):] in _STYLE_SUFFIXES:
            return True
    return False


def _is_installed(family: str, aliases: Iterable[str], installed: Iterable[str]) -> bool:
    inst = list(installed)
    for tok in (_norm(family), *(_norm(a) for a in aliases)):
        if _token_matches(tok, inst):
            return True
    return False


def _pick(candidates: list[tuple], installed: Iterable[str]) -> tuple[Optional[str], bool]:
    """First installed candidate → (family, True); none → (None, False). Each candidate is
    (family, [aliases])."""
    inst = list(installed)
    for family, aliases in candidates:
        if _is_installed(family, aliases, inst):
            return family, True
    return None, False


def _default_cache_path() -> Path:
    """A writable per-user cache file. Platform cache dir → fallback to the user's home → the choice
    is best-effort anyway (a failed write just means we re-probe next time)."""
    sysname = platform.system()
    if sysname == "Darwin":
        base = Path.home() / "Library/Caches"
    elif sysname == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "audit-market-research" / "fonts.json"


def _cache_key(lang: str, style: dict) -> str:
    """Keyed by CJK region + the style defaults, so editing report_style.json's fonts invalidates a
    stale pick (the default is a preference, so its identity is part of the answer)."""
    return f"{_cjk_region_for_lang(lang)}|{style.get('body_latin', '')}|{style.get('body_cjk', '')}"


def _read_cache(path: Optional[Path]) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(path: Optional[Path], data: dict) -> None:
    if not path:
        return
    tmp: Optional[Path] = None
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(data, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=p.parent,
            prefix=p.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(rendered)
            tmp = Path(handle.name)
        os.replace(tmp, p)
    except OSError:
        pass        # a read-only cache dir just means we re-probe next run — never fatal
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _still_installed(family: str, installed: Iterable[str]) -> bool:
    """Re-validate a recorded pick: the concrete chosen family must still be present (self-heals a
    later uninstall / OS upgrade instead of stranding the user on a now-absent font)."""
    aliases: Iterable[str] = ()
    for candidate, candidate_aliases in [
        *_LATIN,
        *(entry for roster in _CJK_BY_REGION.values() for entry in roster),
    ]:
        if candidate == family:
            aliases = candidate_aliases
            break
    return _is_installed(family, aliases, installed)


def resolve_fonts(lang: str, style: dict, *, cache_path: Optional[Path] = ...,
                  installed: Optional[Iterable[str]] = None, refresh: bool = False) -> dict:
    """Return {'body_latin', 'body_cjk', 'from_cache'} — machine-available faces for `lang`.

    House style: the style default CJK face is the TOP candidate for the CHINESE regions (sc/tc), so
    a machine that HAS it keeps it; for JA/KO it is only a LAST resort (a Chinese default must not
    beat a real Japanese/Korean face). A missing default falls through to the first installed
    candidate; if NOTHING matches, the style default is returned unchanged (we can't do better, and
    never crash) — and that unresolved fallback is NOT recorded, so installing a font later
    self-heals on the next report. A recorded pick is reused only while it is STILL installed;
    otherwise it is re-resolved.

    `cache_path` ELLIPSIS (default) → the per-user cache file; `None` → no caching (always probe);
    an explicit Path → use that file (tests point it at a tmp file). `installed` overrides the OS
    probe (tests inject a token set)."""
    style = style or {}
    def_latin = str(style.get("body_latin") or "Georgia")
    def_cjk = str(style.get("body_cjk") or "PingFang SC")

    path = _default_cache_path() if cache_path is ... else cache_path
    key = _cache_key(lang, {"body_latin": def_latin, "body_cjk": def_cjk})
    inst = installed if installed is not None else installed_font_tokens(refresh=refresh)

    if not refresh:
        cached = _read_cache(path).get(key)
        if (isinstance(cached, dict) and cached.get("body_latin") and cached.get("body_cjk")
                and _still_installed(cached["body_latin"], inst)
                and _still_installed(cached["body_cjk"], inst)):
            # The recorded pick, re-validated as still present — reuse it (the "记录下来" contract).
            return {"body_latin": cached["body_latin"], "body_cjk": cached["body_cjk"],
                    "from_cache": True}

    region = _cjk_region_for_lang(lang)
    roster = _CJK_BY_REGION.get(region, _CJK_BY_REGION["sc"])
    default_cand = (def_cjk, [])
    # Chinese: house default first (house-style-wins). JA/KO: house default LAST (a Chinese face
    # must not preempt a real Japanese/Korean one — audit 37901b81 codex f2).
    cjk_candidates = [default_cand, *roster] if region in ("sc", "tc") else [*roster, default_cand]

    latin, latin_hit = _pick([(def_latin, []), *_LATIN], inst)
    cjk, cjk_hit = _pick(cjk_candidates, inst)
    latin = latin or def_latin
    cjk = cjk or def_cjk

    # Persist ONLY a fully-resolved pick. A fallback to an unverified default is NOT recorded, so a
    # first-probe miss (or a machine that later installs a font) re-resolves instead of locking in
    # tofu (audit 37901b81, 4/4 convergent).
    if latin_hit and cjk_hit:
        # Keep the in-process read-modify-write atomic. The file write itself is atomic
        # across processes; a cross-process lost cache entry is harmless and self-heals.
        with _CACHE_WRITE_LOCK:
            store = _read_cache(path)
            store[key] = {"body_latin": latin, "body_cjk": cjk}
            _write_cache(path, store)
    return {"body_latin": latin, "body_cjk": cjk, "from_cache": False}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Resolve + record on-machine report fonts for a language.")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-probe")
    args = ap.parse_args()
    style_path = Path(__file__).resolve().parent.parent / "assets" / "report_style.json"
    style = json.loads(style_path.read_text(encoding="utf-8")).get("font", {})
    r = resolve_fonts(args.lang, style, refresh=args.refresh)
    src = "cache" if r["from_cache"] else "probe"
    print(f"lang={args.lang} · latin={r['body_latin']} · cjk={r['body_cjk']} ({src})")
