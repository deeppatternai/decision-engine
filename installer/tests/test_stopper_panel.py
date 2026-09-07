"""Unit tests for the cross-platform tkinter stop panel (`client.stopper.panel`).

Covers what is testable WITHOUT a display / tkinter (this CI host, and many dev boxes, have no
`_tkinter`) and WITHOUT a real Windows host: the pure render helpers, the poll-merge logic, and
the detached-launch wiring shape. The actual tkinter rendering + real-Windows spawn/liveness are
covered by the Owner self-test checklist in the PR (the panel imports GUI-free by design, so
these tests run anywhere).

Run:  python3 -m unittest installer.tests.test_stopper_panel
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from unittest import mock

from client import i18n, runner
from client.stopper import panel


class _FakeWidget:
    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = dict(options)
        self.dpi = getattr(parent, "dpi", 96.0)
        self.dpi_error = None
        self.children = []
        self.packed_children = []
        self.bindings = {}
        self.destroyed = False
        self.focused = False
        self.value = ""
        self.insert_calls = 0
        if parent is not None:
            parent.children.append(self)

    def pack(self, **options):
        if self.parent is None:
            return
        packed = self.parent.packed_children
        if self in packed:
            packed.remove(self)
        if "before" in options:
            packed.insert(packed.index(options["before"]), self)
        elif "after" in options:
            packed.insert(packed.index(options["after"]) + 1, self)
        else:
            packed.append(self)

    pack_configure = pack

    def place(self, **options):
        self.options["place"] = dict(options)

    place_configure = place

    def pack_propagate(self, value):
        self.options["pack_propagate"] = value

    def pack_forget(self):
        if self.parent is not None and self in self.parent.packed_children:
            self.parent.packed_children.remove(self)

    def pack_slaves(self):
        return list(self.packed_children)

    def winfo_children(self):
        return list(self.children)

    def configure(self, **options):
        self.options.update(options)

    def cget(self, name):
        return self.options.get(name)

    def bind(self, event, callback):
        self.bindings[event] = callback

    def focus_set(self):
        self.focused = True

    def winfo_fpixels(self, _distance):
        if self.dpi_error is not None:
            raise self.dpi_error("dpi unavailable")
        return self.dpi

    def delete(self, _first, _last=None):
        self.value = ""

    def insert(self, _index, value):
        self.insert_calls += 1
        self.value = str(value)

    def get(self):
        return self.value

    def invoke(self):
        command = self.options.get("command")
        if self.options.get("state") != "disabled" and callable(command):
            return command()

    def destroy(self):
        self.destroyed = True
        for child in list(self.children):
            child.destroy()
        if self.parent is not None and self in self.parent.packed_children:
            self.parent.packed_children.remove(self)
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)


class _FakeCanvas(_FakeWidget):
    text_widths = {}

    def __init__(self, parent=None, **options):
        super().__init__(parent, **options)
        self.items = []

    def _create_item(self, kind, coordinates, options):
        item_id = len(self.items) + 1
        self.items.append({
            "id": item_id, "kind": kind, "coordinates": coordinates, "options": dict(options),
        })
        return item_id

    def create_polygon(self, *coordinates, **options):
        return self._create_item("polygon", coordinates, options)

    def create_rectangle(self, *coordinates, **options):
        return self._create_item("rectangle", coordinates, options)

    def create_text(self, *coordinates, **options):
        return self._create_item("text", coordinates, options)

    def itemconfigure(self, item_id, **options):
        self.items[item_id - 1]["options"].update(options)

    def itemcget(self, item_id, option):
        return self.items[item_id - 1]["options"].get(option)

    def coords(self, item_id, *coordinates):
        self.items[item_id - 1]["coordinates"] = coordinates

    def bbox(self, item_id):
        item = self.items[item_id - 1]
        if item["kind"] != "text":
            return None
        text = item["options"].get("text", "")
        configured_width = self.text_widths.get(text)
        if configured_width is None:
            width = len(text) * 7
        else:
            width = configured_width * getattr(self.parent, "dpi", 96.0) / 96.0
        x, y = item["coordinates"]
        left = x if item["options"].get("anchor") == "w" else x - width / 2
        return (round(left), round(y - 7), round(left + width), round(y + 7))

class _FakeTk:
    class TclError(Exception):
        pass

    Frame = _FakeWidget
    Button = _FakeWidget
    Canvas = _FakeCanvas
    Label = _FakeWidget
    Entry = _FakeWidget


class RenderReconciliationTests(unittest.TestCase):
    def _app(self):
        app = object.__new__(panel.StopPanelApp)
        app._tk = _FakeTk
        app.body = _FakeWidget()
        app.runs = {
            "registry-real": {
                "run_id": "payload-wrong",
                "title": "Specific audit",
                "profile": "fast",
                "ui_locale": "en-US",
                "status": "queued",
                "started_at": 1.0,
                "_sort_at": 1.0,
            }
        }
        app._frozen = {}
        app._retired = set()
        app._cancel_inflight = set()
        app._server_verified = {"registry-real"}
        app._state_epochs = {}
        app._lock = threading.Lock()
        app._rows = {}
        app._row_order = []
        app._empty_label = None
        return app

    def test_refresh_reuses_widgets_and_keeps_button_colors(self):
        app = self._app()
        with mock.patch.object(panel.time, "time", return_value=100.0):
            app._render()
        row = app.body.children[0]
        button = app._rows["registry-real"]["button"]

        with mock.patch.object(panel.time, "time", return_value=101.0):
            app._render()
        self.assertIs(app.body.children[0], row, "a timer refresh must not rebuild the row")
        self.assertEqual(button.cget("bg"), panel.FG_RED)
        self.assertEqual(button.cget("cursor"), "hand2")

        app.runs["registry-real"].update(
            status="completed", auditors=[{"status": "completed"}])
        with mock.patch.object(panel.time, "time", return_value=102.0):
            app._render()
        self.assertIs(app.body.children[0], row)
        self.assertEqual(button.cget("bg"), panel.BTN_FINISHED)
        self.assertEqual(button.cget("disabledforeground"), panel.FG_MUTED)
        self.assertEqual(button.cget("state"), "disabled")
        self.assertEqual(button.canvas.items[1]["options"]["state"], "hidden")
        self.assertEqual(button.canvas.items[2]["options"]["text"], "Finished")

        app.runs.clear()
        app._render()
        self.assertTrue(row.destroyed)
        self.assertEqual(len(app.body.children), 1, "the empty label must replace removed rows")

    def test_active_button_draws_one_plain_square_on_a_rounded_background(self):
        app = self._app()
        app._render()

        button = app._rows["registry-real"]["button"]
        self.assertEqual(button.cget("text"), "STOP", "platform-specific stop glyphs must not be text")
        self.assertNotIn("⏹", button.cget("text"))
        canvas = button.canvas
        polygons = [item for item in canvas.items if item["kind"] == "polygon"]
        squares = [item for item in canvas.items if item["kind"] == "rectangle"]
        labels = [item for item in canvas.items if item["kind"] == "text"]
        self.assertEqual(len(polygons), 1)
        self.assertTrue(polygons[0]["options"]["smooth"])
        self.assertEqual(polygons[0]["options"]["fill"], panel.FG_RED)
        self.assertEqual(len(squares), 1)
        self.assertEqual(squares[0]["options"]["fill"], "#FFFFFF")
        self.assertEqual(squares[0]["options"]["state"], "normal")
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["options"]["text"], "STOP")

    def test_button_keeps_native_semantics_and_disables_pointer_activation(self):
        app = self._app()
        app._render()
        button = app._rows["registry-real"]["button"]
        self.assertEqual(button.button.cget("text"), "STOP")
        self.assertEqual(button.button.cget("state"), "normal")
        self.assertFalse(button.canvas.cget("takefocus"))

        app._stop = mock.Mock()
        app.runs["registry-real"].update(
            status="completed", auditors=[{"status": "completed"}],
        )
        app._render()
        self.assertEqual(button.button.cget("state"), "disabled")
        button.canvas.bindings["<Button-1>"](None)
        button.canvas.bindings["<ButtonRelease-1>"](None)
        app._stop.assert_not_called()

    def test_running_row_is_visible_but_cannot_be_cancelled(self):
        app = self._app()
        app.runs["registry-real"]["status"] = "running"
        app._render()

        button = app._rows["registry-real"]["button"]
        self.assertEqual(button.button.cget("text"), "Running")
        self.assertEqual(button.button.cget("state"), "disabled")
        self.assertEqual(button.cget("bg"), panel.FG_RED)
        self.assertEqual(button.cget("disabledforeground"), "#FFFFFF")
        self.assertEqual(button.canvas.items[1]["options"]["state"], "hidden")

    def test_unverified_disk_seeded_queue_shows_checking_and_disables_cancel(self):
        app = self._app()
        app._server_verified.clear()
        app._render()

        button = app._rows["registry-real"]["button"]
        self.assertEqual(button.button.cget("text"), "Checking…")
        self.assertEqual(button.button.cget("state"), "disabled")
        self.assertEqual(button.canvas.items[1]["options"]["state"], "hidden")

    def test_mouse_drag_out_and_back_in_rearms_the_button(self):
        app = self._app()
        app._stop = mock.Mock()
        app._render()
        canvas = app._rows["registry-real"]["button"].canvas

        canvas.bindings["<Button-1>"](None)
        canvas.bindings["<Leave>"](None)
        canvas.bindings["<Enter>"](None)
        canvas.bindings["<ButtonRelease-1>"](None)

        app._stop.assert_called_once_with("registry-real")

    def test_hover_uses_active_color_and_leave_restores_base_color(self):
        app = self._app()
        app._render()
        button = app._rows["registry-real"]["button"]
        background = next(
            item for item in button.canvas.items if item["kind"] == "polygon"
        )

        button.canvas.bindings["<Enter>"](None)
        self.assertEqual(background["options"]["fill"], panel.BTN_RED_ACTIVE)
        button.canvas.bindings["<Leave>"](None)
        self.assertEqual(background["options"]["fill"], panel.FG_RED)

    def test_button_geometry_rescales_when_reported_dpi_changes(self):
        app = self._app()
        app.body.dpi = 144.0
        app._render()
        button = app._rows["registry-real"]["button"]
        self.assertEqual(button.canvas.cget("width"), 129)
        self.assertEqual(button.canvas.cget("height"), 51)

        button.frame.dpi = 192.0
        app._render()
        self.assertEqual(button.canvas.cget("width"), 172)
        self.assertEqual(button.canvas.cget("height"), 68)
        square = next(item for item in button.canvas.items if item["kind"] == "rectangle")
        left, top, right, bottom = square["coordinates"]
        self.assertEqual(right - left, 16)
        self.assertEqual(bottom - top, 16)

    def test_button_expands_to_fit_wide_windows_status_label(self):
        app = self._app()
        with mock.patch.object(_FakeCanvas, "text_widths", {"Cancelling…": 98}):
            app._render()
            button = app._rows["registry-real"]["button"]
            stable_width = button._width
            button.configure(
                text="Cancelling…", bg=panel.BTN_FINISHED, fg=panel.FG_MUTED,
                disabledforeground=panel.FG_MUTED, activebackground=panel.BTN_FINISHED,
                activeforeground=panel.FG_MUTED, state="disabled", cursor="", showicon=False,
            )
            left, _top, right, _bottom = button.canvas.bbox(button._label)

        self.assertGreaterEqual(button._width, 98 + 24)
        self.assertEqual(button._width, stable_width)
        self.assertGreaterEqual(left, 0)
        self.assertLessEqual(right, button._width)

    def test_wide_button_centers_stop_icon_and_label_as_one_group(self):
        app = self._app()
        with mock.patch.object(_FakeCanvas, "text_widths", {"Cancelling…": 98}):
            app._render()
            button = app._rows["registry-real"]["button"]
            for dpi in (96.0, 120.0, 144.0, 168.0, 192.0):
                button.frame.dpi = dpi
                button._sync_geometry()
                icon_left, _top, _right, _bottom = button.canvas.items[1]["coordinates"]
                _left, _top, label_right, _bottom = button.canvas.bbox(button._label)
                content_center = (icon_left + label_right) / 2
                padding = round(panel._ACTION_HORIZONTAL_PADDING * button._scale)
                self.assertGreater(button._width, round(panel._ACTION_WIDTH * button._scale))
                self.assertAlmostEqual(content_center, button._width / 2, delta=0.5)
                self.assertGreaterEqual(icon_left, padding)
                self.assertLessEqual(label_right + padding, button._width)

    def test_missing_text_bounds_falls_back_to_safe_left_padding(self):
        app = self._app()
        with mock.patch.object(_FakeCanvas, "bbox", return_value=None):
            app._render()
        button = app._rows["registry-real"]["button"]
        icon_left, _top, icon_right, _bottom = button.canvas.items[1]["coordinates"]
        label_x, _label_y = button.canvas.items[2]["coordinates"]

        self.assertEqual(
            icon_left, round(panel._ACTION_HORIZONTAL_PADDING * button._scale),
        )
        self.assertEqual(
            label_x,
            icon_right + round(panel._ACTION_ICON_GAP * button._scale),
        )

    def test_width_measurement_preserves_label_canvas_state(self):
        app = self._app()
        app._render()
        button = app._rows["registry-real"]["button"]
        button.canvas.itemconfigure(button._label, text="current", anchor="w")

        button._required_width(button._scale)

        self.assertEqual(button.canvas.itemcget(button._label, "text"), "current")
        self.assertEqual(button.canvas.itemcget(button._label, "anchor"), "w")

    def test_dpi_change_remeasures_without_corrupting_cancelling_state(self):
        app = self._app()
        with mock.patch.object(_FakeCanvas, "text_widths", {"Cancelling…": 98}):
            app._render()
            button = app._rows["registry-real"]["button"]
            button.configure(
                text="Cancelling…", bg=panel.BTN_FINISHED, fg=panel.FG_MUTED,
                disabledforeground=panel.FG_MUTED, activebackground=panel.BTN_FINISHED,
                activeforeground=panel.FG_MUTED, state="disabled", cursor="", showicon=False,
            )
            button.frame.dpi = 144.0
            button._sync_geometry()
            left, _top, right, _bottom = button.canvas.bbox(button._label)

        self.assertGreaterEqual(
            button._width,
            (right - left) + 2 * round(panel._ACTION_HORIZONTAL_PADDING * button._scale),
        )
        self.assertEqual(button.canvas.itemcget(button._label, "text"), "Cancelling…")
        self.assertEqual(button.canvas.itemcget(button._label, "anchor"), "center")
        self.assertEqual(button.canvas.items[1]["options"]["state"], "hidden")
        self.assertGreaterEqual(left, 0)
        self.assertLessEqual(right, button._width)

    def test_icon_label_width_keeps_centered_group_inside_horizontal_padding(self):
        app = self._app()
        with mock.patch.object(_FakeCanvas, "text_widths", {"STOP": 60}):
            app._render()
            button = app._rows["registry-real"]["button"]
            icon_left, _top, _right, _bottom = button.canvas.items[1]["coordinates"]
            left, _top, right, _bottom = button.canvas.bbox(button._label)

        self.assertEqual(button.canvas.itemcget(button._label, "anchor"), "w")
        self.assertGreaterEqual(
            icon_left, round(panel._ACTION_HORIZONTAL_PADDING * button._scale),
        )
        self.assertGreater(left, icon_left)
        self.assertLessEqual(
            right + round(panel._ACTION_HORIZONTAL_PADDING * button._scale),
            button._width,
        )

    def test_display_scale_falls_back_on_tcl_error(self):
        widget = _FakeWidget()
        widget.dpi_error = _FakeTk.TclError
        self.assertEqual(panel._display_scale(widget, _FakeTk), 1.0)

    def test_stop_uses_registry_key_not_payload_id(self):
        app = self._app()
        app._stop = mock.Mock()
        app._render()
        canvas = app._rows["registry-real"]["button"].canvas
        canvas.bindings["<Button-1>"](None)
        canvas.bindings["<ButtonRelease-1>"](None)
        app._stop.assert_called_once_with("registry-real")


    def test_audit_id_is_a_selectable_separate_line_using_registry_identity(self):
        app = self._app()
        app._render()

        widgets = app._rows["registry-real"]
        audit_id = widgets["audit_id"]
        self.assertEqual(widgets["title"].cget("text"), "Specific audit")
        # The ID column label is panel chrome resolved against the HOST locale, so assert against the
        # same source the code reads (not a hard-coded "ID") — this stays green on a zh-CN host too.
        self.assertEqual(
            widgets["audit_id_label"].cget("text"),
            i18n.panel(i18n.resolve_locale(None))["id_label"],
        )
        self.assertEqual(audit_id.get(), "registry-real")
        self.assertEqual(audit_id.cget("state"), "readonly")
        self.assertEqual(
            widgets["audit_id_row"].parent.pack_slaves(),
            [widgets["title"], widgets["audit_id_row"], widgets["detail"]],
            "the selectable audit ID must render on its own line below the title",
        )

    def test_audit_id_column_label_follows_the_host_locale(self):
        # Acceptance criterion: on a zh host the ID column reads「编号」, on an en host it reads "ID".
        # The label is fixed chrome (same for every row), so it tracks the HOST locale (DE_UI_LOCALE /
        # OS UI language), independent of any single run's ui_locale.
        for de_ui_locale, expected in (("zh-CN", "编号"), ("en-US", "ID")):
            with mock.patch.dict(os.environ, {"DE_UI_LOCALE": de_ui_locale}):
                app = self._app()
                app._render()
                self.assertEqual(
                    app._rows["registry-real"]["audit_id_label"].cget("text"), expected
                )

    def test_local_advisory_refresh_removes_a_previous_server_id_line(self):
        app = self._app()
        app._render()
        audit_id = app._rows["registry-real"]["audit_id"]
        audit_id_row = app._rows["registry-real"]["audit_id_row"]
        self.assertIn(audit_id_row, audit_id_row.parent.pack_slaves())

        app.runs["registry-real"]["local"] = True
        app._render()
        self.assertNotIn(audit_id_row, audit_id_row.parent.pack_slaves())

        app.runs["registry-real"]["local"] = False
        app._render()
        self.assertEqual(audit_id.get(), "registry-real")
        self.assertIn(audit_id_row, audit_id_row.parent.pack_slaves())

    def test_refresh_does_not_rewrite_an_unchanged_selectable_id(self):
        app = self._app()
        app._render()
        audit_id = app._rows["registry-real"]["audit_id"]
        self.assertEqual(audit_id.insert_calls, 1)

        app._render()
        self.assertEqual(
            audit_id.insert_calls, 1,
            "a timer refresh must preserve the user's active text selection",
        )

    def test_visible_order_is_active_then_newest_with_stable_ties(self):
        app = self._app()
        app.runs = {
            "b": {"run_id": "b", "status": "running", "_sort_at": 10.0},
            "a": {"run_id": "a", "status": "running", "_sort_at": 10.0},
            "new": {"run_id": "new", "status": "running", "_sort_at": 20.0},
            "done": {"run_id": "done", "status": "completed", "_sort_at": 30.0},
        }
        self.assertEqual(
            [key for key, _run in app._visible_runs()], ["new", "a", "b", "done"])

    def test_registry_key_drives_the_shared_active_predicate(self):
        self.assertTrue(panel._entry_is_active(
            "registry-real", {"run_id": "", "status": "running"}))
        self.assertFalse(panel._entry_is_active(
            "registry-real", {"run_id": "payload-wrong", "status": "completed"}))

    def test_rendered_order_stays_stable_across_poll_updates(self):
        app = self._app()
        app.runs["newer"] = {
            "run_id": "newer", "status": "running", "title": "Newer", "_sort_at": 2.0,
        }
        app._render()
        older_row = app._rows["registry-real"]["row"]
        newer_row = app._rows["newer"]["row"]
        self.assertEqual(app.body.pack_slaves(), [newer_row, older_row])

        app.runs["registry-real"].update(updated_at=999.0, started_at=999.0)
        app._render()
        self.assertEqual(app.body.pack_slaves(), [newer_row, older_row])
        self.assertIs(app._rows["registry-real"]["row"], older_row)
        self.assertIs(app._rows["newer"]["row"], newer_row)

    def test_disk_merge_uses_registry_identity_and_snapshots_sort_time(self):
        app = self._app()
        app.runs = {}
        first = {"runs": {
            "real-id": {"run_id": "wrong-id", "status": "queued", "created_at": 12.0,
                        "updated_at": 99.0},
        }}
        changed = {"runs": {
            "real-id": {"run_id": "another-wrong-id", "status": "queued",
                        "created_at": 500.0, "updated_at": 999.0},
        }}
        with mock.patch.object(
            runner, "load_active_runs_registry", side_effect=[first, changed],
        ):
            app._merge_disk()
            app._merge_disk()
        self.assertEqual(app.runs["real-id"]["run_id"], "real-id")
        self.assertEqual(app.runs["real-id"]["_sort_at"], 12.0)


class RealTkActionButtonSmokeTests(unittest.TestCase):
    def test_real_tk_accepts_draw_options_and_all_labels_fit(self):
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest("tkinter unavailable: %s" % exc)
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest("Tk display unavailable: %s" % exc)
        self.addCleanup(root.destroy)
        root.withdraw()
        host = tk.Frame(root, bg=panel.BG)
        host.pack()
        calls = []
        button = panel._RoundedActionButton(tk, host, command=lambda: calls.append("stop"))
        button.pack()

        states = (
            ("STOP", panel.FG_RED, "#FFFFFF", "normal", True),
            ("Cancelling…", panel.BTN_FINISHED, panel.FG_MUTED, "disabled", False),
            ("Cancelled", panel.BTN_FINISHED, panel.FG_AMBER, "disabled", False),
            ("Running", panel.FG_RED, "#FFFFFF", "disabled", False),
            ("Checking…", panel.BTN_FINISHED, panel.FG_MUTED, "disabled", False),
            ("Finished", panel.BTN_FINISHED, panel.FG_MUTED, "disabled", False),
        )
        for text, background, foreground, state, show_icon in states:
            button.configure(
                text=text, bg=background, fg=foreground, disabledforeground=foreground,
                activebackground=background, activeforeground=foreground,
                state=state, cursor=("hand2" if state == "normal" else ""),
                showicon=show_icon,
            )
            root.update_idletasks()
            self.assertEqual(button.button.cget("text"), text)
            self.assertEqual(button.button.cget("state"), state)
            left, top, right, bottom = button.canvas.bbox(button._label)
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, button._width)
            self.assertLessEqual(bottom, button._height)

        button.configure(
            text="STOP", bg=panel.FG_RED, fg="#FFFFFF", disabledforeground="#FFFFFF",
            activebackground=panel.BTN_RED_ACTIVE, activeforeground="#FFFFFF",
            state="normal", cursor="hand2", showicon=True,
        )
        root.update_idletasks()
        icon_left, _top, _right, _bottom = button.canvas.coords(button._icon)
        _left, _top, label_right, _bottom = button.canvas.bbox(button._label)
        self.assertAlmostEqual(
            (icon_left + label_right) / 2, button._width / 2, delta=0.5,
        )
        button.invoke()
        self.assertEqual(calls, ["stop"])


class RenderHelperTests(unittest.TestCase):
    NOW = 1_000_000.0

    def setUp(self):
        # The default locale path now consults the OS UI language; pin it OFF so "no locale → en-US"
        # assertions are deterministic on any host (this suite runs on zh-CN machines too). Explicit
        # `ui_locale` / `DE_UI_LOCALE` still win — they're resolved before system detection.
        patcher = mock.patch.object(i18n, "detect_system_locale", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_audit_id_text_strips_only_one_leading_prefix_and_stays_bounded(self):
        self.assertEqual(
            panel.audit_id_text({"run_id": "aud_v4fEQN3Nt39iDjSn"}),
            "v4fEQN3Nt39iDjSn",
        )
        self.assertEqual(panel.audit_id_text({"run_id": "aud_aud_nested"}), "aud_nested")
        self.assertEqual(panel.audit_id_text({"run_id": "aud_"}), "")
        self.assertEqual(panel.audit_id_text({"run_id": "local-x", "local": True}), "")
        self.assertEqual(panel.audit_id_text({"run_id": "aud_ok\nspoof"}), "")
        self.assertEqual(panel.audit_id_text({"run_id": "aud_real\n"}), "")
        self.assertEqual(panel.audit_id_text({"run_id": " aud_real"}), "")
        long_id = "aud_" + "a" * 124
        self.assertEqual(panel.audit_id_text({"run_id": long_id}), "a" * 124)
        self.assertEqual(panel.audit_id_text({"run_id": "a" * 129}), "")

    def test_depth_label_reads_profile(self):
        self.assertEqual(panel.depth_label({"profile": "deep", "ui_locale": "en-US"}), "Deep")
        self.assertEqual(panel.depth_label({"profile": "standard", "ui_locale": "en-US"}), "Standard")
        self.assertEqual(panel.depth_label({"profile": "fast", "ui_locale": "en-US"}), "Fast")
        # generic label when the tier is absent (never the dead `mode` key)
        self.assertEqual(panel.depth_label({"mode": "deep"}), "Cross-vendor audit")
        self.assertEqual(panel.depth_label({}), "Cross-vendor audit")
        self.assertEqual(panel.depth_label({"mode": "deep", "ui_locale": "zh-CN"}), "跨厂商审核")
        self.assertEqual(panel.depth_label({"profile": "deep", "ui_locale": "zh-CN"}), "深度")

    def test_run_title_fallback_localizes_by_run_locale(self):
        # A blank title falls back to the localized default, following THIS run's locale (per-run,
        # like the depth/action lines) — not the host chrome. A real title is returned untouched.
        self.assertEqual(panel.run_title({"title": "", "ui_locale": "zh-CN"}), "审核")
        self.assertEqual(panel.run_title({"title": "", "ui_locale": "en-US"}), "Audit")
        self.assertEqual(panel.run_title({"title": "  ", "ui_locale": "zh-CN"}), "审核")
        self.assertEqual(panel.run_title({"title": "Specific audit", "ui_locale": "zh-CN"}),
                         "Specific audit")

    def test_overall_status_hides_voices(self):
        self.assertEqual(panel.overall_status({"status": "queued", "auditors": []}), "queued")
        running = {"auditors": [{"status": "running"}, {"status": "completed"}]}
        self.assertEqual(panel.overall_status(running), "running")
        partial = {"auditors": [{"status": "failed"}, {"status": "completed"}]}
        self.assertEqual(panel.overall_status(partial), "partial")
        allok = {"auditors": [{"status": "completed"}, {"status": "completed"}]}
        self.assertEqual(panel.overall_status(allok), "completed")

    def test_elapsed_frozen_once_terminal(self):
        frozen = {}
        run = {"run_id": "x", "status": "completed", "started_at": self.NOW - 40,
               "auditors": [{"status": "completed"}]}
        first = panel.elapsed_seconds(run, self.NOW, frozen)
        later = panel.elapsed_seconds(run, self.NOW + 100, frozen)  # clock moved on
        self.assertAlmostEqual(first, 40.0, places=3)
        self.assertEqual(first, later, "terminal elapsed must freeze, not keep ticking")

    def test_elapsed_zero_without_started_at(self):
        # the exact "0s" symptom, but ONLY when started_at is genuinely absent (queued / pre-start)
        self.assertEqual(panel.elapsed_seconds({"run_id": "y", "status": "running"}, self.NOW, {}), 0.0)

    def test_elapsed_string(self):
        self.assertEqual(panel.elapsed_string(5), "5s")
        self.assertEqual(panel.elapsed_string(95), "1m 35s")

    def test_depth_line_text_variants(self):
        base = {"run_id": "a", "profile": "deep", "ui_locale": "zh-CN",
                "started_at": self.NOW - 95}
        running = dict(base, status="running", auditors=[{"status": "running"}])
        self.assertEqual(panel.depth_line_text(running, self.NOW, {}), "深度 · 审核中 · 1m 35s ⏱")
        done = dict(base, status="completed", auditors=[{"status": "completed"}])
        self.assertEqual(panel.depth_line_text(done, self.NOW, {}), "深度 · 已完成 · 1m 35s ✓")
        queued = {"run_id": "a", "profile": "fast", "ui_locale": "zh-CN",
                  "status": "queued", "auditors": []}
        self.assertEqual(panel.depth_line_text(queued, self.NOW, {}), "快速 · 排队中 ⏱")

        english = {"run_id": "a", "profile": "standard", "ui_locale": "en-US",
                   "status": "running", "started_at": self.NOW - 95,
                   "auditors": [{"status": "running"}]}
        self.assertEqual(
            panel.depth_line_text(english, self.NOW, {}),
            "Standard · Auditing · 1m 35s ⏱",
        )
        english_queued = {"run_id": "a", "profile": "fast", "ui_locale": "en-US",
                          "status": "queued", "auditors": []}
        self.assertEqual(
            panel.depth_line_text(english_queued, self.NOW, {}),
            "Fast · Queued ⏱",
        )

    def test_hosted_run_ignores_stale_lite_metadata_and_keeps_the_hosted_line(self):
        run = {
            "run_id": "aud_recovered",
            "local": False,
            "local_surface": "de_lite",
            "fallback_mode": "session-llm",
            "degrade_reason": "mcp_unavailable",
            "profile": "standard",
            "ui_locale": "zh-CN",
            "status": "running",
            "started_at": self.NOW - 5,
        }

        line = panel.depth_line_text(run, self.NOW, {})
        projected = runner.active_run_payload(run)
        self.assertEqual(line, "标准 · 审核中 · 5s ⏱")
        self.assertNotIn("DE Lite", line)
        self.assertNotIn("MCP", line)
        for field in ("local_surface", "fallback_mode", "degrade_reason", "degrade_action"):
            self.assertNotIn(field, projected)

    def test_stop_panel_uses_one_run_locale_and_has_a_deterministic_fallback(self):
        self.assertEqual(panel.ui_locale({"ui_locale": "en-US"}), "en")
        self.assertEqual(panel.ui_locale({"ui_locale": "zh-CN"}), "zh")
        env = {k: v for k, v in os.environ.items() if k != "DE_UI_LOCALE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(panel.ui_locale({"ui_locale": "fr-FR"}), "en")
        self.assertEqual(panel.action_text({"ui_locale": "zh-CN"}, "running"), "进行中")
        self.assertEqual(panel.action_text({"ui_locale": "en-US"}, "running"), "Running")

    def test_explicit_run_locale_precedes_the_host_locale(self):
        with mock.patch.dict(os.environ, {"DE_UI_LOCALE": "en-US"}):
            self.assertEqual(panel.ui_locale({"ui_locale": "zh-CN"}), "zh")
            self.assertEqual(panel.ui_locale({}), "en")

    def test_unset_locale_falls_back_to_english(self):
        """Default UI locale is en-US: a run with no `ui_locale` and no host `DE_UI_LOCALE`
        renders English, and an unsupported tag falls back to English rather than Chinese."""
        env = {k: v for k, v in os.environ.items() if k != "DE_UI_LOCALE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(runner.normalize_ui_locale(None), "en-US")
            self.assertEqual(runner.normalize_ui_locale("fr-FR"), "en-US")
            self.assertEqual(panel.ui_locale({}), "en")
            self.assertEqual(panel.action_text({}, "running"), "Running")
            self.assertIn("Auditing", panel.depth_line_text(
                {"run_id": "a", "profile": "standard", "started_at": 0.0,
                 "auditors": [{"status": "running"}]}, 5.0, {}))

    # ── local advisory run (design §11/§17): honest 🔶 label, never a cross-vendor pass ──
    def test_depth_label_marks_a_local_advisory_run(self):
        self.assertEqual(panel.depth_label({"local": True, "profile": "standard"}),
                         "🔶 Local fallback")
        self.assertEqual(
            panel.depth_label({"local": True, "profile": "standard", "ui_locale": "zh-CN"}),
            "🔶 本地降级")

    def test_de_lite_local_run_has_product_label_and_honest_statuses(self):
        running = {"run_id": "local-lite", "local": True, "local_surface": "de_lite",
                   "ui_locale": "zh-CN", "status": "running", "started_at": self.NOW - 95}
        completed = {**running, "status": "completed", "completed_at": self.NOW}
        partial = {**running, "status": "partial", "completed_at": self.NOW}
        self.assertEqual(panel.depth_label(running), "DE Lite")
        self.assertEqual(
            panel.depth_line_text(running, self.NOW, {}),
            "DE Lite · 本地审核中 · 1m 35s ⏱",
        )
        self.assertEqual(
            panel.depth_line_text(completed, self.NOW, {}),
            "DE Lite · 本地审核完成（仅供参考） · 1m 35s",
        )
        self.assertEqual(
            panel.depth_line_text(partial, self.NOW, {}),
            "DE Lite · 本地审核部分完成（仅供参考） · 1m 35s",
        )
        self.assertNotEqual(panel.depth_line_color(completed), panel.FG_GREEN)

    def test_de_lite_local_run_shows_the_downgrade_reason(self):
        run = {
            "run_id": "local-lite", "local": True, "local_surface": "de_lite",
            "ui_locale": "zh-CN", "degrade_reason": "credits_exhausted", "status": "running",
            "started_at": self.NOW - 5,
        }
        line = panel.depth_line_text(run, self.NOW, {})
        self.assertIn("DE Lite · 本地审核中", line)
        self.assertIn("积分不足", line)

    def test_de_lite_local_run_shows_mcp_unavailable_before_and_after_completion(self):
        running = {
            "run_id": "local-host", "local": True, "local_surface": "de_lite",
            "ui_locale": "zh-CN", "degrade_reason": "mcp_unavailable", "status": "running",
            "started_at": self.NOW - 5,
        }
        completed = {**running, "status": "completed", "completed_at": self.NOW}

        self.assertIn("本地审核中 · MCP 不可用", panel.depth_line_text(
            running, self.NOW, {}
        ))
        self.assertIn("本地审核完成（仅供参考） · MCP 不可用", panel.depth_line_text(
            completed, self.NOW, {}
        ))

    def test_de_lite_local_run_shows_service_unavailable_as_a_status_reason(self):
        run = {
            "run_id": "local-service", "local": True, "local_surface": "de_lite",
            "ui_locale": "zh-CN", "degrade_reason": "service_unavailable", "status": "running",
            "started_at": self.NOW - 5,
        }

        self.assertIn(
            "DE Lite · 本地审核中 · 服务不可用",
            panel.depth_line_text(run, self.NOW, {}),
        )

    def test_local_run_running_line_uses_product_language(self):
        run = {"run_id": "local-x", "local": True, "ui_locale": "zh-CN",
               "status": "running", "started_at": self.NOW - 95}
        self.assertEqual(panel.depth_line_text(run, self.NOW, {}),
                         "🔶 本地降级 · 单模型非跨厂商 · 本地审核中 · 1m 35s ⏱")

    def test_local_run_uses_one_english_locale(self):
        run = {"run_id": "local-en", "local": True, "local_surface": "de_lite",
               "degrade_reason": "mcp_unavailable", "ui_locale": "en-US",
               "status": "running", "started_at": self.NOW - 5}
        line = panel.depth_line_text(run, self.NOW, {})
        self.assertIn("DE Lite · Local audit in progress · MCP unavailable", line)
        self.assertNotIn("本地审核", line)
        self.assertNotIn("审核中", line)

    def test_local_run_finished_line_is_advisory_only_never_a_pass_check(self):
        run = {"run_id": "local-x", "local": True, "ui_locale": "zh-CN", "status": "completed",
               "started_at": self.NOW - 30, "completed_at": self.NOW}
        line = panel.depth_line_text(run, self.NOW, {})
        self.assertIn("🔶 本地降级 · 单模型非跨厂商", line)
        self.assertIn("仅参考", line)
        self.assertNotIn("✓", line)                 # ✓ / 已完成 reads as "passed" — never for advisory
        self.assertNotIn("已完成", line)

    def test_local_run_is_never_rendered_green(self):
        # green reads as "passed"; a single-model advisory must never look like a panel pass.
        done = {"run_id": "local-x", "local": True, "status": "completed",
                "started_at": self.NOW - 30, "completed_at": self.NOW}
        running = {"run_id": "local-x", "local": True, "status": "running", "started_at": self.NOW - 5}
        self.assertNotEqual(panel.depth_line_color(done), panel.FG_GREEN)
        self.assertNotEqual(panel.depth_line_color(running), panel.FG_GREEN)

    def test_active_run_payload_carries_and_defaults_the_local_flag(self):
        self.assertIs(runner.active_run_payload(
            {"run_id": "local-x", "status": "running", "local": True})["local"], True)
        self.assertIs(runner.active_run_payload(
            {"run_id": "r1", "status": "running"})["local"], False)
        self.assertIs(runner.active_run_payload(
            {"run_id": "r1", "status": "running", "debug_authorized": True}
        )["debug_authorized"], True)
        self.assertIs(runner.active_run_payload(
            {"run_id": "r1", "status": "running", "debug_authorized": False}
        )["debug_authorized"], False)
        self.assertNotIn(
            "debug_authorized",
            runner.active_run_payload({"run_id": "r1", "status": "running"}),
        )

    # audit ee5e025a: local status→word/color mapping across every state
    def _local(self, status, locale="zh-CN"):
        return {"run_id": "local-x", "local": True, "ui_locale": locale, "status": status,
                "started_at": self.NOW - 30, "completed_at": self.NOW}

    def test_local_status_words_cover_all_states(self):
        self.assertIn("排队中", panel.depth_line_text(self._local("queued"), self.NOW, {}))
        self.assertIn("本地审核中", panel.depth_line_text(self._local("running"), self.NOW, {}))
        self.assertIn("停止中", panel.depth_line_text(self._local("cancelling"), self.NOW, {}))
        self.assertIn("已取消", panel.depth_line_text(self._local("cancelled"), self.NOW, {}))
        self.assertIn("失败", panel.depth_line_text(self._local("failed"), self.NOW, {}))
        self.assertIn("部分完成（仅参考）", panel.depth_line_text(self._local("partial"), self.NOW, {}))

    def test_local_unknown_or_missing_status_never_asserts_completion(self):
        # audit ee5e025a f1 (3/4): neither a missing nor an unrecognized status may fall through
        # to a "完成" claim. A missing status is coerced to queued by overall_status (-> 排队中);
        # a genuinely unrecognized status hits the explicit 状态未知 branch. Both avoid completion.
        missing = panel.depth_line_text({"run_id": "local-x", "local": True,
                                         "ui_locale": "zh-CN", "started_at": self.NOW},
                                        self.NOW, {})
        self.assertNotIn("完成", missing)
        self.assertIn("排队中", missing)
        weird = panel.depth_line_text(self._local("weird_future_state"), self.NOW, {})
        self.assertNotIn("完成", weird)
        self.assertIn("状态未知", weird)
        # A run whose status is the literal string "unknown" is just another unrecognized state:
        # it must hit the 1-slot 状态未知 template, never a 2-slot completion template (which would
        # TypeError on the missing elapsed arg). Guards the catalog "unknown" key from colliding
        # with the arity dispatch. Exercises both the de_lite and the plain local-fallback surface.
        literal = panel.depth_line_text(self._local("unknown"), self.NOW, {})
        self.assertIn("状态未知", literal)
        self.assertNotIn("完成", literal)
        de_lite_unknown = panel.depth_line_text(
            {**self._local("unknown"), "local_surface": "de_lite"}, self.NOW, {})
        self.assertIn("状态未知", de_lite_unknown)

    def test_local_status_words_cover_all_states_in_the_default_english_locale(self):
        """Same audit ee5e025a contract on the now-default locale: no state may read as a pass."""
        en = lambda s: panel.depth_line_text(self._local(s, "en-US"), self.NOW, {})
        self.assertIn("Queued", en("queued"))
        self.assertIn("Local audit in progress", en("running"))
        self.assertIn("Stopping", en("cancelling"))
        self.assertIn("Cancelled", en("cancelled"))
        self.assertIn("Failed", en("failed"))
        self.assertIn("Partially complete (reference only)", en("partial"))
        self.assertIn("Unknown status (reference only)", en("weird_future_state"))
        for state in ("queued", "running", "cancelling", "cancelled", "failed",
                      "partial", "completed", "weird_future_state"):
            self.assertNotIn("✓", en(state))
            self.assertNotIn("审核", en(state))

    def test_local_failed_is_red_and_cancelling_is_active_colored(self):
        # audit ee5e025a f2/f1: failed keeps a red affordance (never green); cancelling is active.
        self.assertEqual(panel.depth_line_color(self._local("failed")), panel.FG_RED)
        self.assertEqual(panel.depth_line_color(self._local("cancelling")), panel.FG_MUTED)
        # and no local state is ever green
        for s in ("queued", "running", "cancelling", "cancelled", "failed", "partial", "completed"):
            self.assertNotEqual(panel.depth_line_color(self._local(s)), panel.FG_GREEN)

    # ── PR5-DE: a local run is never hub-polled (no hub id -> would 404-reap it) ──
    def test_should_hub_poll_is_false_for_local_runs_only(self):
        self.assertFalse(panel.should_hub_poll({"local": True, "status": "running"}))
        self.assertTrue(panel.should_hub_poll({"status": "running"}))
        self.assertTrue(panel.should_hub_poll({"local": False, "status": "running"}))

    def test_an_intel_mac_gets_the_shipped_x86_64_stopper(self):
        """Intel Macs must receive native x86_64 code; Rosetta cannot execute arm64 there."""
        with (
            mock.patch.object(runner.platform, "system", return_value="Darwin"),
            mock.patch.object(runner.platform, "machine", return_value="x86_64"),
        ):
            self.assertTrue(runner.shipped_stopper_arch_supported())
            self.assertEqual(
                runner.shipped_stopper_path(Path("/decision-engine")).name,
                "decision-engine-stopper-x86_64",
            )

    def test_apple_silicon_still_gets_the_shipped_stopper(self):
        """The anti-false-positive lock: the gate must not cost arm64 users their native panel."""
        with (
            mock.patch.object(runner.platform, "system", return_value="Darwin"),
            mock.patch.object(runner.platform, "machine", return_value="arm64"),
        ):
            self.assertTrue(runner.shipped_stopper_arch_supported())

    def test_the_launch_agent_selects_the_x86_64_stopper_on_intel(self):
        """The host LaunchAgent must execute the same architecture-selected binary as the runner."""
        from installer import stopper_launch_agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stopper = (
                root
                / "desktop"
                / "macos"
                / "bin"
                / "decision-engine-stopper-x86_64"
            )
            stopper.parent.mkdir(parents=True)
            stopper.write_bytes(b"native-stopper")
            stopper.chmod(0o755)
            with (
                mock.patch.object(runner.platform, "system", return_value="Darwin"),
                mock.patch.object(runner.platform, "machine", return_value="x86_64"),
                mock.patch.object(
                    stopper_launch_agent,
                    "_runtime_contract",
                    return_value={
                        "registry": Path("active-runs.json"),
                        "environment": {},
                        "log_path": Path("stopper.log"),
                    },
                ),
            ):
                payload = stopper_launch_agent._payload(root)

        self.assertEqual(
            Path(payload["ProgramArguments"][0]).name,
            "decision-engine-stopper-x86_64",
        )

    def test_unsupported_macos_architecture_stays_fail_closed(self):
        from installer import stopper_launch_agent

        root = Path(__file__).resolve().parents[2]
        with (
            mock.patch.object(runner.platform, "system", return_value="Darwin"),
            mock.patch.object(runner.platform, "machine", return_value="ppc64"),
        ):
            self.assertFalse(runner.shipped_stopper_arch_supported())
            self.assertIsNone(runner.shipped_stopper_path(root))
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent._payload(root)
            with self.assertRaises(stopper_launch_agent.LaunchAgentError):
                stopper_launch_agent._expected_payload(root)

    def test_an_oversized_title_cannot_reach_the_registry(self):
        """The registry has a hard 4 MiB ceiling and NOTHING trims it: one oversized title puts
        the file past that ceiling, every later read raises, and the host bridge stays broken
        until a human deletes the file by hand. Titles are one panel line — cap them on write."""
        payload = runner.active_run_payload({"run_id": "r1", "title": "x" * 5000})

        title = payload["title"]
        self.assertEqual(len(title), runner._MAX_TITLE_CHARS)
        self.assertTrue(title.endswith("…"), title[-10:])
        self.assertTrue(title.startswith("xxx"))

    def test_a_normal_title_is_passed_through_untouched(self):
        """The cap must be invisible in normal use — no stray ellipsis on a real title."""
        for title in ("", "审一下 permanent_setup 的 macOS 崩溃", "x" * runner._MAX_TITLE_CHARS):
            with self.subTest(title=title[:20]):
                self.assertEqual(runner.active_run_payload({"run_id": "r", "title": title})["title"], title)

    def test_a_non_string_title_never_reaches_the_registry(self):
        """A caller passing a dict/list would serialize its whole body into the registry — the
        same ceiling failure by another route. Coerce rather than trust the caller."""
        for bad in ({"nested": "x" * 5000}, ["x"] * 5000, 12345, None):
            with self.subTest(kind=type(bad).__name__):
                self.assertEqual(runner.active_run_payload({"run_id": "r", "title": bad})["title"], "")

    def test_active_run_payload_carries_a_non_terminal_ttl(self):
        # a caller-set hidden_after (local advisory safety TTL) survives on a non-terminal run;
        # existing hub runs (no hidden_after) are untouched.
        p = runner.active_run_payload(
            {"run_id": "local-x", "status": "running", "local": True, "hidden_after": 1234.0})
        self.assertEqual(p["hidden_after"], 1234.0)
        self.assertNotIn("hidden_after", runner.active_run_payload(
            {"run_id": "r1", "status": "running"}))

    def test_overall_status_surfaces_cancel_over_auditors(self):
        # cancel is a run-level state; it must win over the auditor-derived status so the pill and
        # the depth line agree (a cancelled run must not read as "completed" off its voices).
        self.assertEqual(panel.overall_status(
            {"status": "cancelling", "auditors": [{"status": "running"}]}), "cancelling")
        self.assertEqual(panel.overall_status(
            {"status": "cancelled", "auditors": [{"status": "completed"}]}), "cancelled")

    def test_depth_line_color_cancelled_is_amber(self):
        self.assertEqual(panel.depth_line_color({"status": "cancelled", "auditors": []}), panel.FG_AMBER)
        self.assertEqual(panel.depth_line_color({"status": "failed", "auditors": [{"status": "failed"}]}), panel.FG_RED)

    def test_depth_line_text_cancel_states(self):
        base = {"run_id": "a", "profile": "fast", "ui_locale": "zh-CN",
                "started_at": self.NOW - 10}
        self.assertEqual(panel.depth_line_text(dict(base, status="cancelling"), self.NOW, {}),
                         "快速 · 停止中 · 10s ⏱")
        self.assertEqual(panel.depth_line_text(dict(base, status="cancelled"), self.NOW, {}),
                         "快速 · 已取消 · 10s")

    def test_run_title_truncation(self):
        self.assertEqual(panel.run_title({"title": ""}), "Audit")
        self.assertEqual(panel.run_title({"title": "short"}), "short")
        long = "x" * 60
        self.assertTrue(panel.run_title({"title": long}).endswith("…"))
        self.assertEqual(len(panel.run_title({"title": long})), 48)


class MergePollTests(unittest.TestCase):
    NOW = 2_000_000.0

    def test_view_fields_overlay_and_none_ignored(self):
        existing = {"run_id": "a", "title": "T", "profile": "deep", "started_at": 100.0}
        view = {"status": "running", "started_at": 123.0, "title": None}  # None must not wipe title
        merged = panel.merge_poll_view(existing, view, "a", self.NOW)
        self.assertEqual(merged["status"], "running")
        self.assertEqual(merged["started_at"], 123.0)
        self.assertEqual(merged["title"], "T")

    def test_cancelling_not_stomped_by_lagging_running_poll(self):
        existing = {"run_id": "a", "status": "cancelling"}
        merged = panel.merge_poll_view(
            existing, {"status": "running"}, "a", self.NOW,
            preserve_cancelling=True,
        )
        self.assertEqual(merged["status"], "cancelling", "a lagging running poll must not un-cancel")

    def test_stale_cancelling_without_an_inflight_request_yields_to_server_status(self):
        existing = {"run_id": "a", "status": "cancelling"}
        merged = panel.merge_poll_view(existing, {"status": "running"}, "a", self.NOW)
        self.assertEqual(merged["status"], "running")

    def test_cancelling_yields_to_terminal_poll(self):
        existing = {"run_id": "a", "status": "cancelling"}
        merged = panel.merge_poll_view(
            existing, {"status": "cancelled"}, "a", self.NOW,
            preserve_cancelling=True,
        )
        self.assertEqual(merged["status"], "cancelled")
        self.assertEqual(merged["hidden_after"], self.NOW + panel.FINISHED_LINGER_S)

    def test_non_dict_view_leaves_existing(self):
        existing = {"run_id": "a", "status": "running"}
        self.assertEqual(panel.merge_poll_view(existing, None, "a", self.NOW), existing)


class CancelContractTests(unittest.TestCase):
    def _app(self, status="queued"):
        app = object.__new__(panel.StopPanelApp)
        app.runs = {"r1": {"run_id": "r1", "status": status, "profile": "standard"}}
        app._cancel_inflight = set()
        app._server_verified = {"r1"}
        app._state_epochs = {}
        app._polling = set()
        app._not_found_polls = {}
        app._lock = threading.Lock()
        app._render = mock.Mock()
        return app

    def test_only_a_queued_run_dispatches_a_cancel_request(self):
        for status in ("running", "cancelling", "completed", "failed", "cancelled"):
            with self.subTest(status=status):
                app = self._app(status)
                with mock.patch.object(panel.threading, "Thread") as thread:
                    app._stop("r1")
                thread.assert_not_called()
                self.assertEqual(app.runs["r1"]["status"], status)

        app = self._app("queued")
        worker = mock.Mock()
        with mock.patch.object(panel.threading, "Thread", return_value=worker):
            app._stop("r1")
        self.assertEqual(app.runs["r1"]["status"], "cancelling")
        self.assertIn("r1", app._cancel_inflight)
        self.assertNotIn("r1", app._server_verified)
        worker.start.assert_called_once_with()

    def test_disk_seeded_queued_run_cannot_cancel_until_a_server_poll_verifies_it(self):
        app = self._app("queued")
        app._server_verified.clear()
        with mock.patch.object(panel.threading, "Thread") as thread:
            app._stop("r1")
        thread.assert_not_called()
        self.assertEqual(app.runs["r1"]["status"], "queued")

    def test_successful_poll_marks_a_queued_run_safe_to_cancel(self):
        app = self._app("queued")
        app._server_verified.clear()
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "queued", "profile": "standard"}
        ):
            app._poll("r1")
        self.assertIn("r1", app._server_verified)

    def test_stopped_true_is_the_only_response_that_confirms_cancelled(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(runner, "request_json", return_value={"stopped": True}), \
             mock.patch.object(runner, "save_active_run") as save, \
             mock.patch.object(panel.time, "time", return_value=100.0):
            app._cancel_request("r1")

        run = app.runs["r1"]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["hidden_after"], 100.0 + panel.FINISHED_LINGER_S)
        self.assertNotIn("r1", app._cancel_inflight)
        save.assert_called_once_with(run)

    def test_stopped_false_restores_a_non_cancellable_running_state(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"stopped": False, "status": "running"}
        ), \
             mock.patch.object(runner, "save_active_run") as save:
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "running")
        self.assertNotIn("r1", app._cancel_inflight)
        save.assert_called_once_with(app.runs["r1"])

    def test_transport_failure_requires_fresh_reconciliation_before_retry(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(runner, "request_json", side_effect=runner.AuditError("timeout")), \
             mock.patch.object(runner, "save_active_run") as save:
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "unknown")
        self.assertNotIn("r1", app._cancel_inflight)
        self.assertNotIn("r1", app._server_verified)
        save.assert_called_once_with(app.runs["r1"])

    def test_stopped_false_without_status_fails_closed_until_poll(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(runner, "request_json", return_value={"stopped": False}), \
             mock.patch.object(runner, "save_active_run") as save:
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "unknown")
        self.assertNotIn("r1", app._server_verified)
        save.assert_called_once_with(app.runs["r1"])

    def test_malformed_cancel_response_fails_closed_until_poll(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(runner, "request_json", return_value={"status": "queued"}), \
             mock.patch.object(runner, "save_active_run") as save:
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "unknown")
        self.assertNotIn("r1", app._server_verified)
        save.assert_called_once_with(app.runs["r1"])

    def test_stopped_false_with_terminal_status_stamps_linger_metadata(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"stopped": False, "status": "completed"}
        ), mock.patch.object(runner, "save_active_run") as save, \
             mock.patch.object(panel.time, "time", return_value=100.0):
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "completed")
        self.assertEqual(app.runs["r1"]["hidden_after"], 100.0 + panel.FINISHED_LINGER_S)
        save.assert_called_once_with(app.runs["r1"])

    def test_late_cancel_response_never_overwrites_a_terminal_poll(self):
        app = self._app("completed")
        app._cancel_inflight.add("r1")
        with mock.patch.object(runner, "request_json", return_value={"stopped": True}), \
             mock.patch.object(runner, "save_active_run") as save:
            app._cancel_request("r1")

        self.assertEqual(app.runs["r1"]["status"], "completed")
        self.assertNotIn("r1", app._cancel_inflight)
        save.assert_not_called()

    def test_poll_persists_a_server_status_transition_for_restart_safety(self):
        app = self._app("queued")
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "running", "profile": "standard"}
        ), mock.patch.object(runner, "save_active_run") as save:
            app._poll("r1")

        self.assertEqual(app.runs["r1"]["status"], "running")
        save.assert_called_once_with(app.runs["r1"])

    def test_terminal_poll_ends_an_inflight_cancel_and_wins_the_race(self):
        app = self._app("cancelling")
        app._cancel_inflight.add("r1")
        app._polling.add("r1")
        with mock.patch.object(
            runner, "request_json", return_value={"status": "completed", "profile": "standard"}
        ), mock.patch.object(runner, "save_active_run") as save:
            app._poll("r1")

        self.assertEqual(app.runs["r1"]["status"], "completed")
        self.assertNotIn("r1", app._cancel_inflight)
        save.assert_called_once_with(app.runs["r1"])

    def test_poll_issued_before_cancel_transition_cannot_regress_newer_state(self):
        for status in ("running", "cancelled"):
            with self.subTest(status=status):
                app = self._app(status)
                app._state_epochs["r1"] = 2
                app._polling.add("r1")
                app._server_verified.clear()
                with mock.patch.object(
                    runner, "request_json", return_value={"status": "queued"}
                ), mock.patch.object(runner, "save_active_run") as save:
                    app._poll("r1", expected_epoch=1)

                self.assertEqual(app.runs["r1"]["status"], status)
                self.assertNotIn("r1", app._server_verified)
                save.assert_not_called()


class LaunchWiringTests(unittest.TestCase):
    def test_detached_kwargs_windows_shape(self):
        with mock.patch.object(runner.os, "name", "nt"):
            kw = runner._detached_spawn_kwargs()
        self.assertIn("creationflags", kw)
        self.assertNotIn("start_new_session", kw)

    def test_detached_kwargs_posix(self):
        with mock.patch.object(runner.os, "name", "posix"):
            self.assertEqual(runner._detached_spawn_kwargs(), {"start_new_session": True})

    def test_gui_python_returns_interpreter_on_posix(self):
        # On this POSIX host _gui_python returns the running interpreter. The Windows pythonw.exe
        # preference can't be exercised here (pathlib refuses to build a WindowsPath on POSIX);
        # it is covered by the Owner self-test on real Windows.
        with mock.patch.object(runner.os, "name", "posix"):
            self.assertEqual(runner._gui_python(), runner.sys.executable)

    def test_panel_subprocess_argv_and_env(self):
        captured = {}

        def fake_popen(argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return mock.Mock()

        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(timeout=5)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(runner.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.dict(
                     runner.os.environ,
                     {"DE_CONFIG_PATH": str(Path(tmp) / "decision-engine" / "config.json")},
                     clear=False,
                 ):
                runner.os.environ.pop("DE_ACTIVE_RUN", None)
                runner.os.environ.pop("DE_ACTIVE_RUNS", None)
                runner._launch_stopper_panel_subprocess()
                for thread in threading.enumerate():
                    if thread is not threading.current_thread() and thread.daemon:
                        thread.join(timeout=5)
        self.assertEqual(captured["argv"][1:], ["-m", "client.stopper.panel"])
        env = captured["kw"]["env"]
        self.assertIn("DE_CONFIG_PATH", env)
        self.assertIn("DE_ACTIVE_RUNS_LOCK", env)
        with mock.patch.dict(runner.os.environ, env, clear=True):
            self.assertEqual(
                json.loads(env["DE_ACTIVE_RUNS_LOCK_PATHS"]),
                [str(path) for path in runner.active_runs_lock_paths()],
            )
            self.assertEqual(
                json.loads(env["DE_ACTIVE_RUNS_WRITE_PATHS"]),
                [str(path) for path in runner._active_runs_write_paths()],
            )
            self.assertFalse(runner._active_paths_are_overridden())
            self.assertEqual(len(runner.active_runs_lock_paths()), 2)
            self.assertEqual(Path(env["DE_ACTIVE_RUNS_LOCK"]), runner.active_runs_lock_path())
        # PYTHONPATH is prepended with the repo root, so `-m client.stopper.panel` resolves even
        # when the client is not pip-installed. The first entry must be that root (it holds client/).
        first_pp = env["PYTHONPATH"].split(runner.os.pathsep)[0]
        self.assertTrue((runner.Path(first_pp) / "client" / "stopper" / "panel.py").exists(), first_pp)

    def test_non_darwin_dispatches_to_panel(self):
        with mock.patch.object(runner.platform, "system", return_value="Windows"), \
             mock.patch.object(runner, "_launch_stopper_panel_subprocess") as launch, \
             mock.patch.dict(runner.os.environ, {}, clear=False):
            runner.launch_stopper_if_available()
        launch.assert_called_once()

    def test_de_lite_prefers_compatible_shipped_binary_over_installed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = root / "decision-engine-stopper-arm64"
            shipped.write_bytes(b"#!/bin/sh\n")
            shipped.chmod(0o755)
            installed = root / "old-installed-stopper"
            installed.write_bytes(b"#!/bin/sh\n")
            installed.chmod(0o755)
            with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
                 mock.patch.object(runner, "kickstart_stopper_launch_agent") as kickstart, \
                 mock.patch.object(runner, "launch_stopper_app") as launch_app, \
                 mock.patch.object(runner, "stopper_binary_path", return_value=installed), \
                 mock.patch.object(runner, "shipped_stopper_binary", return_value=shipped), \
                 mock.patch.object(runner, "stopper_log_path", return_value=root / "stopper.log"), \
                 mock.patch.object(
                     runner,
                     "_de_lite_stopper_runtime_env",
                     return_value={
                         "DE_ACTIVE_RUNS": "isolated-runs.json",
                         "DE_STOPPER_SINGLETON_LOCK": "isolated-stopper.lock",
                     },
                 ), \
                 mock.patch.object(runner, "_ensure_private_dir"), \
                 mock.patch.object(runner.subprocess, "Popen") as popen, \
                 mock.patch.dict(runner.os.environ, {}, clear=True):
                runner.launch_stopper_if_available(prefer_shipped=True)

        kickstart.assert_not_called()
        launch_app.assert_not_called()
        self.assertEqual(popen.call_args.args[0], [str(shipped)])
        self.assertEqual(popen.call_args.kwargs["env"]["DE_ACTIVE_RUNS"], "isolated-runs.json")
        self.assertEqual(
            popen.call_args.kwargs["env"]["DE_STOPPER_SINGLETON_LOCK"],
            "isolated-stopper.lock",
        )

    def test_de_lite_does_not_fall_back_to_an_installed_stopper(self):
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner, "kickstart_stopper_launch_agent") as kickstart, \
             mock.patch.object(runner, "launch_stopper_app") as launch_app, \
             mock.patch.object(runner, "shipped_stopper_binary", return_value=None), \
             mock.patch.object(runner, "_note_stopper_miss") as note, \
             mock.patch.object(runner.subprocess, "Popen") as popen, \
             mock.patch.dict(runner.os.environ, {}, clear=True):
            runner.launch_stopper_if_available(prefer_shipped=True)

        kickstart.assert_not_called()
        launch_app.assert_not_called()
        popen.assert_not_called()
        self.assertIn("compatible shipped stopper required", note.call_args.args[0])

    def test_hosted_launch_keeps_launch_agent_precedence(self):
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner, "kickstart_stopper_launch_agent", return_value=True) as kickstart, \
             mock.patch.object(runner, "launch_stopper_app") as launch_app, \
             mock.patch.object(runner, "shipped_stopper_binary") as shipped, \
             mock.patch.dict(runner.os.environ, {}, clear=True):
            runner.launch_stopper_if_available()

        kickstart.assert_called_once_with()
        launch_app.assert_not_called()
        shipped.assert_not_called()

    def test_de_lite_runtime_env_reuses_the_shared_registry_and_singleton_lock(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runner, "config_path", return_value=Path(tmp) / "config.json"), \
             mock.patch.dict(runner.os.environ, {}, clear=True):
            parent_locks = runner.de_lite_active_runs_lock_paths()
            env = runner._de_lite_stopper_runtime_env()
            regular_runs = runner.active_runs_path()
            lite_runs = runner.de_lite_active_runs_path()
            parent_write_paths = runner._active_runs_write_paths()
            with mock.patch.dict(runner.os.environ, env, clear=True):
                child_locks = runner.active_runs_lock_paths()
                child_primary = runner.active_runs_lock_path()

        self.assertEqual(lite_runs, regular_runs)
        self.assertEqual(child_locks, parent_locks)
        self.assertEqual(child_primary, parent_locks[-1])
        self.assertEqual(Path(env["DE_ACTIVE_RUNS"]), regular_runs)
        self.assertEqual(
            json.loads(env["DE_ACTIVE_RUNS_WRITE_PATHS"]),
            [str(path) for path in parent_write_paths],
        )
        self.assertNotIn("DE_STOPPER_SINGLETON_LOCK", env)

    def test_managed_de_lite_run_uses_the_shared_registry_writer(self):
        empty = {"schema_version": 1, "updated_at": 0.0, "runs": {}}
        with mock.patch.object(runner, "_active_paths_are_overridden", return_value=False), \
             mock.patch.object(runner, "load_active_runs_registry", return_value=empty), \
             mock.patch.object(runner, "_write_active_runs_registry") as shared_write, \
             mock.patch.object(
                 runner,
                 "_load_de_lite_active_runs_registry",
                 side_effect=AssertionError("managed DE Lite must use shared registry"),
             ), \
             mock.patch.object(runner, "_write_active_run_payload"):
            runner.save_local_advisory_run(
                "local-shared", surface="de_lite", title="Shared window"
            )

        shared_write.assert_called_once()

    def test_invalid_injected_lock_paths_fail_closed(self):
        for value in ("not-json", "[]", '["", "/tmp/lock"]'):
            with self.subTest(value=value), \
                 mock.patch.dict(
                     runner.os.environ,
                     {"DE_ACTIVE_RUNS_LOCK_PATHS": value},
                     clear=True,
                 ):
                with self.assertRaises(runner.AuditError):
                    runner.active_runs_lock_paths()

    def test_de_lite_local_run_uses_the_regular_shared_registry(self):
        for thread in threading.enumerate():
            if thread is not threading.current_thread() and thread.daemon:
                thread.join(timeout=5)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runner, "config_path", return_value=Path(tmp) / "config.json"), \
             mock.patch.dict(runner.os.environ, {}, clear=True):
            runner.save_local_advisory_run(
                "local-isolated", surface="de_lite", status="running"
            )
            lite_runs = runner.de_lite_active_runs_path()
            regular_runs = runner.active_runs_path()
            self.assertEqual(lite_runs, regular_runs)
            self.assertTrue(lite_runs.exists())

    def test_hosted_and_de_lite_rows_share_one_registry(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runner, "config_path", return_value=Path(tmp) / "config.json"), \
             mock.patch.dict(runner.os.environ, {}, clear=True):
            runner.save_active_run({
                "run_id": "hosted-one",
                "status": "queued",
                "title": "Hosted audit",
                "debug_authorized": True,
            })
            runner.save_local_advisory_run(
                "local-service-one",
                surface="de_lite",
                status="running",
                title="Offline audit",
                degrade_reason="service_unavailable",
            )
            registry = runner.load_active_runs_registry()

        self.assertEqual(set(registry["runs"]), {"hosted-one", "local-service-one"})
        self.assertIs(registry["runs"]["hosted-one"]["local"], False)
        self.assertIs(registry["runs"]["hosted-one"]["debug_authorized"], True)
        self.assertIs(registry["runs"]["local-service-one"]["local"], True)

    def test_panel_log_setup_failure_never_escapes_audit_start(self):
        with mock.patch.object(runner, "_ensure_private_dir", side_effect=runner.AuditError("linked")), \
             mock.patch.object(runner, "_note_stopper_miss") as note, \
             mock.patch.object(runner.subprocess, "Popen") as popen:
            runner._launch_stopper_panel_subprocess()
        popen.assert_called_once()
        note.assert_called_once()

    def test_native_log_setup_failure_never_escapes_audit_start(self):
        with mock.patch.object(runner.platform, "system", return_value="Darwin"), \
             mock.patch.object(runner, "kickstart_stopper_launch_agent", return_value=False), \
             mock.patch.object(runner, "launch_stopper_app", return_value=False), \
             mock.patch.object(runner, "stopper_binary_path", return_value=Path("stopper")), \
             mock.patch.object(runner.os.path, "exists", return_value=True), \
             mock.patch.object(runner.os, "access", return_value=True), \
             mock.patch.object(runner, "_ensure_private_dir", side_effect=runner.AuditError("linked")), \
             mock.patch.object(runner, "_note_stopper_miss") as note, \
             mock.patch.object(runner.subprocess, "Popen") as popen:
            runner.launch_stopper_if_available()
        popen.assert_not_called()
        note.assert_called_once()

    def test_singleton_reentry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_lock = Path(tmp) / "runtime" / "panel.lock"
            try:
                with mock.patch.object(
                    runner,
                    "stopper_panel_lock_paths",
                    return_value=(Path(tmp) / "legacy.lock", runtime_lock),
                ), mock.patch.object(
                    runner, "stopper_panel_lock_path", return_value=runtime_lock,
                ), mock.patch.object(
                    runner, "runtime_dir", return_value=Path(tmp) / "runtime",
                ), mock.patch.object(runner, "fcntl", None), mock.patch.object(runner, "msvcrt", None):
                    panel._singleton_handles = []
                    self.assertTrue(panel.acquire_single_instance())
                    self.assertTrue(panel.acquire_single_instance())
            finally:
                for handle in panel._singleton_handles:
                    handle.close()
                panel._singleton_handles = []

    def test_singleton_refuses_to_launch_when_no_lock_file_can_be_opened(self):
        panel._singleton_handles = []
        with mock.patch.object(
            runner,
            "stopper_panel_lock_paths",
            return_value=(Path("legacy.lock"), Path("runtime.lock")),
        ), mock.patch.object(runner, "_open_lock_file", side_effect=OSError("read only")) as opened, \
             mock.patch.object(runner, "_note_stopper_miss") as note:
            self.assertFalse(panel.acquire_single_instance())
        self.assertEqual(opened.call_count, 1, "strict compatibility locking stops at the first missing lock")
        note.assert_called_once()

    def test_singleton_second_open_failure_releases_the_first_handle(self):
        first = mock.Mock()
        panel._singleton_handles = []
        with mock.patch.object(
            runner,
            "stopper_panel_lock_paths",
            return_value=(Path("legacy.lock"), Path("runtime.lock")),
        ), mock.patch.object(
            runner, "_open_lock_file", side_effect=[first, OSError("second failed")],
        ), mock.patch.object(runner, "fcntl", None), mock.patch.object(runner, "msvcrt", None):
            self.assertFalse(panel.acquire_single_instance())
        first.close.assert_called_once()


class ProdElapsedRegressionTests(unittest.TestCase):
    """Locks the fix for the reported "Standard took 24m 40s" bug, using the real prod run behind
    that screenshot (hub `audit-hub.sqlite3`, run aud_nNXkUzTcVWSc_Pu8 "DB scripts concurrency
    review"): the hub started it at STARTED, finished it at COMPLETED — 210s, four voices in
    parallel, zero queue — while the panel displayed 24m 40s (1480s).

    Root cause: elapsed came from `panel_local_now - started_at`, frozen the first time the row was
    observed terminal. Whenever the panel observed the finish late — a restart drops the freeze
    cache, and a disk re-seed flips the row back to "running" so the next poll re-freezes it at the
    current time — the number became "how long ago it started". Every screenshot row froze at the
    same wall-clock instant (~10:23 UTC = when the screenshot was taken), each showing
    `that_instant - its own started_at`: 12m26s / 24m40s / 46m30s for 224s / 210s / 208s runs.
    """
    STARTED = 1784195901.3821762      # hub started_at   (09:58:21 UTC)
    COMPLETED = 1784196111.4151993    # hub completed_at (10:01:51 UTC)
    TRUE_S = 210.0                    # what it actually took
    SCREENSHOT_NOW = STARTED + 1480.0  # when the tester looked — 24m40s after the start

    def _run(self, **over):
        run = {"run_id": "aud_nNXkUzTcVWSc_Pu8", "profile": "standard", "status": "completed",
               "started_at": self.STARTED, "completed_at": self.COMPLETED,
               "auditors": [{"status": "completed"} for _ in range(4)]}
        run.update(over)
        return run

    def test_finished_run_observed_late_reports_the_true_duration(self):
        # The exact bug: the panel only looks 24m40s after the start. Pre-fix this returned 1480.
        self.assertAlmostEqual(
            panel.elapsed_seconds(self._run(), self.SCREENSHOT_NOW, {}), self.TRUE_S, places=1)
        self.assertIn("3m 30s", panel.depth_line_text(self._run(), self.SCREENSHOT_NOW, {}))

    def test_duration_is_independent_of_when_and_whether_the_panel_watched(self):
        # Same run, observed at wildly different moments (incl. a panel that attached hours later):
        # the hub's two timestamps are the only inputs, so every answer is identical.
        for offset in (210.0, 1480.0, 2790.0, 86_400.0):
            self.assertAlmostEqual(
                panel.elapsed_seconds(self._run(), self.STARTED + offset, {}), self.TRUE_S, places=1)

    def test_restart_losing_the_freeze_cache_does_not_inflate(self):
        frozen = {}
        panel.elapsed_seconds(self._run(), self.STARTED + 211.0, frozen)   # observed live
        # panel restarts → fresh process, empty cache, first sight is long after the finish
        self.assertAlmostEqual(
            panel.elapsed_seconds(self._run(), self.SCREENSHOT_NOW, {}), self.TRUE_S, places=1)

    def test_local_clock_skew_cannot_reach_the_number(self):
        # Windows box 21min ahead of the hub: pre-fix that skew landed straight in the duration,
        # since `now` (local) was subtracted from `started_at` (hub).
        for skew in (-3600.0, -60.0, 0.0, 60.0, 1260.0, 3600.0):
            self.assertAlmostEqual(
                panel.elapsed_seconds(self._run(), self.COMPLETED + skew, {}), self.TRUE_S, places=1)

    def test_running_run_still_ticks_live(self):
        run = self._run(status="running", completed_at=None,
                        auditors=[{"status": "completed"}, {"status": "running"}])
        self.assertAlmostEqual(
            panel.elapsed_seconds(run, self.STARTED + 90.0, {}), 90.0, places=1)

    def test_terminal_without_completed_at_falls_back_to_the_freeze(self):
        # A disk-seeded row the hub never answered for: keep the old behavior rather than show 0.
        run = self._run(completed_at=None)
        frozen = {}
        first = panel.elapsed_seconds(run, self.STARTED + 300.0, frozen)
        self.assertAlmostEqual(first, 300.0, places=1)
        self.assertAlmostEqual(
            panel.elapsed_seconds(run, self.STARTED + 9999.0, frozen), 300.0, places=1)


class DisconnectTests(unittest.TestCase):
    """A poll failure leaves the previous view in place, so a running row must not keep ticking a
    confident timer while the panel is blind — the run may already be finished server-side."""
    T0 = 2_000_000.0

    def setUp(self):
        # These rows carry no `ui_locale` and assert English output; pin system detection OFF so the
        # default resolves to en-US regardless of the host machine's locale.
        patcher = mock.patch.object(i18n, "detect_system_locale", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _running(self, last_ok):
        return {"run_id": "r1", "profile": "standard", "status": "running", "started_at": self.T0,
                "last_ok_at": last_ok, "auditors": [{"status": "running"}]}

    def test_fresh_polls_are_not_stale(self):
        run = self._running(self.T0 + 100.0)
        self.assertFalse(panel.is_stale(run, self.T0 + 105.0))
        self.assertIn("Auditing", panel.depth_line_text(run, self.T0 + 105.0, {}))

    def test_silence_past_the_threshold_says_disconnected_not_a_live_timer(self):
        run = self._running(self.T0 + 100.0)
        now = self.T0 + 100.0 + panel.STALE_AFTER_S + 1.0
        self.assertTrue(panel.is_stale(run, now))
        text = panel.depth_line_text(run, now, {})
        self.assertIn("Connection interrupted", text)
        self.assertIn("Last known 1m 40s", text)   # anchored to the last real poll, not to `now`
        self.assertEqual(panel.depth_line_color(run, now), panel.FG_AMBER)

    def test_never_polled_row_is_not_reported_as_disconnected(self):
        run = self._running(0.0)              # just seeded by the shim; no poll has run yet
        self.assertFalse(panel.is_stale(run, self.T0 + 9999.0))

    def test_terminal_row_is_never_stale_and_self_heals_after_reconnect(self):
        # Offline through the whole audit; on reconnect the hub's timestamps give the exact answer.
        run = {"run_id": "r1", "profile": "standard", "status": "completed", "started_at": self.T0,
               "completed_at": self.T0 + 180.0, "last_ok_at": self.T0 + 1.0,
               "auditors": [{"status": "completed"}]}
        far_later = self.T0 + 7200.0
        self.assertFalse(panel.is_stale(run, far_later))
        self.assertAlmostEqual(panel.elapsed_seconds(run, far_later, {}), 180.0, places=1)
        self.assertIn("Completed · 3m 0s", panel.depth_line_text(run, far_later, {}))

    def test_successful_poll_stamps_last_ok_at(self):
        merged = panel.merge_poll_view({"run_id": "r1"}, {"status": "running"}, "r1", self.T0)
        self.assertEqual(merged["last_ok_at"], self.T0)


class DiskResurrectionTests(unittest.TestCase):
    """The active-runs registry on disk is only pruned when the shim next saves/clears a run, and
    its entries still carry the status seeded at SUBMIT. So a finished-and-pruned run stays on disk
    looking "running" — re-seeding it would restart the row's timer and re-freeze it at the current
    time on the next poll, compounding every linger cycle. That loop is what let the displayed
    elapsed grow without bound (prod: 210s shown as 24m40s)."""

    def _app(self, retired=()):
        app = object.__new__(panel.StopPanelApp)   # bypass __init__: it imports tkinter
        app.runs = {}
        app._retired = set(retired)
        app._polling = set()
        app._cancel_inflight = set()
        app._server_verified = set()
        app._state_epochs = {}
        app._not_found_polls = {}
        app._lock = threading.Lock()
        return app

    _STALE_DISK = {"runs": {"r1": {"run_id": "r1", "status": "running", "profile": "standard",
                                   "started_at": 1.0}}}

    def test_retired_run_is_not_resurrected_from_disk(self):
        app = self._app(retired=["r1"])
        with mock.patch.object(runner, "load_active_runs_registry", return_value=self._STALE_DISK):
            app._merge_disk()
        self.assertEqual(app.runs, {}, "a finished+pruned run must not come back as running")

    def test_unseen_run_is_still_seeded_from_disk(self):
        app = self._app()
        with mock.patch.object(runner, "load_active_runs_registry", return_value=self._STALE_DISK):
            app._merge_disk()
        self.assertIn("r1", app.runs)   # the seed path still works for genuinely new runs

    def test_prune_marks_the_run_retired(self):
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "completed", "hidden_after": 10.0}}
        app._prune(11.0)
        self.assertEqual(app.runs, {})
        self.assertIn("r1", app._retired)


class RegistryReapTests(unittest.TestCase):
    """Finished audits must leave the panel and STAY gone.

    Reported: on Windows no audit ever disappeared — they stacked into a long list. The panel's
    own lifecycle is fine (30s linger, then prune, then exit when idle); the problem is its data
    source. On the MCP path the shim seeds a run at submit (queued/running → no `hidden_after`)
    and never writes again, so `prune_active_runs` — which only drops entries whose `hidden_after`
    has passed — can never remove it, and `clear_active_run` is only reached from the CLI. The
    entry outlives the audit, so every panel start re-seeds the whole history as "running".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "active-runs.json"
        self._prev = os.environ.get("DE_ACTIVE_RUNS")
        os.environ["DE_ACTIVE_RUNS"] = str(self.registry)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        # Disk reaps are deliberately daemonized in production.  Join any test-started
        # worker before restoring the process-global path override; otherwise a slow
        # Windows lock acquisition can make a worker from test N mutate/delete test
        # N+1's registry (or recreate a directory while TemporaryDirectory removes it).
        self._join_daemons()
        if self._prev is None:
            os.environ.pop("DE_ACTIVE_RUNS", None)
        else:
            os.environ["DE_ACTIVE_RUNS"] = self._prev

    def _seed(self, *run_ids, status="running"):
        # Exactly what the shim writes at submit: no hidden_after, because it is not terminal yet.
        payload = {"runs": {r: {"run_id": r, "status": status, "profile": "standard",
                                "started_at": 1.0} for r in run_ids},
                   "updated_at": 1.0}
        self.registry.write_text(json.dumps(payload), encoding="utf-8")

    def _app(self, retired=()):
        app = object.__new__(panel.StopPanelApp)   # bypass __init__: it imports tkinter
        app.runs = {}
        app._retired = set(retired)
        app._polling = set()
        app._cancel_inflight = set()
        app._server_verified = set()
        app._state_epochs = {}
        app._not_found_polls = {}
        app._lock = threading.Lock()
        return app

    def test_forget_active_run_drops_a_seeded_running_entry(self):
        # The entry the shim leaves behind: still "running", no hidden_after — the exact shape
        # prune_active_runs cannot touch.
        self._seed("r1", "r2")
        runner.forget_active_run("r1")
        left = json.loads(self.registry.read_text())["runs"]
        self.assertNotIn("r1", left)
        self.assertIn("r2", left, "must only drop the run it was asked to")

    def test_save_local_advisory_run_seeds_local_with_a_watchdog_ttl(self):
        # PR5-DE: the seed a caller writes when the user accepts a local advisory read. It carries
        # local=True (panel renders 🔶 + skips the hub poll) and a WATCHDOG hidden_after measured
        # from write-time (audit 9dc138c5): a live run re-writing extends it; started_at is kept
        # independent for correct elapsed and is immune to a stale value.
        before = time.time()
        runner.save_local_advisory_run("local-x", depth="deep", started_at=100.0, ttl_s=600.0)
        entry = json.loads(self.registry.read_text())["runs"]["local-x"]
        self.assertIs(entry["local"], True)
        self.assertEqual(entry["status"], "running")
        self.assertEqual(entry["profile"], "deep")
        self.assertEqual(entry["started_at"], 100.0)          # display start preserved as given
        self.assertGreaterEqual(entry["hidden_after"], before + 600.0)   # watchdog from NOW, not start
        self.assertLessEqual(entry["hidden_after"], time.time() + 601.0)
        reg = runner.prune_active_runs(runner.load_active_runs_registry(),
                                       now=entry["hidden_after"] + 1)
        self.assertNotIn("local-x", reg["runs"])

    def test_save_local_advisory_run_persists_de_lite_surface_and_title(self):
        runner.save_local_advisory_run(
            "local-lite", title="Lite review", surface="de_lite", status="running"
        )
        entry = json.loads(self.registry.read_text())["runs"]["local-lite"]
        self.assertEqual(entry["title"], "Lite review")
        self.assertEqual(entry["local_surface"], "de_lite")
        self.assertIs(entry["local"], True)

    def test_save_local_advisory_run_persists_downgrade_reason_and_action(self):
        runner.save_local_advisory_run(
            "local-lite", surface="de_lite", status="running",
            degrade_reason="subscription_expired", degrade_action="renew_subscription",
        )
        entry = json.loads(self.registry.read_text())["runs"]["local-lite"]
        self.assertEqual(entry["degrade_reason"], "subscription_expired")
        self.assertEqual(entry["degrade_action"], "renew_subscription")

    def test_local_completion_is_merged_into_an_already_visible_running_row(self):
        runner.save_local_advisory_run("local_lite", surface="de_lite", status="running")
        app = self._app()
        app._merge_disk()
        self.assertEqual(app.runs["local_lite"]["status"], "running")

        runner.complete_local_advisory_run("local_lite", status="completed")
        app._merge_disk()
        self.assertEqual(app.runs["local_lite"]["status"], "completed")
        self.assertGreater(app.runs["local_lite"]["hidden_after"],
                           app.runs["local_lite"]["completed_at"])

        app._prune(app.runs["local_lite"]["hidden_after"] + 1)
        self.assertNotIn("local_lite", app.runs)

    def test_terminal_local_status_absorbs_a_late_running_update(self):
        runner.save_local_advisory_run(
            "local-lite", surface="de_lite", status="failed"
        )
        failed = json.loads(self.registry.read_text())["runs"]["local-lite"]

        runner.forget_active_run("local-lite")
        reaped = json.loads(self.registry.read_text())
        self.assertNotIn("local-lite", reaped["runs"])
        self.assertIn("local-lite", reaped["local_terminal_tombstones"])

        runner.save_local_advisory_run(
            "local-lite", surface="de_lite", status="running"
        )
        after = json.loads(self.registry.read_text())

        self.assertNotIn("local-lite", after["runs"])
        self.assertGreater(
            after["local_terminal_tombstones"]["local-lite"], failed["completed_at"]
        )

    def test_save_local_advisory_run_rejects_a_nonsensical_ttl(self):
        # audit 9dc138c5 (codex): 0 / negative / non-finite ttl must fall back to the default ceiling
        # rather than seed an entry that prunes instantly or never.
        before = time.time()
        for bad in (0, -5, float("inf"), float("nan")):
            runner.save_local_advisory_run("local-b", ttl_s=bad)
            entry = json.loads(self.registry.read_text())["runs"]["local-b"]
            self.assertGreaterEqual(entry["hidden_after"], before + runner.LOCAL_ADVISORY_TTL_S)

    def test_local_flag_survives_a_re_save_update(self):
        # audit 9dc138c5 (grok f1): updating a local entry must keep local=True so should_hub_poll
        # stays False (else a later poll 404-reaps it mid-read).
        runner.save_local_advisory_run("local-x", status="running")
        runner.save_local_advisory_run("local-x", status="completed")
        entry = json.loads(self.registry.read_text())["runs"]["local-x"]
        self.assertIs(entry["local"], True)
        self.assertFalse(panel.should_hub_poll(entry))

    def test_complete_local_advisory_run_sets_terminal_status_and_linger(self):
        runner.save_local_advisory_run("local_complete", surface="de_lite", status="running")
        result = runner.complete_local_advisory_run("local_complete", status="completed")
        entry = json.loads(self.registry.read_text())["runs"]["local_complete"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(entry["status"], "completed")
        self.assertGreater(entry["hidden_after"], entry["completed_at"])

    def test_complete_local_advisory_run_rejects_unknown_or_nonterminal_runs(self):
        with self.assertRaises(runner.AuditError):
            runner.complete_local_advisory_run("local-missing", status="completed")
        runner.save_local_advisory_run("local_complete", surface="de_lite", status="running")
        with self.assertRaises(runner.AuditError):
            runner.complete_local_advisory_run("local_complete", status="running")

    def test_prune_reaps_the_registry_so_a_restart_cannot_resurrect(self):
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "completed", "hidden_after": 10.0}}
        app._prune(11.0)
        for t in threading.enumerate():          # the reap runs off the Tk thread
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=2)
        self.assertEqual(app.runs, {})
        self.assertEqual(json.loads(self.registry.read_text())["runs"], {},
                         "a retired run left on disk comes back on the next panel start")
        # A FRESH panel (new process → empty _retired) must now see nothing to re-seed.
        fresh = self._app()
        with mock.patch.object(runner, "load_active_runs_registry",
                               side_effect=runner.load_active_runs_registry):
            fresh._merge_disk()
        self.assertEqual(fresh.runs, {}, "the stacked-rows symptom: history re-seeded on restart")

    def test_a_run_still_going_is_never_reaped(self):
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "running"}}   # no hidden_after → not finished
        app._prune(9_999.0)
        self.assertIn("r1", app.runs)
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_reap_failure_never_breaks_the_panel(self):
        app = self._app()
        with mock.patch.object(runner, "forget_active_run", side_effect=OSError("disk gone")):
            app._forget_on_disk("r1")   # must not raise

    # -- vanished-run reap (a hub 404 that _poll used to swallow forever) -------------------
    @staticmethod
    def _audit_error(status_code=None, error_code=None):
        exc = runner.AuditError("boom")
        if status_code is not None:
            exc.status_code = status_code
        if error_code is not None:
            exc.error_code = error_code
        return exc

    @staticmethod
    def _join_daemons():
        # The disk reap runs off the Tk thread (like _prune); wait for it before asserting on disk.
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(timeout=2)

    def _poll_n(self, app, run_id, times):
        for _ in range(times):
            app._polling.add(run_id)
            app._poll(run_id)

    def test_poll_forgets_a_vanished_run_after_the_404_grace(self):
        # A run whose hub state is gone (purged / never registered) 404s every poll. Past the
        # grace it must be reaped from BOTH memory and the on-disk registry — otherwise it can
        # never reach a terminal poll and shows "排队中" forever, stacking on each later start.
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(runner, "request_json", side_effect=self._audit_error(404)):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD)
        self._join_daemons()
        self.assertNotIn("r1", app.runs, "a run the hub 404s past grace must leave the panel")
        self.assertIn("r1", app._retired, "must be retired so _merge_disk cannot re-seed it")
        self.assertEqual(json.loads(self.registry.read_text())["runs"], {},
                         "the on-disk registry orphan must be reaped too")

    def test_poll_forgets_a_run_the_hub_reports_410_gone(self):
        # 410 Gone is the same "resource is gone" class as 404 — a hub that switches to it must not
        # be able to resurrect the immortal-orphan bug. (Audit aud_9jSM_wSMDESWKIrr finding F0.)
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(runner, "request_json", side_effect=self._audit_error(410)):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD)
        self._join_daemons()
        self.assertNotIn("r1", app.runs, "a 410 Gone past grace must reap like a 404")
        self.assertIn("r1", app._retired)

    def test_poll_forgets_a_synchronous_workflow_conflict(self):
        self._seed("adj-sync-result")
        app = self._app()
        app.runs = {
            "adj-sync-result": {
                "run_id": "adj-sync-result",
                "status": None,
            }
        }
        conflict = self._audit_error(409, "synchronous_workflow_id")
        with mock.patch.object(runner, "request_json", side_effect=conflict):
            self._poll_n(
                app,
                "adj-sync-result",
                panel.NOT_FOUND_FORGET_THRESHOLD,
            )
        self._join_daemons()
        self.assertNotIn("adj-sync-result", app.runs)
        self.assertIn("adj-sync-result", app._retired)
        self.assertEqual(
            json.loads(self.registry.read_text())["runs"],
            {},
        )

    def test_poll_keeps_an_unrelated_409_conflict(self):
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        conflict = self._audit_error(409, "audit_still_starting")
        with mock.patch.object(runner, "request_json", side_effect=conflict):
            self._poll_n(
                app,
                "r1",
                panel.NOT_FOUND_FORGET_THRESHOLD + 2,
            )
        self._join_daemons()
        self.assertIn("r1", app.runs)
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_poll_keeps_a_409_without_a_machine_error_code(self):
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(
            runner,
            "request_json",
            side_effect=self._audit_error(409),
        ):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD + 2)
        self._join_daemons()
        self.assertIn("r1", app.runs)
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_poll_never_reaps_on_a_non_gone_http_error(self):
        # A 401/403/5xx is NOT "gone" — it must stay transient (left as-is), never counted toward
        # the reap, however often it recurs.
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(runner, "request_json", side_effect=self._audit_error(503)):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD + 2)
        self._join_daemons()
        self.assertIn("r1", app.runs, "a 5xx must never reap the run")
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_poll_keeps_a_run_within_the_404_grace(self):
        # Right after submit the hub can briefly 404 before it registers the run — a single 404
        # must NOT reap it (that would delete a legitimate just-submitted audit).
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(runner, "request_json", side_effect=self._audit_error(404)):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD - 1)
        self._join_daemons()
        self.assertIn("r1", app.runs, "a 404 within the grace window must not reap")
        self.assertNotIn("r1", app._retired)
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_poll_transient_error_never_forgets(self):
        # A network / hub-down AuditError has no status_code — it is retried, never treated as
        # not-found, no matter how many times it recurs.
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        with mock.patch.object(runner, "request_json", side_effect=self._audit_error(None)):
            self._poll_n(app, "r1", panel.NOT_FOUND_FORGET_THRESHOLD + 2)
        self._join_daemons()
        self.assertIn("r1", app.runs, "a transient error must never reap the run")
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])

    def test_a_successful_poll_resets_the_404_grace(self):
        # Sub-threshold 404s then a live view must reset the counter, so a later lone 404 on a
        # now-healthy run does not trip the reap.
        self._seed("r1")
        app = self._app()
        app.runs = {"r1": {"run_id": "r1", "status": "queued"}}
        seq = [self._audit_error(404)] * (panel.NOT_FOUND_FORGET_THRESHOLD - 1)
        seq.append({"run_id": "r1", "status": "running", "profile": "standard", "started_at": 1.0})
        seq.append(self._audit_error(404))
        with mock.patch.object(runner, "request_json", side_effect=seq):
            self._poll_n(app, "r1", len(seq))
        self._join_daemons()
        self.assertIn("r1", app.runs, "the live poll must have reset the grace counter")
        self.assertIn("r1", json.loads(self.registry.read_text())["runs"])



class DebugAuditorDisplayTests(unittest.TestCase):
    NOW = 1_000.0

    def test_debug_authorized_reveals_models(self):
        base = {"run_id": "a", "profile": "standard", "ui_locale": "en-US", "status": "running", "started_at": 905.0}
        run = dict(base, debug_authorized=True, auditors=[{"status": "running", "model_id": "gpt-5.6-sol"}, {"status": "completed", "model_alias": "gemini-3.1-pro-high", "duration_ms": 12000}])
        text = panel.depth_line_text(run, self.NOW, {})
        self.assertIn("gpt-5.6-sol", text)
        self.assertIn("gemini-3.1-pro-high", text)
        self.assertIn("completed", text)

    def test_debug_authorized_fails_closed_for_unauthorized_or_malformed_auditors(self):
        base = {"run_id": "a", "profile": "standard", "ui_locale": "en-US", "status": "running", "started_at": 905.0}
        cases = ({"debug_authorized": False, "auditors": [{"status": "running", "model_id": "gpt-5.6-sol"}]}, {"debug_authorized": None, "auditors": [{"status": "running", "model_id": "gpt-5.6-sol"}]}, {"auditors": [{"status": "running", "model_id": "gpt-5.6-sol"}]}, {"debug_authorized": True, "auditors": []}, {"debug_authorized": True, "auditors": [None]})
        for over in cases:
            with self.subTest(over=over):
                text = panel.depth_line_text(dict(base, **over), self.NOW, {})
                self.assertEqual(text.splitlines()[0], "Standard · Auditing · 1m 35s ⏱")
                self.assertNotIn("gpt-5.6-sol", text)
                self.assertNotIn("gemini-3.1-pro-high", text)

if __name__ == "__main__":
    unittest.main()
