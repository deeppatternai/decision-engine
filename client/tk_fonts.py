"""Resolve usable Tk font families without depending on desktop font fallback."""

from __future__ import annotations

from typing import Any, Iterable, Tuple


_LINUX_UI_CANDIDATES = (
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans CN",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "Droid Sans Fallback",
    "DejaVu Sans",
)

_LINUX_MONO_CANDIDATES = (
    "Noto Sans Mono CJK SC",
    "Source Han Mono SC",
    "Sarasa Mono SC",
    "WenQuanYi Zen Hei Mono",
    "DejaVu Sans Mono",
)


def _first_available(available: dict[str, str], candidates: Iterable[str]) -> str:
    for candidate in candidates:
        match = available.get(candidate.casefold())
        if match:
            return match
    return ""


def _first_matching(available: dict[str, str], tokens: Iterable[str]) -> str:
    for token in tokens:
        token = token.casefold()
        for normalized, family in available.items():
            if token in normalized:
                return family
    return ""


def _named_family(tkfont: Any, name: str, fallback: str) -> str:
    try:
        family = str(tkfont.nametofont(name).actual("family") or "").strip()
    except Exception:
        family = ""
    return family or fallback


def resolve_linux_tk_fonts(root: Any, tkfont_module: Any = None) -> Tuple[str, str]:
    """Return installed UI/mono families, preferring fonts with Simplified Chinese glyphs.

    Tk on Linux does not consistently follow the desktop's Fontconfig fallback chain. Selecting
    a family Tk actually enumerates prevents Chinese labels from becoming blank on Fedora while
    keeping a deterministic DejaVu/default fallback for minimal installations.
    """
    if tkfont_module is None:
        from tkinter import font as tkfont_module

    try:
        families = tkfont_module.families(root)
    except Exception:
        families = ()
    available = {
        str(family).strip().casefold(): str(family).strip()
        for family in families
        if str(family).strip()
    }

    default_ui = _named_family(tkfont_module, "TkDefaultFont", "DejaVu Sans")
    default_mono = _named_family(tkfont_module, "TkFixedFont", "DejaVu Sans Mono")
    ui = (
        _first_available(available, _LINUX_UI_CANDIDATES)
        or _first_matching(available, ("noto sans cjk", "source han sans", "wenquanyi"))
        or default_ui
    )
    mono = (
        _first_available(available, _LINUX_MONO_CANDIDATES)
        or _first_matching(available, ("noto sans mono cjk", "source han mono", "sarasa mono"))
        or default_mono
    )
    return ui, mono
