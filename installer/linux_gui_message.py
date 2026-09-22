"""Small isolated pywebview host for Linux setup result messages."""

from __future__ import annotations

import html
import json
import sys
import threading
from typing import Any

from client.popup import backend


_MAX_INPUT_CHARS = 64 * 1024


class _DismissApi:
    def __init__(self) -> None:
        self._settled = threading.Event()
        self._bridge_thread = None

    def dismiss(self) -> dict[str, bool]:
        self._bridge_thread = threading.current_thread()
        self._settled.set()
        return {"ok": True}

    def close_after_delivery(self, window: Any) -> None:
        self._settled.wait()
        bridge = self._bridge_thread
        if bridge is not None and bridge is not threading.current_thread():
            bridge.join()
        try:
            window.destroy()
        except Exception:
            pass


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read(_MAX_INPUT_CHARS + 1)
    if len(raw) > _MAX_INPUT_CHARS:
        raise ValueError("message payload is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("message payload must be an object")
    for key, limit in (("title", 512), ("message", 16 * 1024), ("button", 64), ("lang", 32)):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError("invalid message field: %s" % key)
    payload["error"] = bool(payload.get("error"))
    return payload


def _render(payload: dict[str, Any]) -> str:
    accent = "#B42318" if payload["error"] else "#238636"
    return """<!doctype html>
<html lang="%s"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:100%%;height:100%%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC","Noto Sans",sans-serif;background:#fff;color:#172b4d}
.layout{display:grid;grid-template-columns:7px 1fr;min-height:100vh}.accent{background:%s}
.content{display:flex;min-width:0;flex-direction:column;padding:24px 28px 22px}
h1{margin:0;font-size:20px;line-height:1.35}p{margin:14px 0 22px;color:#526174;font-size:14px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}
.actions{margin-top:auto;text-align:right}button{min-width:96px;height:38px;border:0;border-radius:6px;background:#2f6feb;color:#fff;font-family:inherit;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#2459bd}button:focus{outline:3px solid rgba(47,111,235,.25);outline-offset:2px}
</style></head><body><main class="layout"><div class="accent"></div><section class="content">
<h1>%s</h1><p>%s</p><div class="actions"><button id="dismiss" autofocus>%s</button></div>
</section></main><script>
const button=document.getElementById('dismiss');let closing=false;
async function dismiss(){if(closing)return;closing=true;button.disabled=true;await window.pywebview.api.dismiss()}
button.addEventListener('click',dismiss);document.addEventListener('keydown',e=>{if(e.key==='Escape'||e.key==='Enter')dismiss()});
</script></body></html>""" % (
        html.escape(payload["lang"], quote=True),
        accent,
        html.escape(payload["title"], quote=True),
        html.escape(payload["message"], quote=True),
        html.escape(payload["button"], quote=True),
    )


def main() -> int:
    try:
        payload = _read_payload()
    except (OSError, ValueError, json.JSONDecodeError):
        return 2
    if not backend.ensure_webview(sys.executable, label="de-setup-result"):
        return 3
    try:
        import webview

        api = _DismissApi()
        window = webview.create_window(
            payload["title"],
            html=_render(payload),
            js_api=api,
            width=520,
            height=300,
            resizable=False,
            background_color="#FFFFFF",
            text_select=True,
            zoomable=False,
        )
        closer = threading.Thread(
            target=api.close_after_delivery,
            args=(window,),
            name="de-setup-result-closer",
            daemon=True,
        )
        closer.start()
        webview.start(debug=False, private_mode=True)
    except Exception:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
