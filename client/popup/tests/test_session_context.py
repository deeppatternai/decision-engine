"""Behavior tests for GE popup follow-up context construction."""

from __future__ import annotations

import unittest
from unittest import mock

from client.popup import chat_backend, session


class GeContextBundleTestCase(unittest.TestCase):
    def test_none_caller_preserves_legacy_bundle_shape(self):
        legacy = session.build_ge_context_bundle("source", "Title", "svg")
        explicit_none = session.build_ge_context_bundle(
            "source", "Title", "svg", caller=None
        )

        self.assertEqual(explicit_none, legacy)
        self.assertNotIn("caller", explicit_none)

    def test_resolved_caller_is_recorded(self):
        bundle = session.build_ge_context_bundle(
            "source", "Title", "svg", caller="codex"
        )

        self.assertEqual(bundle["caller"], "codex")

    def test_caller_selects_matching_followup_transport(self):
        with (
            mock.patch.dict(chat_backend.os.environ, {}, clear=True),
            mock.patch.object(chat_backend, "_codex_route_from_user_config", return_value=(None, ())),
            mock.patch.object(chat_backend, "_claude_route_from_user_config", return_value=(None, ())),
        ):
            codex = chat_backend.ChatSession({"caller": "codex"})
            claude = chat_backend.ChatSession({"caller": "claude"})

        self.assertEqual(codex.transport, "codex")
        self.assertEqual(claude.transport, "claude")


if __name__ == "__main__":
    unittest.main()
