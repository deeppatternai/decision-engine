"""Small native-popup notices for detached Decision Engine workflows.

These pages report a terminal client-side delivery outcome only. They never contain an
artifact, generation instructions, server error detail, or a browser fallback.

Language follows the single resolver in `client.i18n` (system UI language when the host set no
`DE_UI_LOCALE`); the page renders one language, not a bilingual stack.
"""
from __future__ import annotations

import html

from client import i18n


def render_ge_terminal_notice(status: str, run_id: str, locale: str | None = None) -> str:
    """Render a self-contained, non-artifact notice for a delayed GE outcome."""
    resolved = i18n.resolve_locale(locale)
    copy = i18n.notice(resolved)
    if status not in copy or status == "close":
        raise ValueError("unsupported GE notice status")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    title, message = copy[status]
    safe_run_id = html.escape(run_id.strip(), quote=True)
    return """<!doctype html>
<html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision Engine</title>
<style>
*{box-sizing:border-box}html,body{height:100%%;margin:0}body{font-family:system-ui,-apple-system,
Segoe UI,sans-serif;background:#f7f8fb;color:#172033;display:flex;flex-direction:column}
header{height:48px;padding:0 12px 0 18px;background:#fff;border-bottom:1px solid #e5e8ef;
display:flex;align-items:center;justify-content:space-between;-webkit-app-region:drag}
header strong{font-size:14px}.controls{display:flex;gap:6px;-webkit-app-region:no-drag}
button{border:0;border-radius:8px;background:#eef1f6;color:#263247;padding:7px 13px;cursor:pointer}
main{flex:1;display:grid;place-items:center;padding:32px}.card{width:min(620px,100%%);background:#fff;
border:1px solid #e1e5ed;border-radius:18px;padding:34px;box-shadow:0 12px 38px #1d2a4420}
.mark{font-size:38px;color:#d97706}.title{font-size:24px;font-weight:700;margin:16px 0 12px}
.message{font-size:16px;line-height:1.7;margin-bottom:22px}.run{font:13px ui-monospace,SFMono-Regular,
Consolas,monospace;background:#f3f5f8;border-radius:9px;padding:11px 13px;overflow-wrap:anywhere;
color:#4b5870}
</style></head><body>
<header><strong>Decision Engine</strong><div class="controls">
<button type="button" onclick="window.pywebview.api.close()">%(close)s</button></div></header>
<main><section class="card"><div class="mark">!</div>
<div class="title">%(title)s</div>
<div class="message">%(message)s</div>
<div class="run">run_id: %(run_id)s</div></section></main></body></html>""" % {
        "lang": resolved,
        "close": html.escape(copy["close"], quote=True),
        "title": html.escape(title, quote=True),
        "message": html.escape(message, quote=True),
        "run_id": safe_run_id,
    }
