"""Single language-resolution point + central UI string catalog for the Python client.

WHY THIS EXISTS. The client used to decide "Chinese vs English" four different ways in four
places: the Stopper panel keyed off a locale tag (`normalize_ui_locale`), the graphic-explanation
popups sniffed the invocation title for Han characters (`_is_zh`), embedded JS re-read
`document.documentElement.lang`, and the activation / notice pages just rendered both languages
side by side. This module collapses all of that into ONE decision (`resolve_locale`). The strings
themselves live one-language-per-file under `client/locales/` (`en_US.py`, `zh_CN.py`); this module
only RESOLVES the locale and LOOKS UP the chosen language's table. Every surface resolves the locale
here and reads its text through the helpers here; no surface re-implements the choice.

Scope: the Python client only. The macOS Swift Stopper (`desktop/macos/DecisionEngineStopper.swift`)
is a separate process that cannot import this module and keeps its own inline pairs — by design.

Resolution order (first supported wins), for a caller that passes no explicit value:
    explicit arg  →  $DE_UI_LOCALE  →  system UI language  →  "en-US"
where the "system UI language" step itself is:  $LC_ALL  →  [macOS: AppleLanguages/AppleLocale]
→  [Windows: OS UI language]  →  [Linux/WSL: LC_CTYPE / LANG / LANGUAGE]  →  stdlib locale.
C / POSIX / C.UTF-8 are treated as "no language signal" and are skipped. On Windows the OS UI
language deliberately outranks $LC_CTYPE/$LANG/$LANGUAGE (but NOT the explicit $LC_ALL or
$DE_UI_LOCALE overrides), because Git Bash / MSYS inject `LANG=en_US.UTF-8` on a zh-CN machine;
an env-first order let that injected tag override the real Chinese UI. On macOS the real UI
language comes from `defaults read -g AppleLanguages` (first item) or `AppleLocale`, so a
Codex-launched C.UTF-8 process locale does not force an English fallback. On Linux/WSL the env
vars stay the main signal; if they are all C-like, detection falls through unless some other
non-env source can answer. Only two locales are supported; every `zh-*` (incl. Traditional)
normalizes to "zh-CN" and every `en-*` to "en-US". The system-language step is what makes an
un-tagged run follow the OS instead of hard-defaulting to English. An operator who must force a
locale against the OS UI sets $LC_ALL or $DE_UI_LOCALE.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any, Optional

SUPPORTED_LOCALES = ("en-US", "zh-CN")
DEFAULT_LOCALE = "en-US"
_C_LOCALE_TAGS = {"c", "posix", "c.utf-8", "c.utf8"}


def _normalize_tag(value: Any) -> Optional[str]:
    """Map one candidate to a supported locale, or None if it is not a recognized en/zh tag."""
    if not isinstance(value, str):
        return None
    tag = value.strip().lower().replace("_", "-")
    if tag == "en" or tag.startswith("en-"):
        return "en-US"
    if tag == "zh" or tag.startswith("zh-"):
        return "zh-CN"
    return None


def _normalize_env_tag(value: Any, *, colon_list: bool = False) -> tuple[Optional[str], bool]:
    """Normalize one env locale candidate, returning (supported_tag, saw_c_like_signal)."""
    if not isinstance(value, str):
        return None, False
    raw = value.strip()
    if not raw:
        return None, False
    candidates = raw.split(":") if colon_list else [raw.split(":", 1)[0]]
    saw_c_like = False
    for candidate in candidates:
        tag = candidate.strip()
        if not tag:
            continue
        normalized = tag.lower().replace("_", "-")
        if normalized in _C_LOCALE_TAGS:
            saw_c_like = True
            continue
        resolved = _normalize_tag(tag)
        if resolved:
            return resolved, saw_c_like
    return None, saw_c_like


def detect_system_locale() -> Optional[str]:
    """Best-effort OS UI language as a supported locale, or None when it can't be read.

    Never raises and never blocks: every backend is wrapped, and anything unrecognized returns
    None so the caller falls through to the next candidate. Split out as its own function so tests
    can monkeypatch it to pin the default path (otherwise the result would follow the CI machine's
    locale and assertions on the en-US default would be flaky).
    """
    # Resolution walks platform-specific groups so an injected tag can't mask the real UI language:
    #   1. LC_ALL — the POSIX "override everything" hammer. It is always DELIBERATE (Git Bash / MSYS
    #      never inject it), so it stays above the OS probe: an operator who sets it means it.
    #   2. macOS / Windows OS UI language — the operator's real preference on those platforms. It
    #      MUST outrank the remaining env vars because Git Bash / MSYS inject LANG=en_US.UTF-8 (and
    #      can set LC_CTYPE) on a zh-CN machine; an env-first order let that injected tag mask the
    #      true Chinese UI, so the graphic-explanation popups rendered English on a Chinese Windows.
    #      On macOS, AppleLanguages/AppleLocale must also outrank LC_CTYPE/LANG/LANGUAGE because a
    #      Codex-launched C.UTF-8 process locale is not user intent.
    #   3. LC_CTYPE / LANG / LANGUAGE — the injectable vars; honored after the platform UI probe.
    # DE_UI_LOCALE (checked earlier in resolve_locale) is the highest-priority explicit override.
    def _from_env(var: str, *, colon_list: bool = False) -> tuple[Optional[str], bool]:
        # LANGUAGE may be a colon list ("zh_CN:en_US"); keep walking until a supported value
        # appears, but remember whether the chain only contained C-like signals.
        return _normalize_env_tag(os.environ.get(var), colon_list=colon_list)

    c_like_seen = False

    lc_all, saw_c_like = _from_env("LC_ALL")
    c_like_seen = c_like_seen or saw_c_like
    if lc_all:
        return lc_all

    if sys.platform == "darwin":
        found = _detect_macos_ui_language()
        if found:
            return found

    if os.name == "nt":
        found = _detect_windows_ui_language()
        if found:
            return found

    for var in ("LC_CTYPE", "LANG"):
        normalized, saw_c_like = _from_env(var)
        c_like_seen = c_like_seen or saw_c_like
        if normalized:
            return normalized

    normalized, saw_c_like = _from_env("LANGUAGE", colon_list=True)
    c_like_seen = c_like_seen or saw_c_like
    if normalized:
        return normalized

    if c_like_seen:
        return None

    # Standard-library fallback (POSIX default catalogs); getdefaultlocale is deprecated, so use
    # getlocale, which reads the process locale set from the environment.
    try:
        import locale

        code = locale.getlocale(locale.LC_CTYPE)[0]
    except Exception:
        code = None
    return _normalize_tag(code) if code else None


def _read_macos_defaults(key: str) -> Optional[str]:
    """Read one macOS global defaults key and return the first usable string value."""
    try:
        completed = subprocess.run(
            ["defaults", "read", "-g", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "").strip()
    if not output:
        return None
    quoted = re.findall(r'"([^"]+)"', output)
    if quoted:
        return quoted[0]
    for line in output.splitlines():
        value = line.strip().strip(",")
        if value and value not in {"(", ")"}:
            return value
    return None


def _detect_macos_ui_language() -> Optional[str]:
    """macOS user UI language via defaults, normalized. None on any failure."""
    for key in ("AppleLanguages", "AppleLocale"):
        found = _read_macos_defaults(key)
        normalized = _normalize_tag(found)
        if normalized:
            return normalized
    return None


def _detect_windows_ui_language() -> Optional[str]:
    """Windows user UI language via kernel32, normalized. None on any failure (non-Windows-safe)."""
    try:
        import ctypes

        # GetUserDefaultUILanguage returns an LCID; the low 10 bits are the primary language id.
        # 0x04 = Chinese, 0x09 = English — enough to pick between our two supported locales.
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
        primary = lcid & 0x3FF
        if primary == 0x04:
            return "zh-CN"
        if primary == 0x09:
            return "en-US"
    except Exception:
        return None
    return None


def resolve_locale(explicit: Any = None) -> str:
    """Return one supported locale. Order: explicit → $DE_UI_LOCALE → system language → en-US.

    A candidate that is not a recognized en/zh tag is SKIPPED (it must not swallow the next
    candidate). This is the single language-decision point for the whole Python client.
    """
    explicit_tag = _normalize_tag(explicit)
    if explicit_tag:
        return explicit_tag
    env_tag = _normalize_tag(os.getenv("DE_UI_LOCALE"))
    if env_tag:
        return env_tag
    system_tag = detect_system_locale()
    if system_tag:
        return system_tag
    return DEFAULT_LOCALE


def is_zh(locale: str) -> bool:
    """True when the resolved locale is Chinese. Convenience for the many two-way branches."""
    return locale == "zh-CN"


def accept_language(explicit: Any = None) -> str:
    """The resolved UI locale as a standard HTTP ``Accept-Language`` header value.

    One decision point for the whole client's outbound requests: every authenticated hub
    fetch tags itself with the shell's ambient UI locale (``resolve_locale`` order: explicit →
    $DE_UI_LOCALE → system language → en-US) so the backend sees the operator's language
    preference. This is the user-agent's preference — orthogonal to any per-run ``ui_locale``
    in a request BODY, which states what language THAT audit's content should render in.
    Only supported tags ship (``en-US`` / ``zh-CN``), so a single BCP-47 tag is emitted with
    no q-weights; the header format lives here alone so it never drifts across call sites.
    """
    return resolve_locale(explicit)


# ── string lookup (strings themselves live one-language-per-file in client/locales) ─────────────
# These helpers resolve a *language table* — they do NOT decide the language (resolve_locale does).
# An unknown locale falls back to the en-US table so a surface can never render nothing.

def _table(locale: str):
    """The locale's string module (en_US / zh_CN), falling back to en-US for an unknown locale."""
    from client.locales import TABLES

    return TABLES.get(locale) or TABLES[DEFAULT_LOCALE]


def notice(locale: str) -> dict:
    """Notice-page strings for a resolved locale (client/popup/notice.py)."""
    return _table(locale).NOTICE


def chat_defaults(locale: str) -> dict:
    """GE chat default runtime strings for a resolved locale (copy, safe for caller overlay)."""
    return dict(_table(locale).CHAT_DEFAULTS)


def activation(locale: str) -> dict:
    """Activation-form strings for a resolved locale (client/popup/launcher.py)."""
    return _table(locale).ACTIVATION


def panel(locale: str) -> dict:
    """Stopper audit-list panel strings/templates for a resolved locale (client/stopper/panel.py)."""
    return _table(locale).PANEL


def ge_chat(locale: str) -> dict:
    """GE follow-up-chat page-side JS strings for a resolved locale (launcher._GE_CHAT_JS)."""
    return _table(locale).GE_CHAT


def ge_chrome(locale: str) -> dict:
    """GE header/region-chrome page-side JS strings for a resolved locale (launcher._GE_CHROME_JS)."""
    return _table(locale).GE_CHROME


def shell(locale: str) -> dict:
    """Native window-shell strings (window-control tooltips + tray tooltip) for a resolved locale."""
    return _table(locale).SHELL


def permanent_setup(locale: str) -> dict:
    """Permanent device-setup form strings for a resolved locale (installer/permanent_setup.py).

    Covers both the pywebview HTML and the tkinter fallback from ONE table, including the nested
    ``errors`` dict whose stable slugs the form backends and the shared credential validators look
    up so the same wording renders no matter which layer raises the failure.
    """
    return _table(locale).PERMANENT_SETUP


def mcp_tools(locale: str) -> dict:
    """MCP tool descriptions for a resolved locale (installer/shim.py).

    Keyed by wire tool name; each entry is ``{"description": str, "params": {param_name: str}}``.
    The shim owns each tool's STRUCTURE (name / inputSchema types / enum / required) and overlays
    only these human-readable strings at tools/list assembly, so the returned table is read-only —
    callers copy the schema they mutate, never this dict.
    """
    return _table(locale).MCP_TOOLS


def mcp_errors(locale: str) -> dict:
    """JSON-RPC ``-32001`` error.message prose for a resolved locale (installer/shim.py).

    Keyed by internal slug (never a wire value); each value is one flat message string. Error CODES
    and the structured ``data`` payload stay in the shim — only the human-readable sentence is
    localized. ``activation_required`` keeps its ``activation_required:`` prefix verbatim in every
    language because callers may match on that token.
    """
    return _table(locale).MCP_ERRORS
