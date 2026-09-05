"""Opt-in real Windows WinForms/WebView2 visual-capture E2E.

This suite opens the actual frameless pywebview popup. The page invokes the real js_api methods after two
animation frames, and the parent validates decoded pixels plus the Windows image clipboard. It is skipped
unless ``DE_RUN_WINDOWS_VISUAL_E2E=1`` so non-interactive CI never mistakes a mock for native evidence.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from client.popup.launcher import PopupSpec, render_artifact_html


_REPO = Path(__file__).resolve().parents[3]
_RUN_NATIVE = os.environ.get("DE_RUN_WINDOWS_VISUAL_E2E") == "1" and sys.platform == "win32"


@unittest.skipUnless(_RUN_NATIVE, "set DE_RUN_WINDOWS_VISUAL_E2E=1 on an interactive Windows desktop")
class WindowsVisualCaptureE2E(unittest.TestCase):
    @staticmethod
    def _shell_command(html, result_path, title, context_path=None):
        command = [
            sys.executable,
            "-c",
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('client.popup.native_shell',run_name='__main__')",
            str(_REPO),
            "--html", str(html),
            "--result-path", str(result_path),
            "--title", title,
        ]
        if context_path is not None:
            command += ["--context", str(context_path)]
        return command

    def test_real_popup_crops_artifact_and_sets_image_clipboard(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html = temp / "capture.html"
            result_path = temp / "result.json"
            html.write_text(textwrap.dedent("""
                <!doctype html><html><head><meta charset="utf-8"><style>
                *{box-sizing:border-box} html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#09111f}
                header{height:42px;background:rgb(4,8,12)}
                main{height:calc(100% - 42px);display:grid;grid-template-columns:62% 38%;padding:12px;gap:12px}
                .artifact{position:relative;background:rgb(17,34,51);border:6px solid rgb(220,180,40);overflow:hidden}
                .artifact .visual{position:absolute;left:13%;top:16%;width:50%;height:48%;background:rgb(70,110,210)}
                .artifact .ge-signature{position:absolute;right:18px;bottom:14px;width:96px;height:24px;background:rgb(250,0,200)}
                .chat-pane{background:rgb(0,250,80)}
                </style></head><body><header></header><main>
                <section class="artifact"><div class="visual"></div><div class="ge-signature"></div></section>
                <aside class="chat-pane"></aside></main><script>
                addEventListener('pywebviewready', function () {
                  requestAnimationFrame(function(){requestAnimationFrame(function(){setTimeout(async function(){
                    async function settle(){await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(function(){setTimeout(resolve,200)})})})}
                    var art=document.querySelector('.artifact'), r=art.getBoundingClientRect();
                    var request=[r.x,r.y,r.width,r.height,innerWidth,innerHeight];
                    var normalRequest=request.slice(), normalDpr=devicePixelRatio;
                    var normal=await window.pywebview.api.snapshot_region(request);
                    var copy=await window.pywebview.api.copy_visual_image(request);
                    var maximizedState=await window.pywebview.api.toggle_maximize(); await settle();
                    r=art.getBoundingClientRect(); request=[r.x,r.y,r.width,r.height,innerWidth,innerHeight];
                    var maximizedRequest=request.slice(), maximizedDpr=devicePixelRatio;
                    var maximized=await window.pywebview.api.snapshot_region(request);
                    var restoredState=await window.pywebview.api.toggle_maximize(); await settle();
                    r=art.getBoundingClientRect(); request=[r.x,r.y,r.width,r.height,innerWidth,innerHeight];
                    var restoredRequest=request.slice(), restoredDpr=devicePixelRatio;
                    var restored=await window.pywebview.api.snapshot_region(request);
                    await window.pywebview.api.commit({normal:normal,maximized:maximized,restored:restored,copy:copy,
                      maximizedState:maximizedState,restoredState:restoredState,
                      requests:{normal:normalRequest,maximized:maximizedRequest,restored:restoredRequest},
                      dprs:{normal:normalDpr,maximized:maximizedDpr,restored:restoredDpr}});
                  },250)})});
                });
                </script></body></html>
            """), encoding="utf-8")

            command = self._shell_command(html, result_path, "DE Windows capture E2E")
            completed = subprocess.run(command, cwd=_REPO, timeout=25, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertEqual(payload["copy"], {"ok": True})
            self.assertTrue(payload["maximizedState"]["ok"])
            self.assertTrue(payload["maximizedState"]["maximized"])
            self.assertTrue(payload["restoredState"]["ok"])
            self.assertFalse(payload["restoredState"]["maximized"])
            normal_size = None
            for state_name in ("normal", "maximized", "restored"):
                with self.subTest(state=state_name):
                    self.assertTrue(payload[state_name]["ok"])
                    png = base64.b64decode(payload[state_name]["image"].split(",", 1)[1], validate=True)
                    stats, size = self._png_stats(png)
                    self.assertGreater(size[0], 400)
                    self.assertGreater(size[1], 300)
                    request = payload["requests"][state_name]
                    dpr = payload["dprs"][state_name]
                    self.assertLessEqual(abs(size[0] - request[2] * dpr), 2.0)
                    self.assertLessEqual(abs(size[1] - request[3] * dpr), 2.0)
                    self.assertGreater(stats["signature"], 0, stats)
                    self.assertEqual(stats["chat"], 0, stats)
                    if state_name == "normal":
                        normal_size = size
            clipboard_png = self._clipboard_png()
            self.assertIsNotNone(clipboard_png)
            clipboard_stats, clipboard_size = self._png_stats(clipboard_png)
            self.assertEqual(clipboard_size, normal_size)
            self.assertGreater(clipboard_stats["signature"], 0, clipboard_stats)
            self.assertEqual(clipboard_stats["chat"], 0, clipboard_stats)

    def test_real_production_layout_region_then_copy_keeps_exact_artifact_capture_working(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-production-capture-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "production-capture.html"
            result_path = temp / "result.json"
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500'>"
                   "<rect width='800' height='500' fill='#17324d'/>"
                   "<rect x='80' y='70' width='360' height='260' fill='#4169e1'/></svg>")
            html = render_artifact_html(PopupSpec(
                kind="ge", title="Windows production capture E2E",
                artifact={"kind": "svg", "data": svg},
            ))
            style = textwrap.dedent("""
                <style>
                .ge-signature{background:rgb(250,0,200)!important;color:#fff!important}
                .chat-pane{background:rgb(0,250,80)!important}
                </style>
            """)
            driver = textwrap.dedent("""
                <script>addEventListener('pywebviewready',function(){setTimeout(async function(){
                  var art=document.querySelector('.artifact'), before=art.getBoundingClientRect();
                  var regionRequest=[before.x+30,before.y+35,220,160,innerWidth,innerHeight];
                  var region=await window.pywebview.api.snapshot_region(regionRequest);
                  var signature=document.querySelector('.ge-signature'), signatureRect=signature.getBoundingClientRect();
                  var partialSignatureRequest=[signatureRect.x,signatureRect.y,signatureRect.width/2,
                    signatureRect.height,innerWidth,innerHeight];
                  var fullSignatureRequest=[signatureRect.x,signatureRect.y,signatureRect.width,
                    signatureRect.height,innerWidth,innerHeight];
                  var partialSignature=await window.pywebview.api.snapshot_region(partialSignatureRequest);
                  var fullSignature=await window.pywebview.api.snapshot_region(fullSignatureRequest);
                  await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(resolve)})});
                  var after=art.getBoundingClientRect();
                  var copyRequest=[after.x,after.y,after.width,after.height,innerWidth,innerHeight];
                  var copy=await window.pywebview.api.copy_visual_image(copyRequest);
                  await window.pywebview.api.commit({region:region,regionRequest:regionRequest,
                    partialSignature:partialSignature,partialSignatureRequest:partialSignatureRequest,
                    fullSignature:fullSignature,fullSignatureRequest:fullSignatureRequest,copy:copy,
                    before:[before.x,before.y,before.width,before.height],
                    after:[after.x,after.y,after.width,after.height],dpr:devicePixelRatio});
                },500)});</script>
            """)
            html_path.write_text(
                html.replace("</head>", style + "</head>").replace("</body>", driver + "</body>"),
                encoding="utf-8",
            )

            completed = subprocess.run(
                self._shell_command(html_path, result_path, "DE Windows production capture E2E"),
                cwd=_REPO, timeout=25, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertTrue(payload["region"]["ok"], payload)
            self.assertEqual(payload["copy"], {"ok": True}, payload)
            region_png = base64.b64decode(
                payload["region"]["image"].split(",", 1)[1], validate=True
            )
            region_stats, region_size = self._png_stats(region_png)
            region_request = payload["regionRequest"]
            self.assertLessEqual(
                abs(region_size[0] - region_request[2] * payload["dpr"]), 2.0
            )
            self.assertLessEqual(
                abs(region_size[1] - region_request[3] * payload["dpr"]), 2.0
            )
            self.assertGreater(region_stats["visual"], 0, region_stats)
            self.assertEqual(region_stats["signature"], 0, region_stats)
            self.assertEqual(region_stats["chat"], 0, region_stats)
            self.assertEqual(payload["after"], payload["before"])

            partial_png = base64.b64decode(
                payload["partialSignature"]["image"].split(",", 1)[1], validate=True
            )
            full_signature_png = base64.b64decode(
                payload["fullSignature"]["image"].split(",", 1)[1], validate=True
            )
            partial_stats, partial_size = self._png_stats(partial_png)
            full_signature_stats, full_signature_size = self._png_stats(
                full_signature_png
            )
            self.assertGreater(partial_stats["signature"], 0, partial_stats)
            self.assertLess(
                partial_stats["signature"], full_signature_stats["signature"]
            )
            self.assertEqual(partial_stats["chat"], 0, partial_stats)
            self.assertEqual(full_signature_stats["chat"], 0, full_signature_stats)
            self.assertLess(partial_size[0], full_signature_size[0])
            self.assertLessEqual(
                abs(
                    partial_size[0]
                    - payload["partialSignatureRequest"][2] * payload["dpr"]
                ),
                2.0,
            )
            self.assertLessEqual(
                abs(
                    full_signature_size[0]
                    - payload["fullSignatureRequest"][2] * payload["dpr"]
                ),
                2.0,
            )
            clipboard_png = self._clipboard_png()
            self.assertIsNotNone(clipboard_png)
            stats, size = self._png_stats(clipboard_png)
            expected = payload["after"]
            self.assertLessEqual(abs(size[0] - expected[2] * payload["dpr"]), 2.0)
            self.assertLessEqual(abs(size[1] - expected[3] * payload["dpr"]), 2.0)
            self.assertGreater(stats["signature"], 0, stats)
            self.assertEqual(stats["chat"], 0, stats)

    def test_region_mask_install_failure_restores_live_artifact(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-region-rollback-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "region-rollback.html"
            result_path = temp / "result.json"
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500'>"
                   "<rect width='800' height='500' fill='#17324d'/></svg>")
            html = render_artifact_html(PopupSpec(
                kind="ge", title="Windows region rollback E2E",
                artifact={"kind": "svg", "data": svg},
            ))
            driver = textwrap.dedent("""
                <script>addEventListener('pywebviewready',function(){setTimeout(async function(){
                  var art=document.querySelector('.artifact'), parent=art.parentNode, next=art.nextSibling;
                  var originalStyle=art.getAttribute('style'), before=art.getBoundingClientRect();
                  var request=[before.x+20,before.y+20,100,80,innerWidth,innerHeight];
                  var originalAppend=Node.prototype.appendChild;
                  Node.prototype.appendChild=function(node){
                    if(this===document.documentElement&&node&&node.id==='de-windows-region-capture-privacy')
                      throw new Error('forced region mask install failure');
                    return originalAppend.call(this,node);
                  };
                  var result;
                  try{result=await window.pywebview.api.snapshot_region(request);}
                  finally{Node.prototype.appendChild=originalAppend;}
                  await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(resolve)})});
                  var after=art.getBoundingClientRect();
                  await window.pywebview.api.commit({result:result,connected:art.isConnected,
                    sameParent:art.parentNode===parent,sameNext:art.nextSibling===next,
                    sameStyle:(art.getAttribute('style')||'')===(originalStyle||''),
                    maskAbsent:!document.getElementById('de-windows-region-capture-privacy'),
                    stateAbsent:!window.__deWindowsRegionCapturePrivacy,
                    before:[before.x,before.y,before.width,before.height],
                    after:[after.x,after.y,after.width,after.height]});
                },500)});</script>
            """)
            html_path.write_text(html.replace("</body>", driver + "</body>"), encoding="utf-8")

            completed = subprocess.run(
                self._shell_command(html_path, result_path, "DE Windows region rollback E2E"),
                cwd=_REPO, timeout=20, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertEqual(payload["result"], {"ok": False}, payload)
            self.assertTrue(payload["connected"], payload)
            self.assertTrue(payload["sameParent"], payload)
            self.assertTrue(payload["sameNext"], payload)
            self.assertTrue(payload["sameStyle"], payload)
            self.assertTrue(payload["maskAbsent"], payload)
            self.assertTrue(payload["stateAbsent"], payload)
            self.assertEqual(payload["after"], payload["before"])

    def test_region_capture_preserves_ancestor_style_scroll_and_rejects_top_layer(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-region-stability-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "region-stability.html"
            result_path = temp / "result.json"
            html_path.write_text(textwrap.dedent("""
                <!doctype html><html><head><meta charset="utf-8"><style>
                *{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#09111f}
                header{height:42px;background:rgb(4,8,12)}
                main{height:calc(100% - 42px);display:grid;grid-template-columns:62% 38%;padding:12px;gap:12px}
                .scroll-pane{height:100%;overflow:auto;background:#25384b}
                .spacer{height:120px}
                .artifact{position:relative;width:480px;height:700px;background:rgb(17,34,51);overflow:hidden}
                .scroll-pane .artifact .ancestor-marker{position:absolute;left:30px;top:35px;width:220px;height:160px;background:rgb(50,110,220)}
                .ge-signature{position:absolute;right:18px;bottom:14px;width:96px;height:24px;background:rgb(250,0,200)}
                .chat-pane{background:rgb(0,250,80)}
                </style></head><body><header></header><main>
                <section class="scroll-pane"><div class="spacer"></div><section class="artifact">
                <div class="ancestor-marker"></div><div class="ge-signature"></div></section><div class="spacer"></div></section>
                <aside class="chat-pane"></aside></main><dialog>top-layer content</dialog>
                <div id="test-popover" popover>popover content</div><script>
                addEventListener('pywebviewready',function(){setTimeout(async function(){
                  var pane=document.querySelector('.scroll-pane'),art=document.querySelector('.artifact');
                  pane.scrollTop=230;
                  await new Promise(function(resolve){requestAnimationFrame(function(){requestAnimationFrame(resolve)})});
                  var before=art.getBoundingClientRect(),beforeScroll=pane.scrollTop,paneRect=pane.getBoundingClientRect();
                  var visibleTop=Math.max(0,before.y,paneRect.y);
                  var request=[before.x+40,visibleTop+20,120,80,innerWidth,innerHeight];
                  var clippedRequest=[before.x+40,Math.max(1,paneRect.y-30),120,20,innerWidth,innerHeight];
                  var clipped=await window.pywebview.api.snapshot_region(clippedRequest);
                  var normal=await window.pywebview.api.snapshot_region(request);
                  var after=art.getBoundingClientRect(),afterScroll=pane.scrollTop;
                  var dialog=document.querySelector('dialog');dialog.showModal();
                  var blocked=await window.pywebview.api.snapshot_region(request);dialog.close();
                  var recovered=await window.pywebview.api.snapshot_region(request);
                  var fullscreenOverride=false,fullscreenBlocked=null;
                  try{Object.defineProperty(document,'fullscreenElement',{configurable:true,value:document.body});
                    fullscreenOverride=document.fullscreenElement===document.body;
                    if(fullscreenOverride)fullscreenBlocked=await window.pywebview.api.snapshot_region(request);
                  }finally{try{delete document.fullscreenElement}catch(fullscreenRestore){}}
                  var popover=document.getElementById('test-popover');
                  var popoverSupported=typeof popover.showPopover==='function',popoverBlocked=null;
                  if(popoverSupported){popover.showPopover();
                    popoverBlocked=await window.pywebview.api.snapshot_region(request);popover.hidePopover();}
                  var finalRecovered=await window.pywebview.api.snapshot_region(request);
                  await window.pywebview.api.commit({clipped:clipped,normal:normal,blocked:blocked,recovered:recovered,
                    fullscreenOverride:fullscreenOverride,fullscreenBlocked:fullscreenBlocked,
                    popoverSupported:popoverSupported,popoverBlocked:popoverBlocked,finalRecovered:finalRecovered,
                    before:[before.x,before.y,before.width,before.height],
                    paneRect:[paneRect.x,paneRect.y,paneRect.width,paneRect.height],clippedRequest:clippedRequest,
                    after:[after.x,after.y,after.width,after.height],
                    beforeScroll:beforeScroll,afterScroll:afterScroll});
                },500)});
                </script></body></html>
            """), encoding="utf-8")

            completed = subprocess.run(
                self._shell_command(html_path, result_path, "DE Windows region stability E2E"),
                cwd=_REPO, timeout=25, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertLess(payload["before"][1], payload["paneRect"][1], payload)
            self.assertGreaterEqual(payload["clippedRequest"][1], max(0, payload["before"][1]), payload)
            self.assertLessEqual(
                payload["clippedRequest"][1] + payload["clippedRequest"][3],
                payload["paneRect"][1], payload,
            )
            self.assertEqual(payload["clipped"], {"ok": False}, payload)
            self.assertTrue(payload["normal"]["ok"], payload)
            self.assertEqual(payload["blocked"], {"ok": False}, payload)
            self.assertTrue(payload["recovered"]["ok"], payload)
            self.assertTrue(payload["fullscreenOverride"], payload)
            self.assertEqual(payload["fullscreenBlocked"], {"ok": False}, payload)
            self.assertTrue(payload["popoverSupported"], payload)
            self.assertEqual(payload["popoverBlocked"], {"ok": False}, payload)
            self.assertTrue(payload["finalRecovered"]["ok"], payload)
            self.assertEqual(payload["after"], payload["before"])
            self.assertEqual(payload["afterScroll"], payload["beforeScroll"])
            for name in ("normal", "recovered", "finalRecovered"):
                png = base64.b64decode(
                    payload[name]["image"].split(",", 1)[1], validate=True
                )
                stats, _size = self._png_stats(png)
                self.assertGreater(stats["visual"], 0, stats)
                self.assertEqual(stats["signature"], 0, stats)
                self.assertEqual(stats["chat"], 0, stats)

    def test_region_cleanup_failure_retains_state_and_recovers_without_stacking(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-region-cleanup-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "region-cleanup.html"
            result_path = temp / "result.json"
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500'>"
                   "<rect width='800' height='500' fill='#17324d'/></svg>")
            html = render_artifact_html(PopupSpec(
                kind="ge", title="Windows region cleanup E2E",
                artifact={"kind": "svg", "data": svg},
            ))
            driver = textwrap.dedent("""
                <script>addEventListener('pywebviewready',function(){setTimeout(async function(){
                  var art=document.querySelector('.artifact'),before=art.getBoundingClientRect();
                  var request=[before.x+20,before.y+20,100,80,innerWidth,innerHeight];
                  var originalRemoveChild=Node.prototype.removeChild,originalRemove=Element.prototype.remove;
                  Node.prototype.removeChild=function(node){
                    if(node&&node.id==='de-windows-region-capture-privacy')throw new Error('forced removeChild failure');
                    return originalRemoveChild.call(this,node);
                  };
                  Element.prototype.remove=function(){
                    if(this.id==='de-windows-region-capture-privacy')throw new Error('forced remove failure');
                    return originalRemove.call(this);
                  };
                  var first=await window.pywebview.api.snapshot_region(request);
                  var firstRoot=document.getElementById('de-windows-region-capture-privacy');
                  var firstMasks=document.querySelectorAll('#de-windows-region-capture-privacy').length;
                  var firstState=!!window.__deWindowsRegionCapturePrivacy;
                  var firstHidden=!firstRoot||getComputedStyle(firstRoot).display==='none';
                  Node.prototype.removeChild=originalRemoveChild;Element.prototype.remove=originalRemove;
                  var recovery=await window.pywebview.api.snapshot_region(request);
                  var masksAfterRecovery=document.querySelectorAll('#de-windows-region-capture-privacy').length;
                  var stateAfterRecovery=!!window.__deWindowsRegionCapturePrivacy;
                  var finalCapture=await window.pywebview.api.snapshot_region(request);
                  var capturedTimer=null,originalSetTimeout=window.setTimeout;
                  window.setTimeout=function(callback,delay){
                    if(delay===12000){capturedTimer=callback;return 12000;}
                    return originalSetTimeout.apply(this,arguments);
                  };
                  Node.prototype.removeChild=function(node){
                    if(node&&node.id==='de-windows-region-capture-privacy')throw new Error('forced timer removeChild failure');
                    return originalRemoveChild.call(this,node);
                  };
                  Element.prototype.remove=function(){
                    if(this.id==='de-windows-region-capture-privacy')throw new Error('forced timer remove failure');
                    return originalRemove.call(this);
                  };
                  var timerFailure=await window.pywebview.api.snapshot_region(request);
                  var timerRoot=document.getElementById('de-windows-region-capture-privacy');
                  var timerCaptured=typeof capturedTimer==='function';
                  var timerHidden=!timerRoot||getComputedStyle(timerRoot).display==='none';
                  Node.prototype.removeChild=originalRemoveChild;Element.prototype.remove=originalRemove;
                  window.setTimeout=originalSetTimeout;
                  if(capturedTimer)capturedTimer();
                  var timerMasksAfterCallback=document.querySelectorAll('#de-windows-region-capture-privacy').length;
                  var timerStateAfterCallback=!!window.__deWindowsRegionCapturePrivacy;
                  var timerRecovery=await window.pywebview.api.snapshot_region(request);
                  var after=art.getBoundingClientRect();
                  await window.pywebview.api.commit({first:first,firstMasks:firstMasks,firstState:firstState,firstHidden:firstHidden,
                    recovery:recovery,masksAfterRecovery:masksAfterRecovery,stateAfterRecovery:stateAfterRecovery,
                    finalCapture:finalCapture,finalMasks:document.querySelectorAll('#de-windows-region-capture-privacy').length,
                    timerFailure:timerFailure,timerCaptured:timerCaptured,timerHidden:timerHidden,
                    timerMasksAfterCallback:timerMasksAfterCallback,timerStateAfterCallback:timerStateAfterCallback,
                    timerRecovery:timerRecovery,
                    finalState:!!window.__deWindowsRegionCapturePrivacy,connected:art.isConnected,
                    before:[before.x,before.y,before.width,before.height],
                    after:[after.x,after.y,after.width,after.height]});
                },500)});</script>
            """)
            html_path.write_text(html.replace("</body>", driver + "</body>"), encoding="utf-8")

            completed = subprocess.run(
                self._shell_command(html_path, result_path, "DE Windows region cleanup E2E"),
                cwd=_REPO, timeout=25, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertEqual(payload["first"], {"ok": False}, payload)
            self.assertEqual(payload["firstMasks"], 1, payload)
            self.assertTrue(payload["firstState"], payload)
            self.assertTrue(payload["firstHidden"], payload)
            self.assertEqual(payload["recovery"], {"ok": False}, payload)
            self.assertEqual(payload["masksAfterRecovery"], 0, payload)
            self.assertFalse(payload["stateAfterRecovery"], payload)
            self.assertTrue(payload["finalCapture"]["ok"], payload)
            self.assertEqual(payload["timerFailure"], {"ok": False}, payload)
            self.assertTrue(payload["timerCaptured"], payload)
            self.assertTrue(payload["timerHidden"], payload)
            self.assertEqual(payload["timerMasksAfterCallback"], 0, payload)
            self.assertFalse(payload["timerStateAfterCallback"], payload)
            self.assertTrue(payload["timerRecovery"]["ok"], payload)
            self.assertEqual(payload["finalMasks"], 0, payload)
            self.assertFalse(payload["finalState"], payload)
            self.assertTrue(payload["connected"], payload)
            self.assertEqual(payload["after"], payload["before"])

    def test_real_popup_header_controls_are_uniformly_spaced(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-spacing-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "spacing.html"
            result_path = temp / "result.json"
            html = render_artifact_html(PopupSpec(
                kind="ge", title="Windows spacing E2E",
                artifact={"kind": "svg", "data": "<svg xmlns='http://www.w3.org/2000/svg'/>"},
            ))
            driver = textwrap.dedent("""
                <script>(function(){var attempts=0;function measure(){
                  var ids=['region-btn','shot-btn','share-btn','hide-btn','maximize-btn','close-btn'];
                  var nodes=ids.map(function(id){return document.getElementById(id)});
                  if(nodes.some(function(node){return !node})) {
                    if(++attempts<240){requestAnimationFrame(measure);return;}
                    window.pywebview.api.commit({error:'header controls did not initialize'});return;
                  }
                  var rects=nodes.map(function(node){var r=node.getBoundingClientRect();return [r.x,r.width]});
                  var header=document.querySelector('header.pywebview-drag-region');
                  window.pywebview.api.commit({rects:rects,gap:getComputedStyle(header).columnGap,
                    directChildren:nodes.every(function(node){return node.parentElement===header})});
                }measure()})();</script>
            """)
            html_path.write_text(html.replace("</body>", driver + "</body>"), encoding="utf-8")
            completed = subprocess.run(
                self._shell_command(html_path, result_path, "Windows spacing E2E"),
                cwd=_REPO, timeout=20, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertNotIn("error", payload)
            self.assertEqual(payload["gap"], "12px")
            self.assertTrue(payload["directChildren"])
            self.assertEqual([round(rect[1]) for rect in payload["rects"]], [30] * 6)
            centers = [rect[0] + rect[1] / 2 for rect in payload["rects"]]
            self.assertEqual([round(centers[i + 1] - centers[i]) for i in range(5)], [42] * 5)

    def test_real_system_share_ui_is_modal_with_png_handler_armed(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-share-e2e-") as temp_dir:
            existing_share_files = set(Path(tempfile.gettempdir()).glob("de-ge-share-*.png"))
            temp = Path(temp_dir)
            html = temp / "share.html"
            result_path = temp / "result.json"
            title = "DE Windows share E2E"
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500'>"
                   "<rect width='800' height='500' fill='#17324d'/>"
                   "<rect x='80' y='70' width='360' height='260' fill='#4169e1'/></svg>")
            page = render_artifact_html(PopupSpec(
                kind="ge", title=title, artifact={"kind": "svg", "data": svg},
            ))
            driver = textwrap.dedent("""
                <script>addEventListener('pywebviewready',function(){setTimeout(async function(){
                  var r=document.querySelector('.artifact').getBoundingClientRect();
                  var request=[r.x,r.y,r.width,r.height,innerWidth,innerHeight];
                  var share=await window.pywebview.api.share_visual_image(request);
                  setTimeout(function(){window.pywebview.api.commit({share:share})},3000);
                },5000)});</script>
            """)
            html.write_text(page.replace("</body>", driver + "</body>"), encoding="utf-8")
            process = subprocess.Popen(
                self._shell_command(html, result_path, title), cwd=_REPO,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
            self.assertTrue(self._activate_window(title, process, timeout=5),
                            "the popup HWND could not be activated before native share")
            first_modal = self._wait_for_window_enabled(title, process, enabled=False, timeout=15)
            if not first_modal:
                stdout, stderr = process.communicate(timeout=15)
                payload = result_path.read_text(encoding="utf-8") if result_path.exists() else "missing result"
                self.fail("the first native share experience never disabled the popup HWND; "
                          f"result={payload}; stderr={stderr or stdout}")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertEqual(payload["share"], {"ok": True}, stderr or stdout)
            new_share_files = set(Path(tempfile.gettempdir()).glob("de-ge-share-*.png")) - existing_share_files
            self.assertEqual(new_share_files, set(), "share PNG survived the popup process")

    def test_real_production_region_flow_attaches_and_sends_crop(self):
        with tempfile.TemporaryDirectory(prefix="de-ge-win-region-e2e-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "region.html"
            context_path = temp / "context.json"
            result_path = temp / "result.json"
            svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500'>"
                   "<rect width='800' height='500' fill='#17324d'/><rect x='80' y='70' width='360' height='260' fill='#4169e1'/>"
                   "</svg>")
            html = render_artifact_html(PopupSpec(
                kind="ge", title="Windows region E2E", artifact={"kind": "svg", "data": svg},
            ))
            html = html.replace(
                "</head>",
                "<style>.ge-toast{background:rgb(255,128,0)!important}"
                ".ge-region-box{border-color:rgb(0,255,255)!important;"
                "background:rgb(255,0,0)!important}</style></head>",
            )
            driver = textwrap.dedent("""
                <script>(function(){
                  var tries=0;
                  function start(){
                    var api=window.pywebview&&window.pywebview.api, rb=document.getElementById('region-btn');
                    if(!api||!rb||rb.disabled){if(++tries<80)setTimeout(start,100);return;}
                    if(!window.__deE2EAskWrapped){
                      var originalAsk=api.ask;
                      api.ask=function(turnId,text,images){window.__deE2EAskCalls=(window.__deE2EAskCalls||0)+1;
                        window.__deE2EImages=(images||[]).slice();
                        var response=originalAsk.call(api,turnId,text,images);
                        Promise.resolve(response).then(function(value){window.__deE2EAskResult=value;});
                        return response;};
                      window.__deE2EAskWrapped=true;
                    }
                    var art=document.querySelector('.artifact'), r=art.getBoundingClientRect();
                    rb.click();
                    var x1=r.left+30,y1=r.top+35,x2=Math.min(r.right-30,x1+220),y2=Math.min(r.bottom-30,y1+160);
                    window.__deE2ESelection=[x2-x1,y2-y1,devicePixelRatio];
                    art.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0,clientX:x1,clientY:y1}));
                    window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,button:0,clientX:x2,clientY:y2}));
                    window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:0,clientX:x2,clientY:y2}));
                    waitForChip();
                  }
                  function waitForChip(){
                    var chips=document.querySelectorAll('#ge-imgchips .imgchip');
                    if(!chips.length){if(++tries<120)setTimeout(waitForChip,100);return;}
                    var input=document.getElementById('composer-input');
                    input.value='What is inside the selected region?';
                    document.getElementById('composer-send').click();
                    setTimeout(function(){
                      var users=document.querySelectorAll('.msg.user'), user=users[users.length-1];
                      var sentPreview=user&&user.querySelector('.msg-images .imgchip-preview');
                      window.pywebview.api.commit({chipCount:chips.length,userText:user&&user.firstChild.textContent,
                        sentPreviewCount:user&&user.querySelectorAll('.msg-images .imgchip-preview').length,
                        sentPreviewSrc:sentPreview&&sentPreview.src,askCalls:window.__deE2EAskCalls||0,
                        askResult:window.__deE2EAskResult,
                        image:window.__deE2EImages&&window.__deE2EImages[0],selection:window.__deE2ESelection});
                    },300);
                  }
                  setTimeout(start,200);
                })();</script>
            """)
            html_path.write_text(html.replace("</body>", driver + "</body>"), encoding="utf-8")
            context_path.write_text(json.dumps({
                "title": "Windows region E2E", "mode": "svg", "source_text": "Synthetic E2E context",
            }), encoding="utf-8")
            completed = subprocess.run(
                self._shell_command(html_path, result_path, "DE Windows region E2E", context_path),
                cwd=_REPO, timeout=30, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result_path.read_text(encoding="utf-8"))["result"]
            self.assertEqual(payload["chipCount"], 1)
            self.assertEqual(payload["userText"], "What is inside the selected region?")
            self.assertEqual(payload["sentPreviewCount"], 1)
            self.assertEqual(payload["sentPreviewSrc"], payload["image"])
            self.assertEqual(payload["askCalls"], 1, "real send path did not call PopupApi.ask exactly once")
            self.assertTrue(payload["askResult"]["ok"], payload)
            self.assertTrue(payload["image"].startswith("data:image/png;base64,"))
            region_png = base64.b64decode(payload["image"].split(",", 1)[1], validate=True)
            region_stats, region_size = self._png_stats(region_png)
            expected_width, expected_height, dpr = payload["selection"]
            self.assertLessEqual(abs(region_size[0] - expected_width * dpr), 2.0)
            self.assertLessEqual(abs(region_size[1] - expected_height * dpr), 2.0)
            self.assertGreater(region_stats["visual"], 0, region_stats)
            self.assertEqual(region_stats["chat"], 0, region_stats)
            self.assertEqual(region_stats["toast"], 0, region_stats)
            self.assertEqual(region_stats["selection"], 0, region_stats)
            self.assertEqual(region_stats["selection_border"], 0, region_stats)
            self.assertFalse(context_path.exists(), "native shell must delete the privacy-sensitive context file")

    @staticmethod
    def _activate_window(title, process, timeout):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            handles = []

            def collect(hwnd, _lparam):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, len(buffer))
                    if buffer.value == title:
                        handles.append(hwnd)
                return True

            user32.EnumWindows(callback_type(collect), 0)
            if handles:
                user32.ShowWindow(handles[0], 5)
                user32.BringWindowToTop(handles[0])
                user32.SetForegroundWindow(handles[0])
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _wait_for_window_enabled(title, process, *, enabled, timeout):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            handles = []

            def collect(hwnd, _lparam):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, len(buffer))
                    if buffer.value == title:
                        handles.append(hwnd)
                return True

            user32.EnumWindows(callback_type(collect), 0)
            for hwnd in handles:
                if bool(user32.IsWindowEnabled(hwnd)) is enabled:
                    return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _png_stats(png):
        import clr
        clr.AddReference("System.Drawing")
        from System import Array, Byte
        from System.Drawing import Bitmap
        from System.Drawing import Rectangle
        from System.Drawing.Imaging import ImageLockMode, PixelFormat
        from System.IO import MemoryStream
        from System.Runtime.InteropServices import Marshal

        stream = MemoryStream(Array[Byte](png))
        bitmap = Bitmap(stream)
        try:
            bits = bitmap.LockBits(Rectangle(0, 0, bitmap.Width, bitmap.Height), ImageLockMode.ReadOnly,
                                   PixelFormat.Format32bppArgb)
            try:
                stride = abs(int(bits.Stride))
                raw = Array.CreateInstance(Byte, stride * bitmap.Height)
                Marshal.Copy(bits.Scan0, raw, 0, len(raw))
                stats = {
                    "signature": 0, "chat": 0, "visual": 0, "toast": 0,
                    "selection": 0, "selection_border": 0, "visible": 0,
                }
                for y in range(bitmap.Height):
                    row = y * stride
                    for x in range(bitmap.Width):
                        offset = row + x * 4
                        b, g, r, a = (int(raw[offset + index]) for index in range(4))
                        if a:
                            stats["visible"] += 1
                        if r > 230 and b > 170 and g < 40:
                            stats["signature"] += 1
                        if g > 220 and r < 30 and b < 120:
                            stats["chat"] += 1
                        if b > 180 and 70 < g < 150 and 30 < r < 100:
                            stats["visual"] += 1
                        if r > 240 and 90 < g < 170 and b < 30:
                            stats["toast"] += 1
                        if r > 240 and g < 20 and b < 20:
                            stats["selection"] += 1
                        if r < 20 and g > 240 and b > 240:
                            stats["selection_border"] += 1
                return stats, (bitmap.Width, bitmap.Height)
            finally:
                bitmap.UnlockBits(bits)
        finally:
            bitmap.Dispose()
            stream.Dispose()

    @staticmethod
    def _clipboard_png():
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System import Array, Byte
        from System.Drawing.Imaging import ImageFormat
        from System.IO import MemoryStream
        from System import Threading
        from System.Windows.Forms import Clipboard

        state = {"png": None}

        def read_clipboard():
            image = Clipboard.GetImage()
            try:
                if Clipboard.ContainsImage() and image is not None:
                    stream = MemoryStream()
                    try:
                        image.Save(stream, ImageFormat.Png)
                        state["png"] = bytes(stream.ToArray())
                    finally:
                        stream.Dispose()
            finally:
                if image is not None:
                    image.Dispose()

        thread = Threading.Thread(Threading.ThreadStart(read_clipboard))
        thread.SetApartmentState(Threading.ApartmentState.STA)
        thread.Start()
        thread.Join()
        return state["png"]


if __name__ == "__main__":
    unittest.main()
