"""Coverage for the Windows detached-popup re-show remedy (``native_shell._win_after_show``).

Windows bug (handoff): a DETACHED_PROCESS child's first FRAMELESS (WS_POPUP) window can come up
hidden — a bordered window was force-shown, only the frameless popup regressed. The remedy re-issues
pywebview's public ``Window.show()`` from the ``loaded`` event. This pins the two behaviours that
matter: it DOES re-show, and it is best-effort (a failing show never crashes the popup).

NOTE: this is unit coverage of the remedy's contract, NOT a reproduction of the original visibility
failure — that bug did not reproduce in an interactive-desktop spawn (its trigger appears tied to the
shim's launch window-station / desktop session; see handoff Next#3). Kept honest on purpose.

Headless (stdlib only; no window / pywebview backend). Run from the repo root:

    python3 -m unittest client.popup.tests.test_detach_show_state
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from client.popup import native_shell


class _FakeWin:
    def __init__(
        self, raise_on_show=False, raise_on_maximize=False, raise_on_restore=False,
        maximize_started=None, maximize_release=None,
    ):
        self.show_calls = 0
        self.maximize_calls = 0
        self.restore_calls = 0
        self.move_calls = []
        self.resize_calls = []
        self.x = 10
        self.y = 20
        self.width = 900
        self.height = 640
        self.evaluated = []
        self._raise_show = raise_on_show
        self._raise_maximize = raise_on_maximize
        self._raise_restore = raise_on_restore
        self._maximize_started = maximize_started
        self._maximize_release = maximize_release

    def show(self):
        self.show_calls += 1
        if self._raise_show:
            raise RuntimeError("backend not ready")

    def maximize(self):
        self.maximize_calls += 1
        if self._maximize_started is not None:
            self._maximize_started.set()
        if self._maximize_release is not None:
            self._maximize_release.wait(timeout=2)
        if self._raise_maximize:
            raise RuntimeError("maximize failed")

    def restore(self):
        self.restore_calls += 1
        if self._raise_restore:
            raise RuntimeError("restore failed")

    def move(self, x, y):
        self.move_calls.append((x, y))

    def resize(self, width, height):
        self.resize_calls.append((width, height))

    def evaluate_js(self, script):
        self.evaluated.append(script)
        if script == native_shell._DIAGRAM_LAYOUT_RECOVERY_JS:
            return "not-applicable"
        return "enhanced"


class _EventHook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class WinAfterShow(unittest.TestCase):
    def test_reissues_show(self):
        win = _FakeWin()
        native_shell._win_after_show(win)
        self.assertEqual(win.show_calls, 1, "the remedy must re-issue Window.show() on the popup")

    def test_show_failure_is_swallowed(self):
        # best-effort: a re-show that raises must NOT propagate (it would crash the detached popup).
        # If _win_after_show let the RuntimeError escape, this call raises and unittest flags an error.
        win = _FakeWin(raise_on_show=True)
        native_shell._win_after_show(win)
        self.assertEqual(win.show_calls, 1)

    def test_linux_popup_marks_ready_from_shown_instead_of_loaded(self):
        class StopAfterShown(Exception):
            pass

        win = _FakeWin()
        win.events = SimpleNamespace(
            shown=_EventHook(),
            loaded=_EventHook(),
            maximized=_EventHook(),
            restored=_EventHook(),
        )

        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return win

            @staticmethod
            def start():
                self.assertEqual(len(win.events.shown.handlers), 1)
                self.assertEqual(win.events.loaded.handlers, [])
                win.events.shown.handlers[0]()
                raise StopAfterShown()

        with mock.patch.dict(sys.modules, {"webview": FakeWebview}), \
                mock.patch.object(native_shell.sys, "platform", "linux"), \
                mock.patch.object(native_shell, "_IS_MAC", False), \
                mock.patch.object(native_shell, "_IS_WINDOWS", False), \
                mock.patch.object(native_shell, "_claim_app_identity"), \
                mock.patch.object(native_shell, "_wire_window_chrome"), \
                mock.patch.object(native_shell, "_mark_popup_shown_ready") as mark_shown:
            with self.assertRaises(StopAfterShown):
                native_shell.open_window(
                    "popup.html", "Popup", "result.json", ready_path="ready.json"
                )

        mark_shown.assert_called_once_with("ready.json")

    def test_windows_chrome_uses_stable_contract_and_native_drag(self):
        win = _FakeWin()
        native_shell._install_windows_chrome(win)
        self.assertEqual(len(win.evaluated), 1)
        script = win.evaluated[0]
        self.assertIn("header.pywebview-drag-region", script)
        self.assertIn("getElementById('hide-btn')", script)
        self.assertIn("getElementById('close-btn')", script)
        self.assertIn("api.move_window(", script)
        self.assertIn("event.screenX - dragOffset.x", script)
        self.assertIn("result.dragging === false", script)
        self.assertIn("moveInFlight", script)
        self.assertIn("closest(INTERACTIVE)", script)
        self.assertIn("event.button !== 0", script)

    def test_windows_chrome_script_executes_drag_and_interactive_contract(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable for the executable DOM shim test")
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(0, 'utf8');
const listeners = {};
const moves = [];
const moveAttempts = [];
const moveResolvers = [];
const inserted = [];
const insertedNodes = [];
let throwOnce = true;
const closeBtn = {id: 'close-btn'};
const hideBtn = {id: 'hide-btn'};
const header = {
  dataset: {},
  contains: (node) => node === closeBtn || node === hideBtn,
  addEventListener: (name, fn) => { listeners['header:' + name] = fn; },
  insertBefore: (node, before) => {
    inserted.push([node.id, before && before.id]);
    insertedNodes.push(node);
  },
};
global.document = {
  querySelector: (selector) => selector === 'header.pywebview-drag-region' ? header : null,
  getElementById: (id) => id === 'close-btn' ? closeBtn : id === 'hide-btn' ? hideBtn : null,
  createElement: () => ({style: {}, setAttribute() {}}),
};
global.window = {
  pywebview: {api: {
    move_window: (x, y) => {
      moveAttempts.push([x, y]);
      if (throwOnce) {
        throwOnce = false;
        throw new Error('synchronous bridge failure');
      }
      moves.push([x, y]);
      return new Promise((resolve) => moveResolvers.push(resolve));
    },
    toggle_maximize: () => Promise.resolve({ok: true, maximized: true}),
  }},
  addEventListener: (name, fn) => { listeners['window:' + name] = fn; },
  removeEventListener: (name, fn) => {
    if (listeners['window:' + name] === fn) delete listeners['window:' + name];
  },
  devicePixelRatio: 1.5,
};
(async () => {
  const outcome = eval(source);
  const interactiveEvent = {
    button: 0,
    target: {closest: () => closeBtn},
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  listeners['header:mousedown'](interactiveEvent);
  const rightClickEvent = {
    button: 2,
    target: {closest: () => null},
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  listeners['header:mousedown'](rightClickEvent);
  const rightClickArmed = !!listeners['window:mousemove'];
  const dragEvent = {
    button: 0, clientX: 12, clientY: 8,
    target: {closest: () => null},
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  listeners['header:mousedown'](dragEvent);
  listeners['window:mousemove']({screenX: 102, screenY: 48});
  await new Promise(setImmediate);
  listeners['window:mousemove']({screenX: 112, screenY: 58});
  listeners['window:mousemove']({screenX: 122, screenY: 68});
  listeners['window:mousemove']({screenX: 132, screenY: 78});
  await Promise.resolve();
  const beforeFirstResolve = moves.slice();
  moveResolvers.shift()({dragging: true});
  await new Promise(setImmediate);
  const afterFirstResolve = moves.slice();
  moveResolvers.shift()({dragging: false});
  await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({
    outcome, moves, moveAttempts, beforeFirstResolve, afterFirstResolve,
    interactiveStopped: !!interactiveEvent.stopped,
    rightClickArmed,
    dragPrevented: !!dragEvent.prevented,
    dragListenerRemoved: !listeners['window:mousemove'],
  }));
})();
"""
        completed = subprocess.run(
            [node, "-e", harness], input=native_shell._WINDOWS_DRAG_JS,
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=5,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["outcome"], "enhanced")
        self.assertEqual(result["beforeFirstResolve"], [[100, 50]])
        self.assertEqual(result["afterFirstResolve"], [[100, 50], [120, 70]])
        self.assertEqual(result["moves"], [[100, 50], [120, 70]])
        self.assertEqual(result["moveAttempts"], [[90, 40], [100, 50], [120, 70]])
        self.assertFalse(result["interactiveStopped"])
        self.assertFalse(result["rightClickArmed"])
        self.assertTrue(result["dragPrevented"])
        self.assertTrue(result["dragListenerRemoved"])

    def test_windows_chrome_injection_failure_is_swallowed(self):
        win = _FakeWin()
        win.evaluate_js = mock.Mock(side_effect=RuntimeError("page gone"))
        native_shell._install_windows_chrome(win)
        win.evaluate_js.assert_called_once()

    def test_windows_chrome_missing_contract_is_reported(self):
        win = _FakeWin()
        win.evaluate_js = mock.Mock(return_value="missing-chrome")
        with mock.patch("sys.stderr") as stderr:
            native_shell._install_windows_chrome(win)
        self.assertTrue(stderr.write.called)


class SharedWindowChrome(unittest.TestCase):
    def test_shared_chrome_wiring_is_platform_neutral(self):
        win = _FakeWin()
        win.events = SimpleNamespace(
            loaded=_EventHook(),
            maximized=_EventHook(),
            restored=_EventHook(),
        )
        api = native_shell.PopupApi("unused")

        native_shell._wire_window_chrome(win, api)

        self.assertEqual(win.events.maximized.handlers, [api._on_window_maximized])
        self.assertEqual(win.events.restored.handlers, [api._on_window_restored])
        self.assertEqual(len(win.events.loaded.handlers), 2)
        # Pin the shell locale so the injected chrome JS is deterministic regardless of the machine's
        # system language or which earlier test last set the module global via open_window.
        with mock.patch.object(native_shell, "_SHELL_LOCALE", "en-US"):
            win.events.loaded.handlers[0]()
            win.events.loaded.handlers[1]()
        expected_chrome = native_shell.launcher._inject_js_strings(
            native_shell._WINDOW_CHROME_JS, "__SHELL_STRINGS__",
            native_shell.i18n.shell("en-US"))
        self.assertEqual(
            win.evaluated,
            [
                expected_chrome,
                native_shell._DIAGRAM_LAYOUT_RECOVERY_JS,
            ],
        )

    def test_script_inserts_middle_maximize_and_eight_resize_handles(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable for the executable DOM shim test")
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(0, 'utf8');
const listeners = {};
const inserted = [];
const bodyChildren = [];
const resizeCalls = [];
const styleNodes = [];
const byId = {};
function element(tag) {
  return {
    tag, id: '', className: '', dataset: {}, style: {}, children: [],
    classList: {
      values: [],
      add(...xs) { this.values.push(...xs); },
      toggle(x, on) {
        this.values = this.values.filter((v) => v !== x);
        if (on) this.values.push(x);
      },
    },
    setAttribute() {},
    appendChild(child) { this.children.push(child); },
    addEventListener(name, fn) { listeners[(this.dataset.edge || this.id) + ':' + name] = fn; },
  };
}
const hideBtn = element('button'); hideBtn.id = 'hide-btn';
const closeBtn = element('button'); closeBtn.id = 'close-btn';
const header = {
  contains: (node) => node === hideBtn || node === closeBtn || inserted.includes(node),
  insertBefore: (node, before) => { inserted.push(node); byId[node.id] = node; node.before = before; },
};
global.document = {
  head: {appendChild: (node) => styleNodes.push(node)},
  body: {appendChild: (node) => bodyChildren.push(node)},
  querySelector: (selector) => selector === 'header.pywebview-drag-region' ? header : null,
  getElementById: (id) => byId[id] || (id === 'hide-btn' ? hideBtn : id === 'close-btn' ? closeBtn : null),
  createElement: element,
};
global.window = {
  screenX: 10, screenY: 20, outerWidth: 900, outerHeight: 640,
  pywebview: {api: {
    resize_window: (...args) => { resizeCalls.push(args); return Promise.resolve({ok: true}); },
    toggle_maximize: () => Promise.resolve({ok: true, maximized: false}),
    window_state: () => Promise.resolve({ok: true, maximized: true}),
  }},
  addEventListener: (name, fn) => { listeners['window:' + name] = fn; },
  removeEventListener: (name, fn) => {
    if (listeners['window:' + name] === fn) delete listeners['window:' + name];
  },
};
(async () => {
  const outcome = eval(source);
  const se = bodyChildren.find((node) => node.dataset.edge === 'se');
  const down = {button: 0, screenX: 100, screenY: 100, preventDefault() {}, stopPropagation() {}};
  listeners['se:mousedown'](down);
  listeners['window:mousemove']({screenX: 140, screenY: 160, buttons: 1});
  await new Promise(setImmediate);
  const initiallyMaximized = byId['maximize-btn'].classList.values.includes('is-maximized');
  listeners['window:mousemove']({screenX: 150, screenY: 170, buttons: 0});
  const stoppedAfterButtonRelease = !listeners['window:mousemove'];
  byId['maximize-btn'].onclick();
  await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({
    outcome,
    controlBefore: byId['maximize-btn'].before && byId['maximize-btn'].before.id,
    controls: [hideBtn, byId['maximize-btn'], closeBtn].map((n) => n.id),
    sharedControlClass: [hideBtn, byId['maximize-btn'], closeBtn]
      .every((n) => n.classList.values.includes('de-window-control')),
    handleCount: bodyChildren.filter((node) => node.className.includes('de-resize-handle')).length,
    resizeCalls,
    maximizeUsesCssState: byId['maximize-btn'].classList.values.includes('is-maximized'),
    initiallyMaximized,
    stoppedAfterButtonRelease,
    styleHasControlGeometry: styleNodes.some((n) => n.textContent.includes('.de-window-control')),
    controlsStackAboveHandles: styleNodes.some((n) => {
      const controls = [...n.textContent.matchAll(/\.de-window-control\s*\{[^}]*z-index:\s*(\d+)\s*(!important)?/g)];
      const handles = [...n.textContent.matchAll(/\.de-resize-handle\s*\{[^}]*z-index:\s*(\d+)\s*(!important)?/g)];
      return controls.length === 1 && handles.length === 1 &&
        controls[0][2] === '!important' &&
        Number(controls[0][1]) > Number(handles[0][1]);
    }),
  }));
})();
"""
        # Production injects the resolved-locale tooltip dict before eval; the raw constant carries
        # the __SHELL_STRINGS__ sentinel (an undefined identifier that would ReferenceError under node).
        injected = native_shell.launcher._inject_js_strings(
            native_shell._WINDOW_CHROME_JS, "__SHELL_STRINGS__",
            native_shell.i18n.shell("en-US"))
        completed = subprocess.run(
            [node, "-e", harness], input=injected,
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=5,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["outcome"], "enhanced")
        self.assertEqual(result["controlBefore"], "close-btn")
        self.assertEqual(result["controls"], ["hide-btn", "maximize-btn", "close-btn"])
        self.assertTrue(result["sharedControlClass"])
        self.assertEqual(result["handleCount"], 8)
        self.assertEqual(result["resizeCalls"], [[1, "se", 40, 60]])
        self.assertFalse(result["maximizeUsesCssState"])
        self.assertTrue(result["initiallyMaximized"])
        self.assertTrue(result["stoppedAfterButtonRelease"])
        self.assertTrue(result["styleHasControlGeometry"])
        self.assertTrue(result["controlsStackAboveHandles"])


class DiagramLayoutRecovery(unittest.TestCase):
    def test_loaded_bridge_retries_a_bannered_diagram_once(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable for the executable recovery test")
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(0, 'utf8');
global.window = {pywebview: {api: {layout: () => Promise.resolve({})}}};
async function runScenario(initiallyBannered) {
  let clicks = 0;
  let error = initiallyBannered ? {className: 'gv-error'} : null;
  let observerCallback = null;
  const reset = {click: () => { clicks += 1; }};
  const boardArea = {};
  global.document = {
    getElementById: (id) => id === 'reset-view' ? reset : id === 'board-area' ? boardArea : null,
    querySelector: (selector) => selector === '.gv-error' ? error : null,
  };
  global.MutationObserver = class {
    constructor(callback) { observerCallback = callback; }
    observe() {}
    disconnect() {}
  };
  const outcome = eval(source);
  if (!initiallyBannered) {
    error = {className: 'gv-error'};
    observerCallback();
  }
  await new Promise((resolve) => setTimeout(resolve, 20));
  return {outcome, clicks};
}
(async () => {
  process.stdout.write(JSON.stringify({
    existingBanner: await runScenario(true),
    lateBanner: await runScenario(false),
  }));
})();
"""
        completed = subprocess.run(
            [node, "-e", harness],
            input=native_shell._DIAGRAM_LAYOUT_RECOVERY_JS,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=5,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["existingBanner"], {"outcome": "armed", "clicks": 1})
        self.assertEqual(result["lateBanner"], {"outcome": "armed", "clicks": 1})


class PopupWindowControls(unittest.TestCase):
    def test_window_state_exposes_only_current_maximize_state(self):
        api = native_shell.PopupApi("unused")
        self.assertEqual(api.window_state(), {"ok": False, "maximized": False})
        api._win = _FakeWin()
        with mock.patch.object(
            native_shell, "_windows_window_maximized", return_value=True
        ):
            self.assertEqual(api.window_state(), {"ok": True, "maximized": True})

    def test_native_maximize_events_synchronize_css_control(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._on_window_maximized()
        api._on_window_restored()
        self.assertEqual(
            api._win.evaluated,
            [
                "window.deWindowChrome&&window.deWindowChrome.setMaximized(true)",
                "window.deWindowChrome&&window.deWindowChrome.setMaximized(false)",
            ],
        )

    def test_toggle_maximize_tracks_native_events_and_restores(self):
        win = _FakeWin()
        api = native_shell.PopupApi("unused")
        api._win = win

        first = api.toggle_maximize()
        self.assertEqual(first, {"ok": True, "maximized": True})
        self.assertEqual(win.maximize_calls, 1)
        api._on_window_maximized()

        second = api.toggle_maximize()
        self.assertEqual(second, {"ok": True, "maximized": False})
        self.assertEqual(win.restore_calls, 1)
        api._on_window_restored()

        third = api.toggle_maximize()
        self.assertEqual(third, {"ok": True, "maximized": True})
        self.assertEqual(win.maximize_calls, 2)

    def test_failed_maximize_does_not_flip_state(self):
        win = _FakeWin(raise_on_maximize=True)
        api = native_shell.PopupApi("unused")
        api._win = win

        self.assertEqual(api.toggle_maximize(), {"ok": False})
        win._raise_maximize = False
        self.assertEqual(api.toggle_maximize(), {"ok": True, "maximized": True})
        self.assertEqual(win.maximize_calls, 2)

    def test_concurrent_maximize_toggles_are_serialized(self):
        started = threading.Event()
        release = threading.Event()
        win = _FakeWin(maximize_started=started, maximize_release=release)
        api = native_shell.PopupApi("unused")
        api._win = win
        results = []

        first = threading.Thread(target=lambda: results.append(api.toggle_maximize()))
        second = threading.Thread(target=lambda: results.append(api.toggle_maximize()))
        first.start()
        self.assertTrue(started.wait(timeout=1))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertCountEqual(
            results,
            [{"ok": True, "maximized": True}, {"ok": True, "maximized": False}],
        )
        self.assertEqual(win.maximize_calls, 1)
        self.assertEqual(win.restore_calls, 1)

    def test_window_move_is_windows_only_bounded_and_delegated(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._win.move = mock.Mock()
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch.object(native_shell, "_windows_primary_button_down", return_value=True), \
             mock.patch.object(native_shell, "_windows_window_maximized", return_value=False):
            self.assertEqual(api.move_window(120.4, -30.7), {"ok": True})
            self.assertEqual(api.move_window(120.4, -30.7), {"ok": True})
            self.assertEqual(api.move_window(float("inf"), 0), {"ok": False})
            self.assertEqual(api.move_window(True, 0), {"ok": False})
        self.assertEqual(api._win.move.call_count, 2)
        api._win.move.assert_called_with(120, -31)

    def test_window_resize_is_cross_platform_bounded_and_delegated(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        with mock.patch.object(native_shell, "_IS_WINDOWS", False), \
             mock.patch.object(native_shell, "_windows_window_maximized", return_value=None):
            self.assertEqual(
                api.resize_window(1, "se", 100.2, 60.8),
                {"ok": True, "dragging": True},
            )
            self.assertEqual(
                api.resize_window(2, "nw", 700, 600),
                {"ok": True, "dragging": True},
            )
            self.assertEqual(
                api.resize_window(3, "e", float("inf"), 0),
                {"ok": False, "dragging": False},
            )
            self.assertEqual(
                api.resize_window(4, "e", True, 0),
                {"ok": False, "dragging": False},
            )
        self.assertEqual(api._win.resize_calls, [(1000, 701), (480, 360)])
        self.assertEqual(api._win.move_calls, [(430, 300)])

    def test_window_resize_stops_on_windows_button_release_and_while_maximized(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch.object(native_shell, "_windows_primary_button_down", return_value=False):
            self.assertEqual(
                api.resize_window(1, "se", 40, 60),
                {"ok": False, "dragging": False},
            )
        api._on_window_maximized()
        with mock.patch.object(native_shell, "_IS_WINDOWS", False), \
             mock.patch.object(native_shell, "_windows_window_maximized", return_value=None):
            self.assertEqual(
                api.resize_window(1, "se", 40, 60),
                {"ok": False, "dragging": False},
            )
        self.assertEqual(api._win.resize_calls, [])
        self.assertEqual(api._win.move_calls, [])

    def test_shared_chrome_declares_resize_and_middle_maximize_contract(self):
        script = native_shell._WINDOW_CHROME_JS
        self.assertIn("api.resize_window(", script)
        self.assertIn("de-resize-handle", script)
        self.assertIn("de-window-control", script)
        self.assertIn("controlHost.insertBefore(maximizeBtn, closeBtn)", script)
        self.assertNotIn("maximizeBtn.textContent", script)

    def test_shared_chrome_resets_legacy_button_margin_for_container_gap(self):
        control_rule = native_shell._WINDOW_CHROME_JS.split(".de-window-control{", 1)[1].split("}", 1)[0]
        self.assertIn("margin-left:0!important", control_rule)

    def test_window_move_stops_when_windows_reports_button_released(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._win.move = mock.Mock()
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch.object(native_shell, "_windows_primary_button_down", return_value=False):
            self.assertEqual(api.move_window(120, 30), {"ok": False, "dragging": False})
        api._win.move.assert_not_called()

    def test_window_move_stops_while_maximized(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._win.move = mock.Mock()
        api._on_window_maximized()
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch.object(native_shell, "_windows_primary_button_down", return_value=True), \
             mock.patch.object(native_shell, "_windows_window_maximized", return_value=None):
            self.assertEqual(api.move_window(120, 30), {"ok": False, "dragging": False})
        api._win.move.assert_not_called()

    def test_windows_primary_button_honors_swapped_mouse_setting(self):
        user32 = mock.Mock()
        user32.GetSystemMetrics.return_value = 1
        user32.GetAsyncKeyState.return_value = 0x8000
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch("ctypes.windll", SimpleNamespace(user32=user32), create=True):
            self.assertTrue(native_shell._windows_primary_button_down())
        user32.GetAsyncKeyState.assert_called_once_with(0x02)

    def test_live_windows_zoom_state_overrides_stale_event_state(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._on_window_maximized()
        with mock.patch.object(
            native_shell, "_windows_window_maximized", return_value=False,
        ):
            self.assertEqual(api.toggle_maximize(), {"ok": True, "maximized": True})
        self.assertEqual(api._win.maximize_calls, 1)
        self.assertEqual(api._win.restore_calls, 0)

    def test_window_guards_fail_closed_off_windows_and_on_backend_error(self):
        api = native_shell.PopupApi("unused")
        api._win = _FakeWin()
        api._win.move = mock.Mock(side_effect=RuntimeError("backend failed"))
        with mock.patch.object(native_shell, "_IS_WINDOWS", False):
            self.assertEqual(api.move_window(1, 2), {"ok": False})
            self.assertFalse(native_shell._windows_primary_button_down())
            self.assertIsNone(native_shell._windows_window_maximized(None))
        api._win.move.assert_not_called()

        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch.object(native_shell, "_windows_primary_button_down", return_value=True), \
             mock.patch.object(native_shell, "_windows_window_maximized", return_value=False):
            self.assertEqual(
                api.move_window(1, 2),
                {"ok": False, "dragging": False},
            )

    def test_windows_window_maximized_calls_is_zoomed_with_pointer_width(self):
        handle = mock.Mock()
        handle.ToInt64.return_value = 0x123456789ABC
        win = SimpleNamespace(native=SimpleNamespace(Handle=handle))
        user32 = mock.Mock()
        user32.IsZoomed.return_value = 1
        with mock.patch.object(native_shell, "_IS_WINDOWS", True), \
             mock.patch("ctypes.windll", SimpleNamespace(user32=user32), create=True):
            self.assertTrue(native_shell._windows_window_maximized(win))
        passed_handle = user32.IsZoomed.call_args.args[0]
        self.assertEqual(passed_handle.value, 0x123456789ABC)
        handle.ToInt32.assert_not_called()


class MacStatusItemLifecycle(unittest.TestCase):
    class _StopAfterLoaded(Exception):
        pass

    def test_status_item_is_created_after_pywebview_loaded_not_before_create_window(self):
        calls = []
        win = _FakeWin()
        win.events = SimpleNamespace(
            loaded=_EventHook(),
            maximized=_EventHook(),
            restored=_EventHook(),
        )

        class _FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                calls.append("create_window")
                return win

            @staticmethod
            def start():
                calls.append("webview.start")
                for handler in list(win.events.loaded.handlers):
                    handler()
                raise MacStatusItemLifecycle._StopAfterLoaded()

        with mock.patch.dict(sys.modules, {"webview": _FakeWebview}), \
             mock.patch.object(native_shell, "_IS_MAC", True), \
             mock.patch.object(native_shell, "_IS_WINDOWS", False), \
             mock.patch.object(native_shell, "_mac_visible_frame", return_value=None), \
             mock.patch.object(native_shell, "_claim_app_identity", side_effect=lambda: calls.append("claim_identity")), \
             mock.patch.object(native_shell, "_claim_dock_app_name", side_effect=lambda _title: calls.append("claim_dock_name")), \
             mock.patch.object(native_shell, "_install_dock_icon", side_effect=lambda _title: calls.append("dock_icon")), \
             mock.patch.object(native_shell, "_mac_after_show", side_effect=lambda: calls.append("mac_after_show")), \
             mock.patch.object(native_shell, "_install_dock_reopen", side_effect=lambda _win: calls.append("dock_reopen")), \
             mock.patch.object(native_shell, "_run_on_mac_main_queue", side_effect=lambda callback: calls.append("schedule_status") or callback()), \
             mock.patch.object(native_shell, "_apply_mac_chrome", side_effect=lambda _title: calls.append("apply_status") or object()), \
             mock.patch.object(native_shell, "_wire_status_toggle", side_effect=lambda _item, _win: calls.append("wire_status")):
            with self.assertRaises(MacStatusItemLifecycle._StopAfterLoaded):
                native_shell.open_window("popup.html", "Popup", "result.json")

        self.assertIn("apply_status", calls)
        self.assertLess(calls.index("claim_dock_name"), calls.index("create_window"))
        self.assertLess(calls.index("create_window"), calls.index("apply_status"))
        self.assertLess(calls.index("webview.start"), calls.index("apply_status"))
        self.assertLess(calls.index("apply_status"), calls.index("wire_status"))


class PopupWindowLevelTests(unittest.TestCase):
    """The content popup — 图解 / 漫解 / 信息图 / 讨论板 / 白板 are all THIS one window — is not
    pinned above the caller (Owner, 2026-09-16). It still opens focused and in front; it just stops
    winning every raise after that. The audit stop panel is the opposite case and lives in
    client/stopper/panel.py.
    """

    class _StopAfterCreate(Exception):
        pass

    @staticmethod
    def _fake_appkit_window():
        return SimpleNamespace(
            makeKeyAndOrderFront_=mock.Mock(),
            setLevel_=mock.Mock(),
        )

    @staticmethod
    def _fake_frameworks(ns_app):
        """AppKit without a floating-level constant: importing one is itself the regression."""
        app_kit = SimpleNamespace(NSApp=ns_app)
        inline_queue = SimpleNamespace(
            mainQueue=lambda: SimpleNamespace(
                addOperationWithBlock_=lambda callback: callback(),
            ),
        )
        return {"AppKit": app_kit, "Foundation": SimpleNamespace(NSOperationQueue=inline_queue)}

    def test_window_is_created_without_on_top_and_keeps_the_visible_frame(self):
        created = {}

        class _FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                created.update(kwargs)
                raise PopupWindowLevelTests._StopAfterCreate()

        with mock.patch.dict(sys.modules, {"webview": _FakeWebview}), \
             mock.patch.object(native_shell, "_IS_MAC", True), \
             mock.patch.object(native_shell, "_IS_WINDOWS", False), \
             mock.patch.object(native_shell, "_mac_visible_frame", return_value=(0, 25, 1440, 875)), \
             mock.patch.object(native_shell, "_claim_app_identity"), \
             mock.patch.object(native_shell, "_claim_dock_app_name"):
            with self.assertRaises(PopupWindowLevelTests._StopAfterCreate):
                native_shell.open_window("popup.html", "Popup", "result.json")

        self.assertNotIn("on_top", created)
        self.assertEqual(
            (created["x"], created["y"], created["width"], created["height"]),
            (0, 25, 1440, 875),
        )

    def test_opening_focuses_the_popup_without_setting_a_window_level(self):
        ns_app = SimpleNamespace(activateIgnoringOtherApps_=mock.Mock())
        window = self._fake_appkit_window()
        with mock.patch.dict(sys.modules, self._fake_frameworks(ns_app)), \
             mock.patch.object(native_shell, "_content_nswindow", return_value=window):
            native_shell._mac_after_show()

        ns_app.activateIgnoringOtherApps_.assert_called_once_with(True)
        window.makeKeyAndOrderFront_.assert_called_once_with(None)
        window.setLevel_.assert_not_called()

    def test_recall_focuses_the_popup_without_setting_a_window_level(self):
        ns_app = SimpleNamespace(activateIgnoringOtherApps_=mock.Mock())
        window = self._fake_appkit_window()
        win = _FakeWin()
        with mock.patch.dict(sys.modules, self._fake_frameworks(ns_app)), \
             mock.patch.object(native_shell, "_content_nswindow", return_value=window):
            native_shell._show_and_focus(win)

        self.assertEqual(win.show_calls, 1)
        ns_app.activateIgnoringOtherApps_.assert_called_once_with(True)
        window.makeKeyAndOrderFront_.assert_called_once_with(None)
        window.setLevel_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
