"""Per-language UI string tables for the Python client.

ONE FILE PER LANGUAGE — English lives in `en_US.py`, Chinese in `zh_CN.py`, and the two are never
mixed in a single file. `client.i18n` imports both and assembles the locale→strings lookup; the
language DECISION itself is made only in `client.i18n.resolve_locale`. To add or edit a string,
edit the matching key in BOTH locale files (a table-parity test guards against drift).

Each locale module exposes these dicts with identical key sets:
    NOTICE          — GE terminal-notice pages (client/popup/notice.py)
    CHAT_DEFAULTS   — GE follow-up-chat runtime strings (client/popup/chat_backend.py)
    ACTIVATION      — first-use device-activation form (client/popup/launcher.py)
    PANEL           — Stopper audit-list panel (client/stopper/panel.py)
    GE_CHAT         — GE follow-up-chat page-side JS strings (launcher._GE_CHAT_JS)
    GE_CHROME       — GE header/region-chrome page-side JS strings (launcher._GE_CHROME_JS)
    SHELL           — native window-shell tooltips (window controls + tray)
    PERMANENT_SETUP — agent-launched permanent device-setup form (installer/permanent_setup.py)
    MCP_TOOLS       — MCP tool descriptions overlaid at tools/list assembly (installer/shim.py)
    MCP_ERRORS      — JSON-RPC -32001 error.message prose for the MCP shim (installer/shim.py)
"""
from client.locales import en_US, zh_CN

# locale tag → that language's three string tables. `client.i18n` reads THROUGH this map; keeping the
# assembly here (not in i18n.py) means i18n.py holds zero strings and each language stays in its own file.
TABLES = {
    "en-US": en_US,
    "zh-CN": zh_CN,
}

__all__ = ["TABLES", "en_US", "zh_CN"]
