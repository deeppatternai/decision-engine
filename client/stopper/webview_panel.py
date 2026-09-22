"""Linux pywebview host for the audit Stop Panel.

The audit model, polling, cancellation, and lifecycle remain in ``panel``. This module replaces
only the Linux renderer because the private Tk 9 runtime does not consume Fontconfig fallback and
therefore cannot render CJK text even when the desktop fonts are installed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict

from client import i18n
from client.popup import backend
from client.stopper import panel


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}
body{background:#262624;color:#f5f5f4;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC","Noto Sans",sans-serif;font-size:14px}
#viewport{height:100vh;overflow-y:auto;padding:14px 16px;scrollbar-color:#5b5a55 #262624}
.empty{color:#8e8d86;font-size:15px;padding:10px 4px}
.row{display:grid;grid-template-columns:116px minmax(0,1fr);gap:14px;padding:10px 0;border-bottom:1px solid #3a3a37}
.row:last-child{border-bottom:0}.action{width:116px;height:40px;border:1px solid transparent;border-radius:4px;color:#fff;font-family:inherit;font-size:14px;font-weight:600;letter-spacing:0}
.action:disabled{cursor:default}.action.enabled{cursor:pointer}.action.enabled:hover{filter:brightness(.92)}
.content{min-width:0}.title{margin:0;color:#f5f5f4;font-size:16px;font-weight:700;line-height:1.4;overflow-wrap:anywhere}
.idline{display:flex;gap:7px;align-items:baseline;margin-top:4px;color:#8e8d86;font-size:12px;min-width:0}
.idline code{font-family:"Noto Sans Mono CJK SC","DejaVu Sans Mono",monospace;user-select:text;overflow-wrap:anywhere}
.detail{margin-top:5px;color:#8e8d86;font-size:14px;line-height:1.45;overflow-wrap:anywhere}
.auditor{margin-top:2px;font-family:"Noto Sans Mono CJK SC","DejaVu Sans Mono",monospace;font-size:13px;line-height:1.45;overflow-wrap:anywhere}
@media (max-width:520px){.row{grid-template-columns:1fr}.action{width:116px}.title{font-size:15px}}
</style></head><body><main id="viewport"></main><script>
const viewport=document.getElementById('viewport');
function node(tag,className,text){const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el}
window.renderPanel=function(payload){
  document.documentElement.lang=payload.lang||'en';
  const scrollTop=viewport.scrollTop;
  viewport.replaceChildren();
  if(!payload.rows.length){viewport.append(node('div','empty',payload.empty));return}
  for(const item of payload.rows){
    const row=node('section','row');
    const button=node('button','action'+(item.action.enabled?' enabled':''),item.action.text);
    button.type='button';button.disabled=!item.action.enabled;
    button.style.background=item.action.background;button.style.color=item.action.foreground;
    if(item.action.enabled){button.addEventListener('click',async()=>{button.disabled=true;await window.pywebview.api.stop(item.run_id)})}
    row.append(button);
    const content=node('div','content');content.append(node('h1','title',item.title));
    if(item.audit_id){const idline=node('div','idline');idline.append(node('span','',payload.id_label));idline.append(node('code','',item.audit_id));content.append(idline)}
    const detail=node('div','detail','\u00b7 '+item.detail.text);detail.style.color=item.detail.color;content.append(detail);
    for(const info of item.auditors){const line=node('div','auditor',info.text);line.style.color=info.color;content.append(line)}
    row.append(content);viewport.append(row);
  }
  viewport.scrollTop=scrollTop;
};
</script></body></html>"""


class _PanelApi:
    def __init__(self, app: "LinuxWebViewStopPanelApp") -> None:
        self._app = app

    def stop(self, run_id: Any) -> Dict[str, bool]:
        if isinstance(run_id, str) and 0 < len(run_id) <= panel._MAX_AUDIT_ID_LENGTH:
            threading.Thread(target=self._app._stop, args=(run_id,), daemon=True).start()
        return {"ok": True}


class LinuxWebViewStopPanelApp(panel.StopPanelApp):
    """Web renderer over the established StopPanelApp state machine."""

    def __init__(self, webview: Any) -> None:
        panel.initialize_panel_state(self)
        self._webview = webview
        self._closed = threading.Event()
        shell_titles = i18n.shell(i18n.resolve_locale(None)).get("surface_title", {})
        self._title = (
            shell_titles.get("audit", "Decision Engine")
            if isinstance(shell_titles, dict)
            else "Decision Engine"
        )
        self.window = webview.create_window(
            self._title,
            html=_HTML,
            js_api=_PanelApi(self),
            width=560,
            height=220,
            min_size=(420, 150),
            resizable=True,
            on_top=True,
            background_color=panel.BG,
            text_select=True,
            zoomable=False,
        )
        self.window.events.closed += lambda *_args: self._closed.set()

    def _action_view(self, row_key: str, run: Dict[str, Any]) -> Dict[str, Any]:
        render_run = {**run, "run_id": row_key}
        active = panel._entry_is_active(row_key, run)
        status = str(render_run.get("status") or "").lower()
        if status == "cancelling":
            key, fg, bg, enabled = "cancelling", panel.FG_MUTED, panel.BTN_FINISHED, False
        elif status == "cancelled":
            key, fg, bg, enabled = "cancelled", panel.FG_AMBER, panel.BTN_FINISHED, False
        elif status == "queued" and active and row_key in self._server_verified:
            key, fg, bg, enabled = "stop", "#FFFFFF", panel.FG_RED, True
        elif status == "queued" and active:
            key, fg, bg, enabled = "checking", panel.FG_MUTED, panel.BTN_FINISHED, False
        elif status == "running" and active:
            key, fg, bg, enabled = "running", "#FFFFFF", panel.FG_RED, False
        elif active:
            key, fg, bg, enabled = "checking", panel.FG_MUTED, panel.BTN_FINISHED, False
        else:
            key, fg, bg, enabled = "finished", panel.FG_MUTED, panel.BTN_FINISHED, False
        return {
            "text": panel.action_text(render_run, key),
            "foreground": fg,
            "background": bg,
            "enabled": enabled,
        }

    def _payload(self) -> Dict[str, Any]:
        now = time.time()
        rows = []
        for row_key, run in self._visible_runs():
            render_run = {**run, "run_id": row_key}
            rows.append({
                "run_id": row_key,
                "title": panel.run_title(render_run),
                "audit_id": panel.audit_id_text(render_run),
                "action": self._action_view(row_key, run),
                "detail": {
                    "text": panel.depth_line_text(render_run, now, self._frozen),
                    "color": panel.depth_line_color(render_run, now),
                },
                "auditors": panel._debug_auditor_details(
                    render_run, now, self._auditor_frozen
                ),
            })
        chrome = i18n.panel(i18n.resolve_locale(None))
        return {
            "lang": "en" if i18n.resolve_locale(None) == "en-US" else "zh-CN",
            "empty": chrome["empty"],
            "id_label": chrome["id_label"],
            "rows": rows,
        }

    def _render(self) -> None:
        encoded = json.dumps(json.dumps(self._payload(), ensure_ascii=True))
        try:
            self.window.evaluate_js("window.renderPanel(JSON.parse(%s))" % encoded)
        except Exception:
            pass

    def _tick_once(self) -> None:
        now = time.time()
        scheduled = time.monotonic()
        self._merge_disk()
        with self._lock:
            to_poll = [
                run_id for run_id, run in self.runs.items()
                if panel._entry_is_active(run_id, run)
                and panel.should_hub_poll(run)
                and run_id not in self._polling
                and self._next_poll_at.get(run_id, 0.0) <= scheduled
            ]
            self._polling.update(to_poll)
            poll_epochs = {
                run_id: self._state_epochs.get(run_id, 0) for run_id in to_poll
            }
        for run_id in to_poll:
            threading.Thread(
                target=self._poll,
                args=(run_id, poll_epochs[run_id]),
                daemon=True,
            ).start()
        self._prune(now)
        self._render()
        if not self._visible_runs():
            self._no_runs_since = self._no_runs_since or now
            if now - self._no_runs_since >= panel.IDLE_EXIT_S:
                try:
                    self.window.destroy()
                finally:
                    self._closed.set()
        else:
            self._no_runs_since = None

    def _worker(self) -> None:
        while not self._closed.is_set():
            self._tick_once()
            self._closed.wait(panel.POLL_INTERVAL_MS / 1000.0)

    def run(self) -> None:
        self._webview.start(self._worker, debug=False, private_mode=True)


def _run_tk_fallback() -> int:
    try:
        panel.StopPanelApp().run()
    except Exception as exc:
        sys.stderr.write("stop panel exited: %r\n" % exc)
        return 1
    return 0


def main() -> int:
    if os.getenv("DE_SKIP_STOPPER_LAUNCH") == "1":
        return 0
    if not panel.acquire_single_instance():
        return 0
    if not backend.ensure_webview(sys.executable, label="de-stop-panel"):
        return _run_tk_fallback()
    try:
        import webview

        LinuxWebViewStopPanelApp(webview).run()
    except Exception as exc:
        sys.stderr.write("stop panel WebView exited: %r\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
