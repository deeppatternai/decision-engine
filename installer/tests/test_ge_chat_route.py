"""Shim routing tests for explicit legacy versus default server GE chat."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import config as installer_config
from installer import shim


class _Forwarder:
    endpoint = "https://hub.example"
    token = "sentinel-device-token"

    def __init__(self, *, fail_fetch=False):
        self.fail_fetch = fail_fetch
        self.fetched = []

    def get_ge_artifact(self, run_id, **_kwargs):
        self.fetched.append(run_id)
        if self.fail_fetch:
            raise shim.ShellError("synthetic HTTP failure")
        return {"kind": "svg", "data": "<svg/>"}


class GeChatRouteTests(unittest.TestCase):
    def _transport_with_configs(self, fixed_transport, override_transport):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            fixed_home = base / "fixed-user" / ".deeppattern"
            fixed_config = fixed_home / "decision-engine" / "config.json"
            if fixed_transport is not None:
                fixed_config.parent.mkdir(parents=True)
                fixed_config.write_text(
                    json.dumps({"ge_chat_transport": fixed_transport}), encoding="utf-8"
                )
            override = base / "override.json"
            override.write_text(
                json.dumps({"ge_chat_transport": override_transport}), encoding="utf-8"
            )
            with (
                mock.patch.object(
                    installer_config, "DEFAULT_DEEPPATTERN_HOME", fixed_home
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "DE_CONFIG_PATH": str(override),
                        "DEEPPATTERN_HOME": str(base / "env-home"),
                        "XDG_CONFIG_HOME": str(base / "xdg-home"),
                    },
                ),
            ):
                return shim._ge_chat_transport(True)

    def test_de_config_path_override_cannot_authorize_legacy(self):
        self.assertEqual(self._transport_with_configs("server", "legacy"), "server")

    def test_only_fixed_user_config_can_authorize_exact_legacy(self):
        self.assertEqual(self._transport_with_configs("legacy", "server"), "legacy")

    def test_missing_fixed_config_ignores_legacy_override_and_defaults_server(self):
        self.assertEqual(self._transport_with_configs(None, "legacy"), "server")

    def _open(self, config_data, *, popup_followup=True, spawn_result=None):
        fwd = _Forwarder()
        spawn_result = spawn_result or {"status": "open", "popup_id": "pop_1"}
        with (
            mock.patch.object(
                shim,
                "managed_component_root",
                return_value=Path("fixed-user-root"),
            ) as path,
            mock.patch.object(shim, "load_json", return_value=config_data) as load,
            mock.patch("client.popup.launcher.render_artifact_html", return_value="<html/>"),
            mock.patch("client.popup.session.build_ge_context_bundle", return_value={"legacy": True}) as context,
            mock.patch("client.popup.session.spawn", return_value=spawn_result) as spawn,
        ):
            result = shim._handle_display_call(
                fwd,
                "open_ge_popup",
                {"run_id": "ge_1", "context": "private"},
                client_host="codex",
                popup_followup=popup_followup,
            )
        return result, fwd, path, load, context, spawn

    def test_missing_and_explicit_server_use_memory_only_http_bridge(self):
        for config_data in ({}, {"ge_chat_transport": "server"}):
            with self.subTest(config=config_data):
                result, fwd, path, load, context, spawn = self._open(config_data)

                self.assertEqual(result["status"], "open")
                self.assertEqual(fwd.fetched, ["ge_1"])
                path.assert_called_once_with("decision-engine")
                load.assert_called_once_with(Path("fixed-user-root") / "config.json")
                context.assert_not_called()
                self.assertNotIn("chat_context", spawn.call_args.kwargs)
                self.assertEqual(
                    spawn.call_args.kwargs["chat_bootstrap"],
                    {
                        "schema_version": 1,
                        "kind": "ge-chat",
                        "route": "server",
                        "endpoint": "https://hub.example",
                        "device_token": "sentinel-device-token",
                        "run_id": "ge_1",
                    },
                )

    def test_exact_legacy_preserves_context_bundle_and_never_sends_http_bridge(self):
        result, _fwd, _path, _load, context, spawn = self._open(
            {"ge_chat_transport": "legacy"}
        )

        self.assertEqual(result["status"], "open")
        context.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["chat_context"], {"legacy": True})
        self.assertNotIn("chat_bootstrap", spawn.call_args.kwargs)

    def test_unknown_transport_shows_display_only_without_chat_bridge(self):
        # An unrecognized transport value never authorizes a chat bridge (fail-closed on chat),
        # but display is decoupled: the artifact popup still opens display-only.
        result, fwd, _path, _load, context, spawn = self._open(
            {"ge_chat_transport": "LEGACY"}
        )

        self.assertEqual(result["status"], "open")
        self.assertEqual(fwd.fetched, ["ge_1"])
        context.assert_not_called()
        self.assertNotIn("chat_context", spawn.call_args.kwargs)
        self.assertNotIn("chat_bootstrap", spawn.call_args.kwargs)

    def test_unreadable_user_config_shows_display_only_without_chat_bridge(self):
        # A config read error never authorizes a chat bridge (fail-closed on chat), but the
        # already-fetched artifact still displays: no bridge is attached, the popup still opens.
        for error in (
            shim.ShellError("bad config"),
            OSError("unreadable config"),
            UnicodeError("bad config encoding"),
        ):
            with self.subTest(error=type(error).__name__):
                fwd = _Forwarder()
                with (
                    mock.patch.object(
                        shim,
                        "managed_component_root",
                        return_value=Path("fixed-user-root"),
                    ),
                    mock.patch.object(shim, "load_json", side_effect=error),
                    mock.patch("client.popup.launcher.render_artifact_html", return_value="<html/>"),
                    mock.patch("client.popup.session.build_ge_context_bundle") as context,
                    mock.patch(
                        "client.popup.session.spawn",
                        return_value={"status": "open", "popup_id": "pop_1"},
                    ) as spawn,
                ):
                    result = shim._handle_display_call(
                        fwd,
                        "open_ge_popup",
                        {"run_id": "ge_1"},
                        client_host="codex",
                    )

                self.assertEqual(result["status"], "open")
                self.assertEqual(fwd.fetched, ["ge_1"])
                context.assert_not_called()
                self.assertNotIn("chat_context", spawn.call_args.kwargs)
                self.assertNotIn("chat_bootstrap", spawn.call_args.kwargs)

    def test_disabled_followup_still_serves_server_bootstrap(self):
        # popup_followup gates only the local-agent legacy route. The server route is pure hub
        # HTTP with no root-client dependency, so a host that cannot host a local Agent
        # (popup_followup=False) still gets the memory-only server bridge on the default config.
        for config_data in ({}, {"ge_chat_transport": "server"}):
            with self.subTest(config=config_data):
                result, fwd, _path, _load, context, spawn = self._open(
                    config_data, popup_followup=False
                )

                self.assertEqual(result["status"], "open")
                self.assertEqual(fwd.fetched, ["ge_1"])
                context.assert_not_called()
                self.assertNotIn("chat_context", spawn.call_args.kwargs)
                self.assertEqual(
                    spawn.call_args.kwargs["chat_bootstrap"],
                    {
                        "schema_version": 1,
                        "kind": "ge-chat",
                        "route": "server",
                        "endpoint": "https://hub.example",
                        "device_token": "sentinel-device-token",
                        "run_id": "ge_1",
                    },
                )

    def test_disabled_followup_legacy_rollback_shows_display_only(self):
        # The local-agent legacy route still requires popup_followup, so no legacy bridge is
        # handed to a non-Agent host — but display is decoupled from chat: the artifact popup
        # still opens (display-only, no chat bridge) instead of failing the whole popup.
        result, fwd, _path, _load, context, spawn = self._open(
            {"ge_chat_transport": "legacy"}, popup_followup=False
        )

        self.assertEqual(result["status"], "open")
        self.assertEqual(fwd.fetched, ["ge_1"])
        context.assert_not_called()
        self.assertNotIn("chat_context", spawn.call_args.kwargs)
        self.assertNotIn("chat_bootstrap", spawn.call_args.kwargs)

    def test_server_bridge_failure_never_retries_through_legacy(self):
        result, _fwd, _path, _load, context, spawn = self._open(
            {}, spawn_result={"status": "failed", "reason": "bridge-handoff-failed"}
        )

        self.assertEqual(result["reason"], "bridge-handoff-failed")
        self.assertEqual(spawn.call_count, 1)
        context.assert_not_called()

    def test_artifact_http_failure_never_calls_legacy_or_spawn(self):
        fwd = _Forwarder(fail_fetch=True)
        with (
            mock.patch.object(
                shim,
                "managed_component_root",
                return_value=Path("fixed-user-root"),
            ),
            mock.patch.object(shim, "load_json", return_value={}),
            mock.patch("client.popup.session.build_ge_context_bundle") as context,
            mock.patch("client.popup.session.spawn") as spawn,
        ):
            result = shim._handle_display_call(
                fwd,
                "open_ge_popup",
                {"run_id": "ge_1"},
                client_host="codex",
            )

        self.assertEqual(result["reason"], "artifact-fetch-failed")
        context.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
