"""Behavior tests for the agent-launched permanent setup dialog."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client import i18n
from installer import config, permanent_setup
from client.popup import backend as popup_backend

# Captured before any test pins the gate in setUp, so a Windows test can drive the REAL one.
_REAL_GUI_GATE = permanent_setup._gui_session_blocked
_REAL_TK_GATE = permanent_setup._tk_creation_blocked


def _credential_tk_double(answers, *, action="Activate"):
    answer_values = iter(answers)
    root = mock.MagicMock()
    root.winfo_screenwidth.return_value = 1920
    root.winfo_screenheight.return_value = 1080
    root.register.side_effect = lambda callback: callback

    def string_var(_parent=None):
        variable = mock.MagicMock()
        values = next(answer_values)
        if values is None:
            variable.get.return_value = ""
            variable.set.side_effect = lambda value: variable.get.configure_mock(return_value=value)
        elif isinstance(values, (list, tuple)):
            variable.get.side_effect = values
        else:
            variable.get.return_value = values
        return variable

    tk_module = SimpleNamespace(
        TclError=RuntimeError,
        StringVar=string_var,
        Tk=mock.Mock(return_value=root),
    )
    label_instances = []
    labels_by_style = {}
    entry_instances = []

    def make_label(*_args, **kwargs):
        label = mock.MagicMock()
        label_instances.append(label)
        labels_by_style.setdefault(kwargs.get("style"), []).append(label)
        return label

    def make_entry(*_args, **_kwargs):
        entry = mock.MagicMock()
        entry_instances.append(entry)
        return entry

    widget_module = SimpleNamespace(
        Style=mock.Mock(side_effect=lambda *_args, **_kwargs: mock.MagicMock()),
        Frame=mock.Mock(side_effect=lambda *_args, **_kwargs: mock.MagicMock()),
        Label=mock.Mock(side_effect=make_label),
        Entry=mock.Mock(side_effect=make_entry),
        Button=mock.Mock(side_effect=lambda *_args, **_kwargs: mock.MagicMock()),
        label_instances=label_instances,
        labels_by_style=labels_by_style,
        entry_instances=entry_instances,
    )

    actions = [action] if isinstance(action, str) else list(action)
    destroyed_during_actions = []

    def choose_action():
        for action_name in actions:
            button = next(
                call.kwargs
                for call in widget_module.Button.call_args_list
                if call.kwargs.get("text") == action_name
            )
            button["command"]()
            destroyed_during_actions.append(root.destroy.called)

    root.wait_window.side_effect = choose_action
    root.destroyed_during_actions = destroyed_during_actions
    return tk_module, widget_module, root


class PermanentSetupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        self._cursor_env = {
            key: os.environ.get(key)
            for key in (
                "CURSOR_CONFIG",
                "CURSOR_SKILLS_DIR",
                "TRAE_WORK_CONFIG",
                "TRAE_WORK_SKILLS_DIR",
                "TRAE_WORK_APP_ROOT",
                "TRAE_WORK_CN_CONFIG",
                "TRAE_WORK_CN_SKILLS_DIR",
                "TRAE_WORK_CN_APP_ROOT",
                "QODER_CN_CONFIG",
                "QODER_CN_SKILLS_DIR",
                "QODER_CN_APP_ROOT",
                "TRAE_CONFIG",
                "TRAE_SKILLS_DIR",
                "TRAE_APP_ROOT",
                "TRAE_CN_CONFIG",
                "TRAE_CN_SKILLS_DIR",
                "TRAE_CN_APP_ROOT",
                "DE_CONFIG_PATH",
            )
        }
        os.environ.pop("DE_CONFIG_PATH", None)
        os.environ["CURSOR_CONFIG"] = str(
            Path(self.tmp.name) / "cursor-home" / "mcp.json"
        )
        os.environ.pop("CURSOR_SKILLS_DIR", None)
        os.environ["TRAE_WORK_CONFIG"] = str(
            Path(self.tmp.name) / "trae-work-home" / "User" / "mcp.json"
        )
        os.environ["TRAE_WORK_SKILLS_DIR"] = str(
            Path(self.tmp.name) / "no-trae-work" / "skills"
        )
        os.environ["TRAE_WORK_APP_ROOT"] = str(
            Path(self.tmp.name) / "no-trae-work" / "app"
        )
        os.environ["TRAE_WORK_CN_CONFIG"] = str(
            Path(self.tmp.name) / "trae-work-cn-home" / "User" / "mcp.json"
        )
        os.environ["TRAE_WORK_CN_SKILLS_DIR"] = str(
            Path(self.tmp.name) / "no-trae-work-cn" / "skills"
        )
        os.environ["TRAE_WORK_CN_APP_ROOT"] = str(
            Path(self.tmp.name) / "no-trae-work-cn" / "app"
        )
        os.environ["QODER_CN_CONFIG"] = str(
            Path(self.tmp.name) / "qoder-cn-home" / "settings.json"
        )
        os.environ["QODER_CN_SKILLS_DIR"] = str(
            Path(self.tmp.name) / "no-qoder-cn" / "skills"
        )
        os.environ["QODER_CN_APP_ROOT"] = str(
            Path(self.tmp.name) / "no-qoder-cn" / "app"
        )
        os.environ["TRAE_CONFIG"] = str(
            Path(self.tmp.name) / "trae-home" / "User" / "mcp.json"
        )
        os.environ["TRAE_SKILLS_DIR"] = str(
            Path(self.tmp.name) / "no-trae" / "skills"
        )
        os.environ["TRAE_APP_ROOT"] = str(
            Path(self.tmp.name) / "no-trae" / "app"
        )
        os.environ["TRAE_CN_CONFIG"] = str(
            Path(self.tmp.name) / "trae-cn-home" / "User" / "mcp.json"
        )
        os.environ["TRAE_CN_SKILLS_DIR"] = str(
            Path(self.tmp.name) / "no-trae-cn" / "skills"
        )
        os.environ["TRAE_CN_APP_ROOT"] = str(
            Path(self.tmp.name) / "no-trae-cn" / "app"
        )
        self.addCleanup(self._restore_cursor_env)
        # Every other test here asserts DESKTOP-session behaviour. Pin the macOS
        # GUI-registration gate so running this suite from inside an agent sandbox — where
        # the gate really does fire — cannot change their outcome.
        gate = mock.patch.object(
            permanent_setup, "_gui_session_blocked", return_value=None
        )
        gate.start()
        self.addCleanup(gate.stop)
        # Same reason for the in-process NSApplication gate: this suite asserts the
        # behaviour of a process Tk still owns, and must not change verdict if the
        # runner ever loads a foreign AppKit.
        tk_gate = mock.patch.object(
            permanent_setup, "_tk_creation_blocked", return_value=None
        )
        tk_gate.start()
        self.addCleanup(tk_gate.stop)
        # The form now resolves its language through i18n.resolve_locale, so pin it to en-US: these
        # tests assert the English copy/error text, and the suite must not flip to Chinese when run on
        # a zh-CN machine. The dedicated zh-CN localization test below overrides this deliberately.
        locale_pin = mock.patch.dict(os.environ, {"DE_UI_LOCALE": "en-US"})
        locale_pin.start()
        self.addCleanup(locale_pin.stop)

    def _restore_cursor_env(self):
        for key, value in self._cursor_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_config(self, payload):
        config.atomic_write_json(self.config_path, payload)

    def test_credential_dialog_refuses_a_session_that_cannot_register_a_window(self):
        """Tk aborts the WHOLE process where GUI registration is refused — measured in a real
        agent sandbox: `python3 -c "import tkinter; tkinter.Tk()"` exits 134 (128+SIGABRT).
        `except TclError` cannot catch an abort, and unlike the popup probe this runs
        IN-PROCESS, so setup would die with a crash dialog and no message. Refuse first."""

        class ExplodingTk:
            TclError = Exception

            @staticmethod
            def Tk():
                raise AssertionError("Tk() must not be constructed in a GUI-less session")

        with (
            mock.patch.object(
                permanent_setup,
                "_gui_session_blocked",
                return_value="sandboxed process (agent shell)",
            ),
            self.assertRaises(config.ShellError) as caught,
        ):
            permanent_setup._prompt_credentials_gui(ExplodingTk, SimpleNamespace())

        message = str(caught.exception)
        self.assertIn("sandboxed process", message)
        self.assertIn("desktop terminal", message)
        self.assertIn("--from-env", message)  # the only route left to a confined caller
        self.assertNotIn("=", message)  # never echo a credential VALUE, only the variable names

    def test_gui_session_gate_includes_host_owned_interactive_setup_guard(self):
        from installer.client_hosts import registry

        with (
            mock.patch.object(
                popup_backend,
                "gui_registration_blocked_reason",
                return_value=None,
            ),
            mock.patch.object(
                registry,
                "interactive_setup_blocked_reason",
                return_value="host requires an approved outside-sandbox rerun",
            ) as host_guard,
        ):
            self.assertEqual(
                _REAL_GUI_GATE(),
                "host requires an approved outside-sandbox rerun",
            )
        host_guard.assert_called_once_with()

    def test_gui_session_gate_keeps_generic_macos_sandbox_guard_first(self):
        from installer.client_hosts import registry

        with (
            mock.patch.object(
                popup_backend,
                "gui_registration_blocked_reason",
                return_value="sandboxed process (agent shell)",
            ),
            mock.patch.object(
                registry,
                "interactive_setup_blocked_reason",
            ) as host_guard,
        ):
            self.assertEqual(
                _REAL_GUI_GATE(),
                "sandboxed process (agent shell)",
            )
        host_guard.assert_not_called()

    def test_webview_render_gate_routes_straight_to_tk_before_webview_starts(self):
        """Measured inside WorkBuddy's Windows sandbox: WebView2 paints a dead black
        frame that neither raises nor falls back, so the gate must route to the Tk
        form BEFORE webview is ever started — an exception-driven fallback alone
        can never fire there."""

        def explode(**_kwargs):
            raise AssertionError("webview must not be started once its render gate fires")

        class ForbiddenWebview:
            create_window = staticmethod(explode)
            start = staticmethod(explode)

        with (
            mock.patch.object(
                permanent_setup,
                "_gui_session_blocked",
                return_value=None,
            ),
            mock.patch.object(
                permanent_setup,
                "_webview_render_blocked",
                return_value="WorkBuddy's Windows sandbox cannot reliably render the "
                "permanent setup WebView",
            ),
            mock.patch.dict(sys.modules, {"webview": ForbiddenWebview}),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_tk",
                return_value=("https://owner.example", "owner_invite_value"),
            ) as fallback,
        ):
            result = permanent_setup._prompt_credentials_gui()

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        fallback.assert_called_once_with(strings=mock.ANY)

    def test_webview_render_gate_stays_inert_when_no_host_blocks_webview(self):
        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return mock.Mock()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("renderer unavailable")

        with (
            mock.patch.object(
                permanent_setup,
                "_gui_session_blocked",
                return_value=None,
            ),
            mock.patch.object(
                permanent_setup,
                "_webview_render_blocked",
                return_value=None,
            ) as render_gate,
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_tk",
                return_value=("https://owner.example", "owner_invite_value"),
            ) as fallback,
        ):
            result = permanent_setup._prompt_credentials_gui(
                webview_module=FakeWebview
            )

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        render_gate.assert_not_called()  # an injected webview_module already chose its backend
        fallback.assert_called_once_with(strings=mock.ANY)

    def test_status_dialog_is_skipped_rather_than_aborting_a_gui_less_session(self):
        # The fake's TclError must be a NARROW class: with `TclError = Exception` the guard's
        # own `except tk.TclError` swallows the tripwire below and this test passes even with
        # the guard deleted (a false green — caught in audit aud_qTVAfWmxsCpHvP3V).
        class _FakeTclError(Exception):
            pass

        exploding = SimpleNamespace(
            TclError=_FakeTclError,
            messagebox=SimpleNamespace(
                showinfo=lambda *a, **k: None, showerror=lambda *a, **k: None
            ),
        )

        def explode():
            raise AssertionError("Tk() must not be constructed in a GUI-less session")

        exploding.Tk = explode
        with (
            mock.patch.object(
                permanent_setup,
                "_gui_session_blocked",
                return_value="sandboxed process (agent shell)",
            ),
            mock.patch.dict(
                sys.modules,
                {"tkinter": exploding, "tkinter.messagebox": exploding.messagebox},
            ),
        ):
            permanent_setup._show_gui_message("title", "message")

    def test_gui_combines_both_fields_in_one_modal_and_masks_secret(self):
        tk_module, widgets, root = _credential_tk_double(
            ["https://owner.example", "owner_invite_value"]
        )

        result = permanent_setup._prompt_credentials_gui(tk_module, widgets)

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        entries = widgets.Entry.call_args_list
        self.assertEqual(len(entries), 2)
        self.assertNotIn("show", entries[0].kwargs)
        self.assertEqual(entries[1].kwargs["show"], "•")
        self.assertEqual(entries[0].kwargs["validate"], "key")
        self.assertEqual(entries[1].kwargs["validate"], "key")
        endpoint_check = entries[0].kwargs["validatecommand"][0]
        secret_check = entries[1].kwargs["validatecommand"][0]
        self.assertTrue(endpoint_check("x" * 2048))
        self.assertFalse(endpoint_check("x" * 2049))
        self.assertTrue(secret_check("x" * 4096))
        self.assertFalse(secret_check("x" * 4097))
        tk_module.Tk.assert_called_once_with()
        self.assertLess(
            root.method_calls.index(mock.call.deiconify()),
            root.method_calls.index(mock.call.wait_visibility()),
        )
        self.assertLess(
            root.method_calls.index(mock.call.wait_visibility()),
            root.method_calls.index(mock.call.grab_set()),
        )
        root.grab_set.assert_called_once_with()
        root.wait_window.assert_called_once_with()
        root.destroy.assert_called_once_with()

    def test_tk_fallback_uses_ttk_widgets(self):
        tk_module, widgets, root = _credential_tk_double(
            ["https://owner.example", "owner_invite_value"]
        )
        tk_module.ttk = widgets

        with mock.patch.dict(sys.modules, {"tkinter": tk_module}):
            result = permanent_setup._prompt_credentials_tk()

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        widgets.Style.assert_called_once_with(root)
        self.assertEqual(len(widgets.Entry.call_args_list), 2)
        endpoint_options = widgets.Entry.call_args_list[0].kwargs
        endpoint_options["textvariable"].set.assert_called_once_with(
            "https://endpoint.deeppattern.ai"
        )
        self.assertEqual(endpoint_options.get("state", "normal"), "normal")

    def test_tk_form_submits_default_endpoint_when_unedited(self):
        tk_module, widgets, _root = _credential_tk_double([None, "owner_invite_value"])

        result = permanent_setup._prompt_credentials_tk(tk_module, widgets)

        self.assertEqual(result, ("https://endpoint.deeppattern.ai", "owner_invite_value"))

    def test_tk_form_keeps_invalid_required_input_open_with_feedback(self):
        tk_module, widgets, root = _credential_tk_double(
            ["   ", "owner_invite_value"]
        )

        result = permanent_setup._prompt_credentials_tk(tk_module, widgets)

        self.assertIsNone(result)
        widgets.labels_by_style["Error.TLabel"][0].configure.assert_called_once_with(
            text="Endpoint is required."
        )
        widgets.entry_instances[0].focus_set.assert_called()
        self.assertEqual(root.destroyed_during_actions, [False])

    def test_tk_form_covers_invalid_endpoint_and_blank_key_feedback(self):
        cases = (
            (
                ["http://owner.example", "owner_invite_value"],
                "Enter a valid HTTPS endpoint.",
                0,
            ),
            (
                ["https://owner.example", "   "],
                "Activation key is required.",
                1,
            ),
            (
                ["https://owner.example", "x" * 4097],
                "Activation key is too long.",
                1,
            ),
        )
        for answers, message, focused_entry in cases:
            with self.subTest(message=message):
                tk_module, widgets, root = _credential_tk_double(answers)

                result = permanent_setup._prompt_credentials_tk(
                    tk_module, widgets
                )

                self.assertIsNone(result)
                widgets.labels_by_style["Error.TLabel"][0].configure.assert_called_once_with(
                    text=message
                )
                widgets.entry_instances[focused_entry].focus_set.assert_called()
                self.assertEqual(root.destroyed_during_actions, [False])

    def test_tk_form_accepts_corrected_values_in_the_same_window(self):
        tk_module, widgets, root = _credential_tk_double(
            [
                ["   ", "owner.example"],
                ["owner_invite_value", "owner_invite_value"],
            ],
            action=["Activate", "Activate"],
        )

        result = permanent_setup._prompt_credentials_tk(tk_module, widgets)

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        self.assertEqual(root.destroyed_during_actions, [False, True])

    def test_tk_fallback_refuses_legacy_tk_that_renders_blank(self):
        class LegacyTk:
            TkVersion = 8.5
            TclError = RuntimeError
            Tk = mock.Mock(side_effect=AssertionError("must refuse before Tk()"))

        with self.assertRaises(config.ShellError) as ctx:
            permanent_setup._prompt_credentials_tk(LegacyTk, SimpleNamespace())

        self.assertIn("8.6", str(ctx.exception))
        LegacyTk.Tk.assert_not_called()

    def test_gui_prefers_webview_combined_form_and_masks_secret(self):
        captured = {}
        destroyed = threading.Event()

        class FakeWindow:
            def destroy(self):
                destroyed.set()

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured.update(kwargs)
                return FakeWindow()

            @staticmethod
            def start(**kwargs):
                captured["start_kwargs"] = kwargs
                captured["js_api"].submit(
                    "https://owner.example", "owner_invite_value"
                )
                # start() returns when the window closes, and closing it is the watcher's job
                captured["destroyed"] = destroyed.wait(5)

        result = permanent_setup._prompt_credentials_gui(
            webview_module=FakeWebview
        )

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        self.assertIn('id="endpoint"', captured["html"])
        self.assertIn('id="secret" type="password"', captured["html"])
        self.assertIn('id="form-error"', captured["html"])
        self.assertIn('id="endpoint" required', captured["html"])
        endpoint_input = captured["html"].split('<input id="endpoint"', 1)[1].split(">", 1)[0]
        self.assertIn('value="https://endpoint.deeppattern.ai"', endpoint_input)
        self.assertNotIn("readonly", endpoint_input)
        self.assertNotIn("disabled", endpoint_input)
        self.assertIn('id="secret" type="password" required', captured["html"])
        self.assertIn("Owner-issued endpoint", captured["html"])
        self.assertIn("Activation key", captured["html"])
        self.assertNotIn('id="cancel" disabled', captured["html"])
        self.assertNotIn('id="activate" disabled', captured["html"])
        self.assertIn("result&&result.ok===false", captured["html"])
        self.assertIn("catch(_bridgeError)", captured["html"])
        self.assertIn("overflow-y:auto", captured["html"])
        self.assertTrue(captured["destroyed"])
        self.assertEqual(captured["start_kwargs"]["private_mode"], True)
        self.assertEqual(captured["height"], 560)
        self.assertIs(captured["resizable"], True)

    def test_webview_form_marks_activation_busy_and_reenables_after_feedback(self):
        html = permanent_setup._render_credential_form_html(
            permanent_setup._setup_strings("en-US")
        )

        self.assertIn("if(submitting)return", html)
        # The busy label now comes from the injected string dict (S.activating / S.activate) rather
        # than a JS literal, and the localized value is baked into that dict.
        self.assertIn("activate.textContent=busy?S.activating:S.activate", html)
        self.assertIn("Activating\\u2026", html)  # "Activating…" JSON-escaped into the S dict
        self.assertIn("endpoint.disabled=busy", html)
        self.assertIn("secret.disabled=busy", html)
        self.assertIn("cancel.disabled=busy", html)
        self.assertIn("activate.disabled=busy", html)
        self.assertIn("if(result&&result.ok===false){setBusy(false);", html)
        self.assertIn("catch(_bridgeError){setBusy(false);", html)
        self.assertNotIn("finally{setBusy(false)}", html)

    def test_webview_form_renders_the_resolved_locale_and_localizes_the_title(self):
        """One resolved locale drives the page copy AND the OS window title; no English leaks and no
        substitution sentinel survives. Driven with the zh-CN table so a regression that hard-codes
        English (the bug this fixes) fails loudly."""
        from client import i18n

        captured = {}
        destroyed = threading.Event()

        class FakeWindow:
            def destroy(self):
                destroyed.set()

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured.update(kwargs)
                return FakeWindow()

            @staticmethod
            def start(**_kwargs):
                captured["js_api"].submit("https://owner.example", "owner_invite_value")
                destroyed.wait(5)

        zh = i18n.permanent_setup("zh-CN")
        result = permanent_setup._prompt_credentials_webview(
            FakeWebview, strings=zh
        )

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        self.assertEqual(captured["title"], zh["window_title"])
        self.assertIn(zh["secret_label"], captured["html"])       # 激活密钥
        self.assertIn(zh["subtitle"], captured["html"])           # 将此设备连接到你的工作区
        self.assertIn(zh["endpoint_hint"], captured["html"])
        self.assertNotIn("Activation key", captured["html"])
        self.assertNotIn("Owner-issued endpoint", captured["html"])
        self.assertNotIn("__DE_", captured["html"])               # every sentinel substituted
        self.assertIn('value="https://endpoint.deeppattern.ai"', captured["html"])

    def test_render_is_single_pass_so_a_poisoned_slot_cannot_re_enter_substitution(self):
        """Defense-in-depth the docstring promises: a slot value that itself contains a substitution
        sentinel (only reachable by editing client/locales/*.py) must be inserted as inert, escaped
        DATA — never rescanned to splice the JS bundle's raw-quote JSON into an HTML attribute. This
        pins the single-pass render against the cascading-.replace() breakout a deep audit flagged.
        The payload embeds __DE_SETUP_STRINGS__ (the JS-bundle sentinel) plus an attribute-breakout
        try in secret_placeholder, which lands in placeholder="…" — the one quoted-attribute slot."""
        from html.parser import HTMLParser

        poisoned = json.loads(json.dumps(permanent_setup._setup_strings("en-US")))
        poisoned["secret_placeholder"] = 'x__DE_SETUP_STRINGS__" onfocus="alert(1)'

        html_out = permanent_setup._render_credential_form_html(poisoned)

        # The JS bundle (its telltale key) is emitted EXACTLY once — inside <script>, never a second
        # time spliced into the poisoned attribute by a re-entrant replace.
        self.assertEqual(html_out.count('"activate":'), 1)
        # The sentinel carried IN by the poison is inserted as inert, escaped data: the quote is
        # neutralized (&quot;) so it cannot close the attribute, and the sentinel text is NOT expanded
        # (single pass — the replacement is never rescanned). Its survival here as escaped data is the
        # secure outcome; the clean-render test asserts no sentinel survives a legitimate table.
        self.assertIn('placeholder="x__DE_SETUP_STRINGS__&quot; onfocus=&quot;alert(1)"', html_out)
        self.assertNotIn('onfocus="alert(1)"', html_out)  # the raw, un-escaped breakout never forms

        # Parse the DOM and prove the injected on* handler did not become a real attribute on <input>.
        injected = []

        class _Probe(HTMLParser):
            def handle_starttag(self, tag, attrs):
                for name, _value in attrs:
                    if name.startswith("on"):
                        injected.append((tag, name))

        _Probe().feed(html_out)
        self.assertEqual(injected, [])  # onfocus never escaped the quoted placeholder value

    def test_render_localizes_the_document_lang_attribute(self):
        """<html lang> must follow the resolved locale so the webview picks CJK fonts and a screen
        reader picks the right voice — a zh form inside lang="en" renders Han glyphs with the wrong
        font. en → "en"; zh → "zh-Hans" (the BCP-47 subtag, distinct from the resolver's locale tag)."""
        from client import i18n

        en_html = permanent_setup._render_credential_form_html(i18n.permanent_setup("en-US"))
        zh_html = permanent_setup._render_credential_form_html(i18n.permanent_setup("zh-CN"))

        self.assertIn('<html lang="en">', en_html)
        self.assertIn('<html lang="zh-Hans">', zh_html)

    def test_webview_bridge_reports_errors_in_the_resolved_locale(self):
        from client import i18n

        zh = i18n.permanent_setup("zh-CN")
        api = permanent_setup._CredentialFormApi(errors=zh["errors"])

        self.assertEqual(
            api.submit("   ", "owner_invite_value"),
            {"ok": False, "field": "endpoint", "error": zh["errors"]["endpoint_required"]},
        )
        self.assertEqual(
            api.submit("https://owner.example", "x" * 4097),
            {"ok": False, "field": "secret", "error": zh["errors"]["secret_too_long"]},
        )

    def test_tk_form_renders_the_resolved_locale_and_localizes_error_feedback(self):
        """The Tk fallback resolves the same table: title, labels, buttons, and the retry text all
        come from zh-CN here. The button is clicked by its localized text, proving it was relabeled."""
        from client import i18n

        zh = i18n.permanent_setup("zh-CN")
        tk_module, widgets, root = _credential_tk_double(
            ["   ", "owner_invite_value"], action=zh["activate"]
        )

        result = permanent_setup._prompt_credentials_tk(tk_module, widgets, strings=zh)

        self.assertIsNone(result)
        root.title.assert_called_once_with(zh["window_title"])
        widgets.labels_by_style["Error.TLabel"][0].configure.assert_called_once_with(
            text=zh["errors"]["endpoint_required"]
        )
        header_titles = [
            call.kwargs.get("text") for call in widgets.Label.call_args_list
        ]
        self.assertIn(zh["title"], header_titles)
        self.assertIn(zh["secret_label"], header_titles)
        self.assertNotIn("Activation key", header_titles)

    def test_the_setup_window_claims_the_product_icon(self):
        """The first window a new device ever sees. pywebview's Windows backend is a WinForms host
        with no `icon` argument, so without WM_SETICON the activation dialog and its taskbar button
        are branded as Python — which is exactly what a fresh Windows install showed."""
        applied = []

        class FakeEvents:
            def __init__(self):
                self.loaded = []

        class FakeWindow:
            def __init__(self):
                self.events = FakeEvents()
                self.native = type("Form", (), {"Handle": 0xBEEF})()

            def destroy(self):
                pass

        window = FakeWindow()
        with mock.patch("client.tk_icon.apply_taskbar_icon",
                        side_effect=lambda h: applied.append(h) or True), \
                mock.patch("client.tk_icon.apply_dock_icon", return_value=False):
            permanent_setup._apply_webview_icon(window)
        self.assertEqual(applied, [0xBEEF])

    def test_the_dialog_registers_the_icon_on_loaded(self):
        """The handler has to be attached to the window, not just exist. pywebview only realises
        the native handle when the page loads, so nothing earlier can read it."""
        registered = []

        class FakeEvents:
            def __init__(self):
                self.loaded = self

            def __iadd__(self, handler):
                registered.append(handler)
                return self

        class FakeWindow:
            def __init__(self):
                self.events = FakeEvents()

            def destroy(self):
                pass

        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return FakeWindow()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("stop after wiring")

        with self.assertRaises(RuntimeError):
            permanent_setup._prompt_credentials_webview(FakeWebview)
        self.assertEqual(len(registered), 1, "no loaded handler was attached to the setup window")

    def test_the_setup_window_is_closed_by_the_watcher_not_the_form(self):
        """End to end over the backend seam: the form submits from pywebview's own bridge thread,
        and the window still comes down — from the watcher, once that thread is finished."""
        destroyed = threading.Event()
        captured = {}

        class FakeEvents:
            def __init__(self):
                self.loaded = self

            def __iadd__(self, _handler):
                return self

        class FakeWindow:
            def __init__(self):
                self.events = FakeEvents()

            def destroy(self):
                destroyed.set()

        window = FakeWindow()

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured["api"] = kwargs["js_api"]
                return window

            @staticmethod
            def start(**_kwargs):
                bridge = threading.Thread(
                    target=lambda: captured["api"].submit(
                        "https://owner.example", "owner_invite_value"),
                )
                bridge.start()
                bridge.join(5)
                self.assertTrue(destroyed.wait(5), "the watcher never closed the setup window")

        self.assertEqual(
            permanent_setup._prompt_credentials_webview(FakeWebview),
            ("https://owner.example", "owner_invite_value"),
        )

    def test_the_setup_window_icon_is_applied_once(self):
        """`loaded` fires once per navigation; each LoadImageW hands back an HICON nothing frees."""
        calls = []

        class FakeWindow:
            native = type("Form", (), {"Handle": 0xBEEF})()

        window = FakeWindow()
        with mock.patch("client.tk_icon.apply_taskbar_icon",
                        side_effect=lambda h: calls.append(h) or True), \
                mock.patch("client.tk_icon.apply_dock_icon", return_value=False):
            permanent_setup._apply_webview_icon(window)
            permanent_setup._apply_webview_icon(window)
        self.assertEqual(len(calls), 1)

    def test_a_window_without_a_handle_is_silent(self):
        with mock.patch("client.tk_icon.apply_taskbar_icon") as apply_icon, \
                mock.patch("client.tk_icon.apply_dock_icon", return_value=False):
            permanent_setup._apply_webview_icon(object())
        apply_icon.assert_not_called()

    def test_a_raising_icon_never_blocks_activation(self):
        """Activation is the product; the icon is decoration. This runs from a webview callback."""

        class FakeWindow:
            native = type("Form", (), {"Handle": 0xBEEF})()

        with mock.patch("client.tk_icon.apply_taskbar_icon", side_effect=OSError("no user32")):
            permanent_setup._apply_webview_icon(FakeWindow())   # must not raise

    def test_webview_bridge_rejects_invalid_or_oversized_credentials(self):
        api = permanent_setup._CredentialFormApi()

        self.assertEqual(
            api.submit(None, "secret"),
            {
                "ok": False,
                "field": "endpoint",
                "error": "Enter a valid HTTPS endpoint.",
            },
        )
        self.assertEqual(
            api.submit("https://owner.example", "x" * 4097),
            {
                "ok": False,
                "field": "secret",
                "error": "Activation key is too long.",
            },
        )
        self.assertIsNone(api._take_result())
        self.assertFalse(hasattr(api, "take_result"))
        self.assertFalse(hasattr(api, "attach"))
        self.assertFalse(api._is_done())

    def test_webview_bridge_requires_both_fields_without_closing(self):
        api = permanent_setup._CredentialFormApi()

        self.assertEqual(
            api.submit("   ", "owner_invite_value"),
            {
                "ok": False,
                "field": "endpoint",
                "error": "Endpoint is required.",
            },
        )
        self.assertEqual(
            api.submit("https://owner.example", "   "),
            {
                "ok": False,
                "field": "secret",
                "error": "Activation key is required.",
            },
        )
        self.assertIsNone(api._take_result())
        self.assertFalse(api._is_done())

    def test_webview_bridge_rejects_invalid_endpoint_without_closing(self):
        api = permanent_setup._CredentialFormApi()

        self.assertEqual(
            api.submit("http://owner.example", "owner_invite_value"),
            {
                "ok": False,
                "field": "endpoint",
                "error": "Enter a valid HTTPS endpoint.",
            },
        )
        self.assertIsNone(api._take_result())
        self.assertFalse(api._is_done())

    def test_webview_bridge_accepts_corrected_values_in_the_same_window(self):
        api = permanent_setup._CredentialFormApi()

        rejected = api.submit("   ", "owner_invite_value")
        accepted = api.submit("owner.example", "owner_invite_value")

        self.assertFalse(rejected["ok"])
        self.assertEqual(accepted, {"ok": True})
        self.assertEqual(
            api._take_result(),
            ("https://owner.example", "owner_invite_value"),
        )

    def test_webview_bridge_keeps_invalid_activation_key_open_with_safe_reason(self):
        secret = "wrong_owner_value"
        reflected = "server reflected " + secret

        def activate_in_form(endpoint, activation_secret):
            return permanent_setup._activate_from_form(
                endpoint,
                activation_secret,
                config_path=self.config_path,
            )

        with mock.patch.object(
            permanent_setup.activate,
            "activate_with_recovery_guard",
            side_effect=permanent_setup.activate.ActivationRefusedError(403, reflected),
        ):
            api = permanent_setup._CredentialFormApi(submit_handler=activate_in_form)
            response = api.submit("https://owner.example", secret)

        self.assertEqual(
            response,
            {
                "ok": False,
                "field": "secret",
                "error": "Activation key is invalid or expired. Check the key and try again.",
            },
        )
        self.assertFalse(api._is_done(), "a retryable server rejection must keep the form open")
        self.assertIsNone(api._take_result())
        self.assertNotIn(secret, repr(response))
        self.assertNotIn("reflected", repr(response))

    def test_webview_bridge_allows_only_one_activation_attempt_at_a_time(self):
        entered = threading.Event()
        release = threading.Event()
        responses = []
        calls = []

        def activate_in_form(_endpoint, _secret):
            calls.append("activate")
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                raise permanent_setup._CredentialFormValidationError(
                    "secret", "Activation key is invalid or expired."
                )
            return {"activated": True}

        api = permanent_setup._CredentialFormApi(submit_handler=activate_in_form)
        first = threading.Thread(
            target=lambda: responses.append(
                api.submit("https://owner.example", "wrong_owner_value")
            )
        )
        first.start()
        self.assertTrue(entered.wait(5))
        self.assertFalse(api._window_close_allowed())

        duplicate = api.submit("https://owner.example", "wrong_owner_value")
        cancelled = api.cancel()
        release.set()
        first.join(5)

        self.assertEqual(calls, ["activate"])
        self.assertEqual(
            duplicate,
            {"ok": False, "error": "Activation is already in progress."},
        )
        self.assertEqual(
            cancelled,
            {"ok": False, "error": "Activation is already in progress."},
        )
        self.assertFalse(api._is_done())
        self.assertTrue(api._window_close_allowed())
        self.assertFalse(responses[0]["ok"])

    def test_pywebview_closing_contract_vetoes_only_while_bridge_is_busy(self):
        class ClosingEvent:
            def __init__(self, handler):
                self.handler = handler

            def set(self):
                return self.handler() is False

        api = permanent_setup._CredentialFormApi()
        closing = ClosingEvent(api._window_close_allowed)

        self.assertFalse(closing.set(), "idle OS close must be allowed")
        with api._lock:
            api._submitting = True
        self.assertTrue(closing.set(), "in-flight activation must veto OS close")
        with api._lock:
            api._submitting = False
            api._delivery_pending = True
        self.assertTrue(closing.set(), "bridge result delivery must veto OS close")
        api._mark_delivery_complete()
        self.assertFalse(closing.set(), "completed delivery must allow closer teardown")

    def test_abandon_does_not_discard_an_in_flight_success(self):
        entered = threading.Event()
        release = threading.Event()
        responses = []

        def activate_in_form(_endpoint, _secret):
            entered.set()
            release.wait(5)
            return {"activated": True, "device_id": "device-1"}

        api = permanent_setup._CredentialFormApi(submit_handler=activate_in_form)
        bridge = threading.Thread(
            target=lambda: responses.append(
                api.submit("https://owner.example", "owner_invite_value")
            )
        )
        bridge.start()
        self.assertTrue(entered.wait(5))

        self.assertFalse(api._abandon())
        release.set()
        bridge.join(5)

        self.assertEqual(responses, [{"ok": True}])
        self.assertEqual(
            api._take_result(),
            {"activated": True, "device_id": "device-1"},
        )

    def test_webview_start_return_waits_for_an_in_flight_activation(self):
        """Some native backends may return their UI loop while a bridge worker is finishing."""
        entered = threading.Event()
        release = threading.Event()
        prompt_result = []
        captured = {}

        class Hook:
            def __iadd__(self, _handler):
                return self

        class FakeWindow:
            def __init__(self):
                self.events = type("Events", (), {"loaded": Hook(), "closing": Hook()})()

            def destroy(self):
                pass

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured["api"] = kwargs["js_api"]
                return FakeWindow()

            @staticmethod
            def start(**_kwargs):
                bridge = threading.Thread(
                    target=lambda: captured["api"].submit(
                        "https://owner.example", "owner_invite_value"
                    )
                )
                bridge.start()
                self.assertTrue(entered.wait(5))
                # Deliberately return before the bridge handler completes.

        def activate_in_form(_endpoint, _secret):
            entered.set()
            release.wait(5)
            return {"activated": True, "device_id": "device-1"}

        prompt = threading.Thread(
            target=lambda: prompt_result.append(
                permanent_setup._prompt_credentials_webview(
                    FakeWebview, submit_handler=activate_in_form
                )
            )
        )
        prompt.start()
        self.assertTrue(entered.wait(5))
        self.assertTrue(prompt.is_alive(), "the in-flight activation result was abandoned")

        release.set()
        prompt.join(5)

        self.assertFalse(prompt.is_alive())
        self.assertEqual(
            prompt_result,
            [{"activated": True, "device_id": "device-1"}],
        )

    def test_webview_backend_error_does_not_discard_an_in_flight_success(self):
        entered = threading.Event()
        release = threading.Event()
        captured = {}

        class Hook:
            def __iadd__(self, _handler):
                return self

        class FakeWindow:
            def __init__(self):
                self.events = type("Events", (), {"loaded": Hook(), "closing": Hook()})()

            def destroy(self):
                pass

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured["api"] = kwargs["js_api"]
                return FakeWindow()

            @staticmethod
            def start(**_kwargs):
                threading.Thread(
                    target=lambda: captured["api"].submit(
                        "https://owner.example", "owner_invite_value"
                    )
                ).start()
                self.assertTrue(entered.wait(5))
                threading.Timer(0.05, release.set).start()
                raise RuntimeError("native backend exited")

        def activate_in_form(_endpoint, _secret):
            entered.set()
            release.wait(5)
            return {"activated": True, "device_id": "device-1"}

        self.assertEqual(
            permanent_setup._prompt_credentials_webview(
                FakeWebview, submit_handler=activate_in_form
            ),
            {"activated": True, "device_id": "device-1"},
        )

    def test_webview_bridge_accepts_a_corrected_key_after_server_rejection(self):
        attempts = []

        def activate_in_form(_endpoint, secret):
            attempts.append(secret)
            if len(attempts) == 1:
                raise permanent_setup._CredentialFormValidationError(
                    "secret",
                    "Activation key is invalid or expired. Check the key and try again.",
                )
            return {"activated": True, "device_id": "device-1"}

        api = permanent_setup._CredentialFormApi(submit_handler=activate_in_form)

        rejected = api.submit("https://owner.example", "wrong_owner_value")
        accepted = api.submit("https://owner.example", "correct_owner_value")

        self.assertFalse(rejected["ok"])
        self.assertEqual(accepted, {"ok": True})
        self.assertEqual(attempts, ["wrong_owner_value", "correct_owner_value"])
        self.assertEqual(
            api._take_result(),
            {"activated": True, "device_id": "device-1"},
        )

    def test_webview_bridge_closes_on_recovery_required_instead_of_retrying(self):
        failure = permanent_setup.ActivationRecoveryRequiredError(
            "owner-guided recovery required"
        )
        api = permanent_setup._CredentialFormApi(
            submit_handler=mock.Mock(side_effect=failure)
        )

        response = api.submit("https://owner.example", "owner_invite_value")

        self.assertEqual(response, {"ok": True})
        self.assertTrue(api._is_done())
        with self.assertRaises(permanent_setup.ActivationRecoveryRequiredError):
            api._take_result()

    def test_webview_bridge_cancel_is_one_shot(self):
        api = permanent_setup._CredentialFormApi()

        self.assertEqual(api.cancel(), {"ok": True})
        self.assertEqual(
            api.submit("https://owner.example", "owner_invite_value"),
            {"ok": False},
        )
        self.assertIsNone(api._take_result())

    def test_the_bridge_owns_no_window_and_closes_nothing(self):
        """The deadlock that shipped: closing the window from inside a ``js_api`` handler queues
        the teardown ahead of pywebview's own return-value delivery, which on cocoa and GTK blocks
        the calling thread on a semaphore the dead webview can no longer release. The form freezes
        mid-submit and the non-daemon bridge thread then blocks interpreter shutdown forever."""
        api = permanent_setup._CredentialFormApi()

        self.assertEqual(
            api.submit("https://owner.example", "owner_invite_value"),
            {"ok": True},
        )
        self.assertFalse(
            [name for name in vars(api) if "window" in name.lower()],
            "the form bridge must not hold the window it would be tempted to destroy",
        )

    def test_the_window_closes_only_after_the_bridge_call_returns(self):
        """pywebview delivers the ``js_api`` result on the same thread, after the handler returns.
        The window may not go down until that thread is done with the page."""
        api = permanent_setup._CredentialFormApi()
        order = []
        returned = threading.Event()
        window = mock.Mock()
        window.destroy.side_effect = lambda: order.append("destroyed")

        def bridge_call():
            api.submit("https://owner.example", "owner_invite_value")
            returned.wait(5)            # stands in for pywebview's blocking result delivery
            order.append("bridge-returned")

        bridge = threading.Thread(target=bridge_call)
        closer = threading.Thread(
            target=permanent_setup._close_form_when_settled, args=(api, window), daemon=True
        )
        bridge.start()
        closer.start()
        returned.set()
        bridge.join(5)
        closer.join(5)

        self.assertEqual(order, ["bridge-returned", "destroyed"])

    def test_a_slow_bridge_keeps_its_window_until_result_delivery_finishes(self):
        """Never reintroduce the macOS deadlock with a timed, premature destroy."""
        api = permanent_setup._CredentialFormApi()
        window = mock.Mock()
        release_delivery = threading.Event()
        bridge_ready = threading.Event()
        join_entered = threading.Event()
        self.addCleanup(release_delivery.set)

        def bridge_call():
            api.submit("https://owner.example", "owner_invite_value")
            bridge_ready.set()
            release_delivery.wait(30)

        bridge = threading.Thread(target=bridge_call)
        bridge.start()
        self.assertTrue(bridge_ready.wait(5))
        original_join = bridge.join

        def observed_join():
            join_entered.set()
            original_join()

        bridge.join = observed_join
        closer = threading.Thread(
            target=permanent_setup._close_form_when_settled,
            args=(api, window),
            daemon=True,
        )
        closer.start()
        self.assertTrue(join_entered.wait(5))
        window.destroy.assert_not_called()

        release_delivery.set()
        closer.join(5)

        self.assertFalse(closer.is_alive())
        window.destroy.assert_called_once_with()
        self.addCleanup(original_join, 5)

    def test_a_cancelled_form_closes_too(self):
        api = permanent_setup._CredentialFormApi()
        window = mock.Mock()

        api.cancel()
        permanent_setup._close_form_when_settled(api, window)

        window.destroy.assert_called_once_with()
        self.assertIsNone(api._take_result())

    def test_abandon_wakes_the_closer_without_destroying_an_os_closed_window(self):
        api = permanent_setup._CredentialFormApi()
        window = mock.Mock()
        closer = threading.Thread(
            target=permanent_setup._close_form_when_settled,
            args=(api, window),
            daemon=True,
        )
        closer.start()

        self.assertTrue(api._abandon())
        closer.join(1)

        self.assertFalse(closer.is_alive())
        window.destroy.assert_not_called()

    def test_webview_runtime_failure_falls_back_to_tk(self):
        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return mock.Mock()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("renderer unavailable")

        with mock.patch.object(
            permanent_setup,
            "_prompt_credentials_tk",
            return_value=("https://owner.example", "owner_invite_value"),
        ) as fallback:
            result = permanent_setup._prompt_credentials_gui(
                webview_module=FakeWebview
            )

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        fallback.assert_called_once_with(strings=mock.ANY)

    def test_terminal_activation_failure_never_falls_back_to_tk(self):
        destroyed = threading.Event()
        captured = {}

        class FakeEvents:
            def __init__(self):
                self.loaded = self
                self.closing = self

            def __iadd__(self, _handler):
                return self

        class FakeWindow:
            events = FakeEvents()

            def destroy(self):
                destroyed.set()

        class FakeWebview:
            @staticmethod
            def create_window(**kwargs):
                captured["api"] = kwargs["js_api"]
                return FakeWindow()

            @staticmethod
            def start(**_kwargs):
                bridge = threading.Thread(
                    target=lambda: captured["api"].submit(
                        "https://owner.example", "owner_invite_value"
                    )
                )
                bridge.start()
                bridge.join(5)
                destroyed.wait(5)

        failure = permanent_setup.ActivationRecoveryRequiredError(
            "owner-guided recovery required"
        )
        with (
            mock.patch.object(
                permanent_setup, "_prompt_credentials_tk"
            ) as fallback,
            self.assertRaises(permanent_setup.ActivationRecoveryRequiredError),
        ):
            permanent_setup._prompt_credentials_gui(
                webview_module=FakeWebview,
                submit_handler=mock.Mock(side_effect=failure),
            )
        fallback.assert_not_called()

    def test_webview_failure_does_not_fall_back_to_tk_it_cannot_construct(self):
        """The fallback is only safe while nothing else owns the process NSApplication.

        pywebview's cocoa backend installs a plain ``NSApplication``; Tk 9.0 then aborts
        the WHOLE process inside ``Tkapp_New`` (crash Python-2026-08-26-213325.ips) looking
        for a ``TKApplication``-only selector. An abort is not catchable, so the fallback
        has to be refused BEFORE it is attempted, with the one route a caller has left."""

        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return mock.Mock()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("renderer unavailable")

        def explode():
            raise AssertionError("Tk() must not be constructed once webview owns NSApp")

        with (
            mock.patch.object(
                permanent_setup,
                "_tk_creation_blocked",
                return_value="another framework already registered this process",
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_tk", side_effect=explode),
            self.assertRaises(config.ShellError) as caught,
        ):
            permanent_setup._prompt_credentials_gui(webview_module=FakeWebview)

        message = str(caught.exception)
        self.assertIn("--from-env", message)  # the only route left to this caller
        self.assertNotIn("=", message)  # never echo a credential VALUE, only variable names

    def test_windows_keeps_the_tk_fallback_a_foreign_nsapp_would_cost_macos(self):
        """Windows has no NSApplication and no abort to avoid, so the gate must stay inert
        there: a pywebview failure has to reach the Tk form exactly as it did before this
        gate existed. Drives the REAL gate (not the setUp pin) with a foreign AppKit loaded,
        so moving the platform check would fail here instead of silently costing Windows
        users their only remaining activation window."""

        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return mock.Mock()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("renderer unavailable")

        with (
            mock.patch.object(permanent_setup, "_tk_creation_blocked", _REAL_TK_GATE),
            mock.patch.object(popup_backend.sys, "platform", "win32"),
            mock.patch.dict(
                sys.modules, {"AppKit": SimpleNamespace(NSApp=lambda: object())}
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_tk",
                return_value=("https://owner.example", "owner_invite_value"),
            ) as fallback,
        ):
            result = permanent_setup._prompt_credentials_gui(webview_module=FakeWebview)

        self.assertEqual(result, ("https://owner.example", "owner_invite_value"))
        fallback.assert_called_once_with(strings=mock.ANY)

    def test_status_dialog_is_skipped_rather_than_aborting_a_webview_owned_process(self):
        """The crash the user actually hit: activation finished, then the status/error box
        constructed Tk in the process pywebview had already claimed and aborted it — taking
        the exit code with it, so a SUCCESSFUL activation still looked like a failed install."""

        class _FakeTclError(Exception):
            pass

        def explode():
            raise AssertionError("Tk() must not be constructed once webview owns NSApp")

        exploding = SimpleNamespace(
            TclError=_FakeTclError,
            Tk=explode,
            messagebox=SimpleNamespace(
                showinfo=lambda *a, **k: None, showerror=lambda *a, **k: None
            ),
        )
        with (
            mock.patch.object(
                permanent_setup,
                "_tk_creation_blocked",
                return_value="another framework already registered this process",
            ),
            mock.patch.dict(
                sys.modules,
                {"tkinter": exploding, "tkinter.messagebox": exploding.messagebox},
            ),
        ):
            permanent_setup._show_gui_message("title", "message", error=True)

    def test_both_gui_failures_do_not_reflect_secret(self):
        secret = "owner_invite_value"

        class FakeWebview:
            @staticmethod
            def create_window(**_kwargs):
                return mock.Mock()

            @staticmethod
            def start(**_kwargs):
                raise RuntimeError("backend reflected " + secret)

        with (
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_tk",
                side_effect=RuntimeError("tk reflected " + secret),
            ),
            self.assertRaises(config.ShellError) as ctx,
        ):
            permanent_setup._prompt_credentials_gui(webview_module=FakeWebview)

        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("reflected", str(ctx.exception))

    def test_cancel_before_secret_returns_none_and_does_not_configure(self):
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui", return_value=None),
            mock.patch.object(permanent_setup.activate, "activate_with_credentials") as activate_device,
            mock.patch.object(permanent_setup, "_configure_agent_hosts") as write_mcp,
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.cancelled)
        activate_device.assert_not_called()
        write_mcp.assert_not_called()

    def test_gui_cancel_destroys_window_and_returns_none(self):
        tk_module, widgets, root = _credential_tk_double(
            ["https://owner.example", "owner_invite_value"], action="Cancel"
        )

        result = permanent_setup._prompt_credentials_gui(tk_module, widgets)

        self.assertIsNone(result)
        root.destroy.assert_called_once_with()

    def test_already_activated_skips_prompt_and_repairs_wiring(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "device_id": "device-1",
                "access_token": "device-token",
                "api_key": "legacy_invitation_value",
            }
        )
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
            mock.patch.object(permanent_setup.activate, "activate_with_credentials") as activate_device,
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ) as write_mcp,
            mock.patch.object(permanent_setup.doctor, "main", return_value=0) as run_doctor,
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.already_activated)
        self.assertTrue(result.permanent)
        prompt.assert_not_called()
        activate_device.assert_not_called()
        write_mcp.assert_called_once_with(self.config_path)
        run_doctor.assert_called_once_with([])
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", persisted)

    def test_cursor_setup_reuses_activation_and_repairs_skills_before_doctor(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "device_id": "device-1",
                "access_token": "device-token",
            }
        )
        cursor_config = Path(os.environ["CURSOR_CONFIG"])
        cursor_config.parent.mkdir(parents=True)
        events = []

        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
            mock.patch.object(
                permanent_setup.activate, "activate_with_credentials"
            ) as activate_device,
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                side_effect=lambda path: events.append(("hosts", path))
                or permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "main",
                side_effect=lambda argv: events.append(("doctor", tuple(argv))) or 0,
            ),
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.already_activated)
        prompt.assert_not_called()
        activate_device.assert_not_called()
        self.assertEqual(
            events,
            [
                ("hosts", self.config_path),
                ("doctor", ()),
            ],
        )

    def test_new_activation_runs_in_order_without_leaking_secret(self):
        events = []
        secret = "owner_invite_value"

        def activate_device(endpoint, activation_secret, *, config_path, **_kwargs):
            events.append(("activate", endpoint, activation_secret, config_path.name))
            return {"activated": True, "device_id": "device-1"}

        def write_mcp(path):
            events.append(("hosts", path.name))
            return permanent_setup.HostConfigurationResult()

        def run_doctor(argv):
            events.append(("doctor", tuple(argv)))
            return 0

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("owner.example/", secret),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                side_effect=activate_device,
            ),
            mock.patch.object(permanent_setup, "_configure_agent_hosts", side_effect=write_mcp),
            mock.patch.object(permanent_setup.doctor, "main", side_effect=run_doctor),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.permanent)
        self.assertEqual(
            events,
            [
                ("activate", "https://owner.example", secret, "config.json"),
                ("hosts", "config.json"),
                ("doctor", ()),
            ],
        )
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_default_gui_activates_before_the_form_closes_and_does_not_retry(self):
        secret = "owner_invite_value"

        def prompt_inside_form(*, submit_handler, strings=None):
            return submit_handler("https://owner.example", secret)

        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                side_effect=prompt_inside_form,
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_recovery_guard",
                return_value={"activated": True, "device_id": "device-1"},
            ) as activate_device,
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(permanent_setup.doctor, "main", return_value=0),
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.permanent)
        activate_device.assert_called_once_with(
            "https://owner.example",
            secret,
            config_path=self.config_path,
        )

    def test_from_env_activates_without_prompt_or_gui_and_never_prints_secret(self):
        self._write_config({})
        secret = "owner_env_value"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                return_value={"activated": True, "device_id": "device-1"},
            ) as activate_device,
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                side_effect=lambda _path: self._assert_owner_env_scrubbed()
                or permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "main",
                side_effect=lambda _argv: self._assert_owner_env_scrubbed(),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main(["--from-env"])

        self.assertEqual(exit_code, 0)
        prompt.assert_not_called()
        show_gui.assert_not_called()
        self.assertEqual(activate_device.call_args.args, ("https://owner.example", secret))
        self.assertEqual(activate_device.call_args.kwargs["config_path"], self.config_path)
        self.assertFalse(
            permanent_setup.activate.activation_recovery_marker_path(self.config_path).exists()
        )
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def _assert_owner_env_scrubbed(self):
        self.assertNotIn("DE_ENDPOINT", os.environ)
        self.assertNotIn("DE_ACTIVATION_SECRET", os.environ)
        return 0

    def test_cli_forwards_explicit_codex_and_cursor_selection_to_post_activation_wiring(self):
        result = permanent_setup.PermanentSetupResult(
            permanent=True,
            already_activated=True,
        )
        with mock.patch.object(
            permanent_setup, "run_permanent_setup", return_value=result
        ) as run:
            exit_code = permanent_setup.main(
                ["--from-env", "--client", "codex", "--client", "cursor"]
            )

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(
            from_env=True,
            clients=["codex", "cursor"],
        )

    def test_cli_renders_host_notice_after_first_or_repeated_activation(self):
        notice = (
            "WorkBuddy action required: open Connector Management > Custom "
            "connectors, find decision-engine, select Trust, then fully restart "
            "WorkBuddy or start a new session."
        )
        for already_activated in (False, True):
            with self.subTest(already_activated=already_activated):
                stdout = io.StringIO()
                result = permanent_setup.PermanentSetupResult(
                    permanent=True,
                    already_activated=already_activated,
                    post_mcp_write_notices=(notice,),
                )
                with (
                    mock.patch.object(
                        permanent_setup, "run_permanent_setup", return_value=result
                    ),
                    redirect_stdout(stdout),
                ):
                    exit_code = permanent_setup.main(["--from-env"])

                self.assertEqual(exit_code, 0)
                self.assertIn(notice, stdout.getvalue())

    def test_gui_success_dialog_renders_host_notice(self):
        notice = "host-specific manual follow-up"
        result = permanent_setup.PermanentSetupResult(
            permanent=True,
            post_mcp_write_notices=(notice,),
        )

        _title, message = permanent_setup._localized_setup_success_dialog(result)

        self.assertIn(notice, message)

    def test_run_propagates_host_notice_after_first_or_repeated_activation(self):
        notice = "host-specific manual follow-up"
        route_result = permanent_setup.install.SkillRouteRepairResult(
            {"workbuddy": ["audit"]}, {}
        )
        for already_activated in (False, True):
            with self.subTest(already_activated=already_activated):
                config_payload = (
                    {
                        "server_endpoint": "https://owner.example",
                        "device_id": "device-1",
                        "access_token": "device-token",
                    }
                    if already_activated
                    else {}
                )
                self._write_config(config_payload)
                with (
                    mock.patch.object(
                        permanent_setup,
                        "_managed_config_path",
                        return_value=self.config_path,
                    ),
                    mock.patch.object(
                        permanent_setup,
                        "_prompt_credentials_gui",
                        return_value=("https://owner.example", "owner-invite"),
                    ),
                    mock.patch.object(
                        permanent_setup.activate,
                        "activate_with_recovery_guard",
                        return_value={"activated": True, "device_id": "device-1"},
                    ),
                    mock.patch.object(
                        permanent_setup.mcp_config,
                        "write_entries",
                        return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                            ({"client": "workbuddy"},), (), (notice,)
                        ),
                    ),
                    mock.patch.object(
                        permanent_setup.install,
                        "repair_detected_skill_routes",
                        return_value=route_result,
                    ),
                    mock.patch.object(permanent_setup.doctor, "main", return_value=0),
                ):
                    result = permanent_setup.run_permanent_setup(
                        clients=["workbuddy"]
                    )

                self.assertEqual(result.already_activated, already_activated)
                self.assertEqual(result.post_mcp_write_notices, (notice,))

    def test_from_env_scrubs_owner_values_even_when_already_activated(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "device_id": "device-1",
                "access_token": "token-1",
            }
        )
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": "unused-owner-value",
                },
                clear=False,
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                side_effect=lambda _path: self._assert_owner_env_scrubbed()
                or permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "main",
                side_effect=lambda _argv: self._assert_owner_env_scrubbed(),
            ),
        ):
            result = permanent_setup.run_permanent_setup(from_env=True)

        self.assertTrue(result.already_activated)

    def test_from_env_missing_either_value_skips_without_activation_or_gui(self):
        self._write_config({})
        for environment in (
            {"DE_ENDPOINT": "", "DE_ACTIVATION_SECRET": ""},
            {"DE_ENDPOINT": "https://owner.example", "DE_ACTIVATION_SECRET": ""},
            {"DE_ENDPOINT": "", "DE_ACTIVATION_SECRET": "owner_env_value"},
        ):
            with self.subTest(environment=sorted(key for key, value in environment.items() if value)):
                stdout = io.StringIO()
                with (
                    mock.patch.object(
                        permanent_setup, "_managed_config_path", return_value=self.config_path
                    ),
                    mock.patch.dict(os.environ, environment, clear=False),
                    mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
                    mock.patch.object(
                        permanent_setup.activate, "activate_with_credentials"
                    ) as activate_device,
                    mock.patch.object(permanent_setup.mcp_config, "main") as write_mcp,
                    mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
                    redirect_stdout(stdout),
                ):
                    exit_code = permanent_setup.main(["--from-env"])

                self.assertEqual(exit_code, 0)
                self.assertIn("activation was skipped", stdout.getvalue())
                prompt.assert_not_called()
                activate_device.assert_not_called()
                write_mcp.assert_not_called()
                show_gui.assert_not_called()

    def test_from_env_rejects_plaintext_remote_before_network(self):
        self._write_config({})
        stderr = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "http://owner.example",
                    "DE_ACTIVATION_SECRET": "owner_process_value",
                },
                clear=False,
            ),
            mock.patch.object(
                permanent_setup.activate, "activate_with_credentials"
            ) as activate_device,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main(["--from-env"])

        self.assertEqual(exit_code, 1)
        activate_device.assert_not_called()
        self.assertIn("refusing to send", stderr.getvalue())
        self.assertIn("use https://", stderr.getvalue())

    def test_from_env_activation_failure_preserves_config_and_does_not_retry(self):
        original = {"device_name": "existing-device", "access_token": ""}
        self._write_config(original)
        before = self.config_path.read_bytes()
        secret = "wrong_owner_value"
        stderr = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DE_ENDPOINT": "https://owner.example",
                    "DE_ACTIVATION_SECRET": secret,
                },
                clear=False,
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                side_effect=config.ShellError("server reflected " + secret),
            ) as activate_device,
            mock.patch.object(permanent_setup.mcp_config, "main") as write_mcp,
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main(["--from-env"])

        self.assertEqual(exit_code, 1)
        activate_device.assert_called_once()
        write_mcp.assert_not_called()
        show_gui.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(
            permanent_setup.activate.activation_recovery_marker_path(self.config_path).exists()
        )
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("verify the owner-issued values and network", stderr.getvalue())

    def test_local_token_persistence_failure_forbids_automatic_retry(self):
        self._write_config({})
        secret = "owner_env_value"
        marker = permanent_setup.activate.activation_recovery_marker_path(self.config_path)
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_credentials_from_env",
                return_value=("https://owner.example", secret),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                side_effect=permanent_setup.activate.ActivationPersistenceError(
                    "synthetic local write failure"
                ),
            ) as activate_device,
        ):
            with self.assertRaises(config.ShellError) as context:
                permanent_setup.run_permanent_setup(from_env=True)
            self.assertTrue(marker.exists())
            with self.assertRaises(config.ShellError) as blocked_retry:
                permanent_setup.run_permanent_setup(from_env=True)

        activate_device.assert_called_once()
        message = str(context.exception)
        self.assertIn("may already have accepted", message)
        self.assertIn("do not retry automatically", message)
        self.assertNotIn(secret, message)
        self.assertIn("previous device activation may have reached", str(blocked_retry.exception))

    def test_local_token_persistence_failure_has_recovery_exit_code(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                side_effect=permanent_setup.ActivationRecoveryRequiredError(
                    "do not retry automatically"
                ),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main(["--from-env"])

        self.assertEqual(exit_code, permanent_setup.RECOVERY_REQUIRED_EXIT_CODE)
        self.assertIn("do not retry automatically", stderr.getvalue())
        show_gui.assert_not_called()

    def test_mcp_failure_stops_before_doctor_and_reports_stage_only(self):
        secret = "owner_invite_value"
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", secret),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                return_value={"activated": True, "device_id": "device-1"},
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                side_effect=config.ShellError(
                    "device credentials are saved, but no Agent MCP entry could be wired"
                ),
            ),
            mock.patch.object(permanent_setup.doctor, "main") as run_doctor,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()

        self.assertIn("MCP entry", str(ctx.exception))
        self.assertIn("credentials are saved", str(ctx.exception))
        self.assertNotIn(secret, str(ctx.exception))
        run_doctor.assert_not_called()

    def test_one_host_failure_keeps_other_hosts_permanently_configured(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "device_id": "device-1",
                "access_token": "device-token",
            }
        )
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(
                    (("cursor", "skill routing conflict"),)
                ),
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "run_all",
                return_value=[
                    permanent_setup.doctor.CheckResult(
                        "FAIL", "skills", "cursor route failed"
                    ),
                    permanent_setup.doctor.CheckResult("PASS", "python", "3.13"),
                ],
            ) as run_doctor,
            mock.patch.object(
                permanent_setup.doctor,
                "skill_route_failures",
                return_value={"cursor": ["audit"]},
                create=True,
            ),
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.permanent)
        self.assertEqual(result.host_failures[0][0], "cursor")
        run_doctor.assert_called_once_with()

    def test_host_configuration_deduplicates_cursor_failure_and_skips_unsafe_route(self):
        route_result = permanent_setup.install.SkillRouteRepairResult(
            {"claude": ["audit"]}, {}
        )
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["claude-code", "cursor"],
            ),
            mock.patch.object(
                permanent_setup.install,
                "retire_legacy_cursor_owned_copy",
                return_value="legacy owned-copy could not be verified and was preserved",
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "claude-code"},), ()
                ),
            ) as write_entries,
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                return_value=route_result,
            ) as repair_routes,
        ):
            configuration = permanent_setup._configure_agent_hosts(self.config_path)

        self.assertEqual(
            configuration.failures,
            ((
                "cursor",
                "legacy owned-copy could not be verified and was preserved",
            ),),
        )
        write_entries.assert_called_once_with(["claude-code"])
        repair_routes.assert_called_once_with(
            self.config_path.parent, excluded_clients=frozenset({"cursor"})
        )

    def test_detected_codex_host_is_wired_alongside_the_other_hosts(self):
        # Regression lock for the shipped symptom: a machine with Codex must get a Codex MCP
        # entry from setup, not just Codex skills and routing.
        route_result = permanent_setup.install.SkillRouteRepairResult(
            {"claude": ["audit"], "codex": ["audit"]}, {}
        )
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["claude-code", "codex"],
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "claude-code"}, {"client": "codex"}), ()
                ),
            ) as write_entries,
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                return_value=route_result,
            ),
        ):
            configuration = permanent_setup._configure_agent_hosts(self.config_path)

        self.assertEqual(configuration.failures, ())
        write_entries.assert_called_once_with(["claude-code", "codex"])

    def test_host_configuration_preserves_successful_mcp_follow_up_notices(self):
        notice = "host-specific manual follow-up"
        route_result = permanent_setup.install.SkillRouteRepairResult(
            {"workbuddy": ["audit"]}, {}
        )
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["workbuddy"],
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "workbuddy"},), (), (notice,)
                ),
            ),
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                return_value=route_result,
            ),
        ):
            configuration = permanent_setup._configure_agent_hosts(
                self.config_path
            )

        self.assertEqual(configuration.failures, ())
        self.assertEqual(configuration.notices, (notice,))

    def test_explicit_codex_selection_reports_a_failed_codex_entry(self):
        # --client codex means Codex specifically; a failed write there cannot end as success.
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    (), (("codex", "client MCP configuration failed"),)
                ),
            ) as write_entries,
            mock.patch.object(
                permanent_setup.install, "repair_detected_skill_routes"
            ) as repair_routes,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._configure_agent_hosts(self.config_path, ["codex"])

        write_entries.assert_called_once_with(["codex"])
        repair_routes.assert_not_called()
        self.assertIn("no Agent MCP entry could be wired", str(ctx.exception))

    def test_codex_entry_failure_beside_a_wired_host_is_reported_not_swallowed(self):
        # Codex skills can land while its MCP write fails; that partial state must be
        # named for the user (Doctor separately reports the same host as unwired).
        route_result = permanent_setup.install.SkillRouteRepairResult(
            {"claude": ["audit"]}, {}
        )
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["claude-code", "codex"],
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "claude-code"},),
                    (("codex", "client MCP configuration failed"),),
                ),
            ),
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                return_value=route_result,
            ) as repair_routes,
        ):
            configuration = permanent_setup._configure_agent_hosts(self.config_path)

        self.assertEqual(
            configuration.failures,
            (("codex", "client MCP configuration failed"),),
        )
        repair_routes.assert_called_once_with(
            self.config_path.parent, excluded_clients=frozenset({"codex"})
        )

    def test_post_credential_skill_lock_failure_reports_saved_state_without_detail(self):
        private = "C:/private/profile/install.lock"
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["claude-code"],
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "claude-code"},), ()
                ),
            ),
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                side_effect=config.ShellError(private),
            ),
            self.assertRaises(config.ShellError) as caught,
        ):
            permanent_setup._configure_agent_hosts(self.config_path)

        self.assertIn("credentials and MCP wiring are saved", str(caught.exception))
        self.assertNotIn(private, str(caught.exception))

    def test_all_detected_skill_routes_failing_is_not_reported_as_partial_success(self):
        with (
            mock.patch.object(
                permanent_setup.mcp_config,
                "detect_clients",
                return_value=["claude-code", "cursor"],
            ),
            mock.patch.object(
                permanent_setup.install,
                "retire_legacy_cursor_owned_copy",
                return_value=None,
            ),
            mock.patch.object(
                permanent_setup.mcp_config,
                "write_entries",
                return_value=permanent_setup.mcp_config.ClientWriteBatchResult(
                    ({"client": "claude-code"}, {"client": "cursor"}), ()
                ),
            ),
            mock.patch.object(
                permanent_setup.install,
                "repair_detected_skill_routes",
                return_value=permanent_setup.install.SkillRouteRepairResult(
                    {},
                    {
                        "claude": "existing user-managed skill entry was preserved",
                        "cursor": "existing user-managed skill entry was preserved",
                    },
                ),
            ),
            self.assertRaisesRegex(
                config.ShellError, "no detected Agent skill route could be wired"
            ),
        ):
            permanent_setup._configure_agent_hosts(self.config_path)

    def test_partial_host_failure_does_not_hide_unrelated_doctor_failure(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "access_token": "tok",
                "device_id": "dev",
            }
        )
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(
                    (("cursor", "skill conflict"),)
                ),
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "run_all",
                return_value=[
                    permanent_setup.doctor.CheckResult(
                        "FAIL", "skills", "cursor and codex routes failed"
                    )
                ],
            ),
            mock.patch.object(
                permanent_setup.doctor,
                "skill_route_failures",
                return_value={"cursor": ["audit"], "codex": ["audit"]},
                create=True,
            ),
        ):
            with self.assertRaises(config.ShellError) as caught:
                permanent_setup.run_permanent_setup()

        self.assertIn("Doctor", str(caught.exception))

    def test_partial_config_accepts_structured_concurrent_already_activated_result(self):
        self._write_config({"access_token": "stale", "device_id": "", "server_endpoint": ""})
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", "owner_invite_value"),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                return_value={"activated": False, "already_activated": True},
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(permanent_setup.doctor, "main", return_value=0),
        ):
            result = permanent_setup.run_permanent_setup()

        self.assertTrue(result.permanent)

    def test_corrupt_config_fails_closed_without_prompt_or_overwrite(self):
        corrupt = "{not-json"
        self.config_path.write_text(corrupt, encoding="utf-8")
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()

        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), corrupt)
        prompt.assert_not_called()

    def test_unexpected_activation_error_is_redacted(self):
        secret = "owner_invite_value"
        reflected = "server reflected " + secret
        self._write_config({})
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", secret),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                side_effect=RuntimeError(reflected),
            ),
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()

        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("reflected", str(ctx.exception))

    def test_main_activation_failure_gui_uses_safe_zh_cn_copy(self):
        secret = "owner_invite_value"
        reflected = "server reflected " + secret
        self._write_config({})
        stderr = io.StringIO()
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", secret),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_recovery_guard",
                side_effect=RuntimeError(reflected),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, stderr.getvalue())
        shown_title, shown_message = show_gui.call_args.args
        self.assertEqual(shown_title, "Decision Engine 配置失败")
        self.assertEqual(shown_message, "激活失败，请检查 endpoint、网络与激活密钥后重试。")
        self.assertIs(show_gui.call_args.kwargs["error"], True)
        self.assertNotIn(secret, shown_message)
        self.assertNotIn("reflected", shown_message)

    def test_blank_activation_secret_stops_before_activation(self):
        self._write_config({})
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", "   "),
            ),
            mock.patch.object(
                permanent_setup.activate, "activate_with_credentials"
            ) as activate_device,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()

        self.assertIn("activation key is required", str(ctx.exception))
        activate_device.assert_not_called()

    def test_doctor_failure_reports_saved_activation_stage(self):
        self._write_config({})
        with (
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", "owner_invite_value"),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_credentials",
                return_value={"activated": True, "device_id": "device-1"},
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(permanent_setup.doctor, "main", return_value=1),
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()

        self.assertIn("credentials and MCP wiring are saved", str(ctx.exception))

    def test_main_doctor_failure_gui_uses_zh_cn_result_copy(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "access_token": "tok",
                "device_id": "dev",
            }
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(permanent_setup.doctor, "main", return_value=1),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main(["--client", "trae-work-cn"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Doctor still reports a failure", stderr.getvalue())
        show_gui.assert_called_once_with(
            "Decision Engine 配置失败",
            "设备凭据和 MCP 接线已保存，但 Doctor 仍报告失败。"
            "请修复终端输出中报告的问题，然后重新运行永久配置。",
            error=True,
        )

    def test_main_doctor_failure_gui_uses_en_us_result_copy(self):
        self._write_config(
            {
                "server_endpoint": "https://owner.example",
                "access_token": "tok",
                "device_id": "dev",
            }
        )
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="en-US"),
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_configure_agent_hosts",
                return_value=permanent_setup.HostConfigurationResult(),
            ),
            mock.patch.object(permanent_setup.doctor, "main", return_value=1),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(io.StringIO()),
        ):
            exit_code = permanent_setup.main(["--client", "trae-work-cn"])

        self.assertEqual(exit_code, 1)
        show_gui.assert_called_once_with(
            "Decision Engine setup failed",
            "Device credentials and MCP wiring are saved, but Doctor still reports "
            "a failure. Fix the item reported in the terminal output, then rerun "
            "permanent setup.",
            error=True,
        )

    def test_main_activation_incomplete_gui_uses_zh_cn_result_copy(self):
        self._write_config({})
        stderr = io.StringIO()
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup, "_managed_config_path", return_value=self.config_path
            ),
            mock.patch.object(
                permanent_setup,
                "_prompt_credentials_gui",
                return_value=("https://owner.example", "owner_invite_value"),
            ),
            mock.patch.object(
                permanent_setup.activate,
                "activate_with_recovery_guard",
                return_value={"activated": False, "already_activated": False},
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("permanent binding", stderr.getvalue())
        show_gui.assert_called_once_with(
            "Decision Engine \u914d\u7f6e\u5931\u8d25",
            "\u6fc0\u6d3b\u672a\u5b8c\u6210\uff0c\u8bf7\u6838\u5bf9\u8f93\u5165\u540e\u91cd\u8bd5\u3002",
            error=True,
        )

    def test_main_recovery_required_gui_uses_zh_cn_result_copy(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                side_effect=permanent_setup.ActivationRecoveryRequiredError(
                    "raw recovery diagnostic"
                ),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, permanent_setup.RECOVERY_REQUIRED_EXIT_CODE)
        self.assertIn("raw recovery diagnostic", stderr.getvalue())
        show_gui.assert_called_once_with(
            "Decision Engine \u914d\u7f6e\u9700\u8981\u6062\u590d",
            "\u670d\u52a1\u53ef\u80fd\u5df2\u7ecf\u63a5\u53d7\u4e86\u6b64\u8bbe\u5907\uff0c"
            "\u4f46\u672c\u5730\u51ed\u636e\u672a\u80fd\u4fdd\u5b58\u3002"
            "\u8bf7\u4fdd\u7559\u5f53\u524d\u5b89\u88c5\uff0c\u5e76\u4f7f\u7528 Owner "
            "\u6307\u5bfc\u7684\u6062\u590d\u6d41\u7a0b\u3002",
            error=True,
        )

    def test_main_plain_shell_error_gui_uses_zh_cn_generic_copy(self):
        raw_message = "raw installer diagnostic"
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                side_effect=config.ShellError(raw_message),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(io.StringIO()),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 1)
        show_gui.assert_called_once_with(
            "Decision Engine \u914d\u7f6e\u5931\u8d25",
            "\u914d\u7f6e\u672a\u80fd\u5b8c\u6210\u3002\u672a\u663e\u793a\u4efb\u4f55"
            "\u51ed\u636e\u8be6\u60c5\u3002\u8bf7\u67e5\u770b\u7ec8\u7aef\u8f93\u51fa\uff0c"
            "\u4fee\u590d\u62a5\u544a\u7684\u95ee\u9898\u540e\u91cd\u8bd5\u3002",
            error=True,
        )
        self.assertNotIn(raw_message, show_gui.call_args.args[1])

    def test_gui_import_failure_is_actionable(self):
        real_import = __import__

        def fail_tkinter(name, *args, **kwargs):
            if name == "tkinter":
                raise ImportError("tk unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fail_tkinter):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._prompt_credentials_tk()
        self.assertIn("tkinter", str(ctx.exception))

    def test_gui_tcl_error_is_actionable(self):
        class FakeTclError(Exception):
            pass

        class FakeTk:
            TclError = FakeTclError

            @staticmethod
            def Tk():
                raise FakeTclError("no display")

        with self.assertRaises(config.ShellError) as ctx:
            permanent_setup._prompt_credentials_gui(FakeTk, SimpleNamespace())
        self.assertIn("desktop session", str(ctx.exception))

    def test_tk_widget_tcl_error_is_actionable(self):
        class FakeTclError(Exception):
            pass

        root = mock.MagicMock()

        class FakeTk:
            TclError = FakeTclError
            Tk = mock.Mock(return_value=root)

        widgets = SimpleNamespace(
            Style=mock.Mock(side_effect=FakeTclError("bad theme"))
        )

        with self.assertRaises(config.ShellError) as ctx:
            permanent_setup._prompt_credentials_tk(FakeTk, widgets)
        self.assertIn("desktop session", str(ctx.exception))
        root.destroy.assert_called_once_with()

    def test_main_cancellation_has_distinct_nonzero_exit(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                return_value=permanent_setup.PermanentSetupResult(
                    permanent=False, cancelled=True
                ),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 2)
        self.assertIn("cancelled", stdout.getvalue())

    def test_main_success_gui_uses_zh_cn_result_copy(self):
        cases = (
            (
                permanent_setup.PermanentSetupResult(permanent=True),
                "永久配置成功。激活密钥未被保留。"
                "请完全重启 Agent；后续重启无需重复输入。",
            ),
            (
                permanent_setup.PermanentSetupResult(
                    permanent=True, already_activated=True
                ),
                "此设备已永久激活。MCP 接线和 Doctor 已就绪。"
                "请完全重启 Agent 以加载 Decision Engine。",
            ),
            (
                permanent_setup.PermanentSetupResult(
                    permanent=True,
                    host_failures=(("cursor", "skill routing conflict"),),
                ),
                "永久配置已为其他 Agent 客户端完成。"
                "这些客户端仍需修复：cursor。请完全重启已配置成功的 Agent。",
            ),
        )
        for result, expected_message in cases:
            with self.subTest(result=result):
                with (
                    mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
                    mock.patch.object(
                        permanent_setup, "run_permanent_setup", return_value=result
                    ),
                    mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
                    redirect_stdout(io.StringIO()),
                ):
                    exit_code = permanent_setup.main([])

                self.assertEqual(exit_code, 0)
                show_gui.assert_called_once_with(
                    "Decision Engine 永久配置",
                    expected_message,
                )

    def test_main_success_gui_uses_en_us_result_copy(self):
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="en-US"),
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                return_value=permanent_setup.PermanentSetupResult(
                    permanent=True,
                    host_failures=(("cursor", "skill routing conflict"),),
                ),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 0)
        show_gui.assert_called_once_with(
            "Decision Engine permanent setup",
            "Permanent setup succeeded for the other Agent clients. "
            "These clients still need repair: cursor. Fully restart the successfully "
            "configured Agents.",
        )

    def test_main_redacts_unexpected_exception(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(i18n, "resolve_locale", return_value="zh-CN"),
            mock.patch.object(
                permanent_setup,
                "run_permanent_setup",
                side_effect=RuntimeError("reflected-private-response"),
            ),
            mock.patch.object(permanent_setup, "_show_gui_message") as show_gui,
            redirect_stderr(stderr),
        ):
            exit_code = permanent_setup.main([])

        self.assertEqual(exit_code, 1)
        self.assertNotIn("reflected-private-response", stderr.getvalue())
        show_gui.assert_called_once_with(
            "Decision Engine \u914d\u7f6e\u5931\u8d25",
            "\u914d\u7f6e\u672a\u80fd\u5b8c\u6210\u3002\u672a\u663e\u793a\u4efb\u4f55"
            "\u51ed\u636e\u8be6\u60c5\u3002\u8bf7\u67e5\u770b\u7ec8\u7aef\u8f93\u51fa\uff0c"
            "\u4fee\u590d\u62a5\u544a\u7684\u95ee\u9898\u540e\u91cd\u8bd5\u3002",
            error=True,
        )

    def test_path_override_error_survives_run_without_prompt(self):
        with (
            mock.patch.dict(
                os.environ,
                {"DE_CONFIG_PATH": "/tmp/not-the-managed-config.json"},
                clear=False,
            ),
            mock.patch.object(permanent_setup, "_prompt_credentials_gui") as prompt,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup.run_permanent_setup()
        self.assertIn("DE_CONFIG_PATH", str(ctx.exception))
        prompt.assert_not_called()

    def test_endpoint_validation_is_https_and_rejects_credential_urls(self):
        self.assertEqual(
            permanent_setup._validate_endpoint("owner.example/path/"),
            "https://owner.example/path",
        )
        for invalid in (
            "http://owner.example",
            "https://user:password@owner.example",
            "https://owner.example/#fragment",
            "https://owner.example/?token=query",
            "https:///missing-host",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(config.ShellError):
                    permanent_setup._validate_endpoint(invalid)

    def test_permanent_setup_refuses_config_path_environment_override(self):
        with mock.patch.dict(
            os.environ,
            {"DE_CONFIG_PATH": "/tmp/not-the-managed-config.json"},
            clear=False,
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._managed_config_path()
        self.assertIn("refuses", str(ctx.exception))

    def test_permanent_setup_names_deeppattern_home_override(self):
        with mock.patch.dict(
            os.environ,
            {"DEEPPATTERN_HOME": str(Path(self.tmp.name) / "relocated")},
            clear=False,
        ):
            os.environ.pop("DE_CONFIG_PATH", None)
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._managed_config_path()
        self.assertIn("DEEPPATTERN_HOME", str(ctx.exception))

    @unittest.skipIf(os.name == "nt", "POSIX symlink coverage")
    def test_permanent_setup_refuses_symlinked_managed_root(self):
        target = Path(self.tmp.name) / "target"
        target.mkdir()
        (target / ".git").mkdir()
        linked = Path(self.tmp.name) / "managed-link"
        linked.symlink_to(target, target_is_directory=True)
        with (
            mock.patch.object(
                permanent_setup, "managed_component_root", return_value=linked
            ),
            mock.patch.object(permanent_setup, "de_config_path", return_value=linked / "config.json"),
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._managed_config_path()
        self.assertIn("symlink", str(ctx.exception))

    @unittest.skipIf(os.name == "nt", "POSIX symlink coverage")
    def test_permanent_setup_refuses_symlinked_config_file(self):
        root = Path(self.tmp.name) / "managed"
        root.mkdir()
        (root / ".git").mkdir()
        elsewhere = Path(self.tmp.name) / "elsewhere.json"
        elsewhere.write_text("{}\n", encoding="utf-8")
        (root / "config.json").symlink_to(elsewhere)
        with (
            mock.patch.object(
                permanent_setup, "managed_component_root", return_value=root
            ),
            mock.patch.object(permanent_setup, "de_config_path", return_value=root / "config.json"),
        ):
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._managed_config_path()
        self.assertIn("symlink", str(ctx.exception))

    def test_permanent_setup_refuses_reparse_like_config_file(self):
        root = Path(self.tmp.name) / "managed"
        root.mkdir()
        (root / ".git").mkdir()
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(
                permanent_setup, "managed_component_root", return_value=root
            ),
            mock.patch.object(permanent_setup, "de_config_path", return_value=root / "config.json"),
            mock.patch.object(
                permanent_setup.managed_install,
                "canonical_managed_root",
                return_value=root,
            ),
            mock.patch.object(
                permanent_setup.managed_install, "_is_link_like", return_value=True
            ),
        ):
            os.environ.pop("DE_CONFIG_PATH", None)
            with self.assertRaises(config.ShellError) as ctx:
                permanent_setup._managed_config_path()
        self.assertIn("reparse", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
