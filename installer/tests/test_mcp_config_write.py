"""Behavior tests for `mcp_config --write` — merging the shim registration INTO an agent's
own config file (idempotent, backup-first, merge-not-clobber). Every test runs against a temp
config path via the env overrides, so a developer's real ~/.claude.json is never touched."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import traceback
import unittest
import venv
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from installer import client_host_ownership, managed_install, mcp_config
from installer.config import ShellError

tomllib = mcp_config._tomllib()


class WriteJsonClientTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cc = self.tmp / ".claude.json"
        self._prev = os.environ.get("CLAUDE_CODE_CONFIG")
        os.environ["CLAUDE_CODE_CONFIG"] = str(self.cc)
        # registration_root() now fails closed with no activated managed install (Phase 3) —
        # these tests exercise write/merge mechanics, not root selection, so give them a stable
        # root the same way EntryStatusTestCase already does.
        self._root = mock.patch.object(mcp_config, "registration_root", return_value=self.tmp)
        self._root.start()

    def tearDown(self):
        self._root.stop()
        if self._prev is None:
            os.environ.pop("CLAUDE_CODE_CONFIG", None)
        else:
            os.environ["CLAUDE_CODE_CONFIG"] = self._prev
        self._tmp.cleanup()

    def _entry(self):
        return mcp_config.render_entry()["mcpServers"]["decision-engine"]

    def test_entry_has_pythonpath_for_cwd_independent_import(self):
        # Objection #4: the default (claude-code) entry omits `cwd`, so
        # PYTHONPATH == the managed root must carry the import, and the
        # cwd-independent bootstrap's root argv must name that same root.
        e = self._entry()
        self.assertEqual(e["type"], "stdio")
        self.assertEqual(e["env"]["PYTHONPATH"], str(mcp_config.registration_root()))
        self.assertEqual(e["env"]["PYTHONPATH"], e["args"][2])

    def test_claude_code_entry_omits_cwd_and_uses_cwd_independent_bootstrap(self):
        # Claude Code launches MCP servers from the open project directory and
        # does not honor `cwd`, so `python -m installer.launcher` resolves the
        # `installer` package from whatever checkout is open. Inside a
        # decision-engine source checkout that shadows the managed install and
        # the launcher refuses to serve. Same contract Cursor already ships.
        e = self._entry()
        root = str(mcp_config.registration_root())
        self.assertNotIn("cwd", e)
        self.assertEqual(e["type"], "stdio")
        self.assertEqual(
            e["args"],
            ["-c", mcp_config._CWD_INDEPENDENT_BOOTSTRAP, root, "--managed-root", root],
        )
        self.assertEqual(e["env"]["PYTHONPATH"], root)
        self.assertEqual(e["env"][mcp_config.CLIENT_HOST_ENV], "claude")

    def test_rendered_claude_code_argv_ignores_shadowing_cwd(self):
        # Regression for the failure this contract fixes: Claude Code runs the
        # server with cwd = the open project. A project carrying its own
        # `installer/` package must not be the one the launcher is imported
        # from. Spawns the rendered argv verbatim so the bootstrap payload
        # itself is exercised, not re-derived from the constant.
        bound = self.tmp / "installer"
        bound.mkdir()
        (bound / "__init__.py").write_text("", encoding="utf-8")
        (bound / "launcher.py").write_text(
            "def main(arguments):\n"
            "    print(__file__, *arguments)\n"
            "    return 23\n",
            encoding="utf-8",
        )
        open_project = self.tmp / "open-project"
        decoy = open_project / "installer"
        decoy.mkdir(parents=True)
        (decoy / "__init__.py").write_text("", encoding="utf-8")
        (decoy / "launcher.py").write_text(
            "def main(arguments):\n    return 41\n", encoding="utf-8"
        )

        entry = self._entry()
        environment = dict(os.environ)
        environment.update(entry["env"])
        completed = subprocess.run(
            [entry["command"], *entry["args"]],
            cwd=str(open_project),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(completed.returncode, 23, completed.stderr)
        loaded, *arguments = completed.stdout.split()
        self.assertEqual(Path(loaded).resolve(), (bound / "launcher.py").resolve())
        self.assertEqual(arguments, ["--managed-root", str(self.tmp)])

    def test_write_replaces_legacy_cwd_pinned_claude_code_entry(self):
        root = str(mcp_config.registration_root())
        legacy = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "installer.launcher"],
            "cwd": root,
            "env": {"PYTHONPATH": root, mcp_config.CLIENT_HOST_ENV: "claude"},
        }
        other = {"command": "other-server", "args": ["--flag"]}
        self.cc.write_text(
            json.dumps({"mcpServers": {"decision-engine": legacy, "other": other}}),
            encoding="utf-8",
        )
        self.assertEqual(mcp_config.entry_status("claude-code"), "stale")

        result = mcp_config.write_entry("claude-code")

        self.assertEqual(result["action"], "updated")
        self.assertIsNotNone(result["backup"])
        data = json.loads(self.cc.read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["decision-engine"], self._entry())
        self.assertNotIn("cwd", data["mcpServers"]["decision-engine"])
        self.assertEqual(data["mcpServers"]["other"], other)
        self.assertEqual(mcp_config.entry_status("claude-code"), "ready")

    def test_write_creates_fresh_config(self):
        result = mcp_config.write_entry("claude-code")
        self.assertEqual(result["action"], "added")
        self.assertIsNone(result["backup"])   # nothing to back up on a fresh file
        data = json.loads(self.cc.read_text())
        self.assertEqual(data["mcpServers"]["decision-engine"], self._entry())

    def test_write_supports_explicit_pre_activation_mode_without_changing_default(self):
        for path in (
            self.tmp / ".git",
            self.tmp / "installer",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.tmp / "config.json").write_text("{}\n", encoding="utf-8")
        (self.tmp / "installer" / "launcher.py").write_text(
            "# launcher\n", encoding="utf-8"
        )
        (self.tmp / "installer" / "mcp_bootstrap.py").write_text(
            "# bootstrap\n", encoding="utf-8"
        )
        with (
            mock.patch.object(mcp_config, "managed_root", return_value=self.tmp),
            mock.patch.object(
                mcp_config.managed_install,
                "_require_fixed_managed_root",
            ),
            mock.patch.object(
                mcp_config.managed_install,
                "canonical_managed_root",
                return_value=self.tmp,
            ),
        ):
            result = mcp_config.write_entry("claude-code", allow_unactivated=True)
        self.assertEqual(result["action"], "added")
        entry = json.loads(self.cc.read_text())["mcpServers"]["decision-engine"]
        self.assertNotIn("cwd", entry)
        self.assertEqual(entry["args"][2:], [str(self.tmp), "--managed-root", str(self.tmp)])
        self.assertEqual(entry["env"]["PYTHONPATH"], str(self.tmp))
        self.assertNotIn("DE_ENDPOINT", json.dumps(entry))

    def test_write_is_idempotent(self):
        mcp_config.write_entry("claude-code")
        first = self.cc.read_text()
        result = mcp_config.write_entry("claude-code")
        self.assertEqual(result["action"], "unchanged")
        self.assertIsNone(result["backup"])
        self.assertEqual(self.cc.read_text(), first)   # byte-identical, no churn
        # a second write also must not spawn a backup sidecar
        self.assertEqual(list(self.tmp.glob(".claude.json.de-bak.*")), [])

    def test_write_preserves_other_servers_and_keys(self):
        self.cc.write_text(json.dumps({
            "userID": "keep-me",
            "mcpServers": {"other": {"command": "x", "args": []}},
            "projects": {"/p": {"k": 1}},
        }))
        result = mcp_config.write_entry("claude-code")
        self.assertEqual(result["action"], "added")
        self.assertIsNotNone(result["backup"])   # existing file → backed up
        data = json.loads(self.cc.read_text())
        self.assertEqual(data["userID"], "keep-me")             # unrelated top-level key survives
        self.assertEqual(data["projects"], {"/p": {"k": 1}})    # nested user state survives
        self.assertIn("other", data["mcpServers"])              # sibling server survives
        self.assertIn("decision-engine", data["mcpServers"])    # ours added

    def test_write_updates_existing_entry_with_backup(self):
        self.cc.write_text(json.dumps({"mcpServers": {"decision-engine": {"command": "stale"}}}))
        result = mcp_config.write_entry("claude-code")
        self.assertEqual(result["action"], "updated")
        self.assertIsNotNone(result["backup"])
        self.assertTrue(Path(result["backup"]).exists())
        data = json.loads(self.cc.read_text())
        self.assertEqual(data["mcpServers"]["decision-engine"], self._entry())

    def test_write_aborts_on_non_dict_json(self):
        self.cc.write_text(json.dumps(["not", "an", "object"]))
        with self.assertRaises(ShellError):
            mcp_config.write_entry("claude-code")
        self.assertEqual(json.loads(self.cc.read_text()), ["not", "an", "object"])   # untouched

    def test_write_aborts_on_invalid_json(self):
        self.cc.write_text("{ this is not json ]")
        with self.assertRaises(ShellError):
            mcp_config.write_entry("claude-code")
        self.assertEqual(self.cc.read_text(), "{ this is not json ]")   # untouched

    def test_write_aborts_when_mcpservers_is_wrong_type(self):
        self.cc.write_text(json.dumps({"mcpServers": [1, 2, 3]}))
        with self.assertRaises(ShellError):
            mcp_config.write_entry("claude-code")

    def test_dry_run_writes_nothing(self):
        result = mcp_config.write_entry("claude-code", dry_run=True)
        self.assertIn("dry-run", result["action"])
        self.assertFalse(self.cc.exists())

    def test_oversized_existing_config_is_refused_before_parsing(self):
        real_stat = os.stat
        with mock.patch.object(
            mcp_config.os, "stat", wraps=mcp_config.os.stat
        ) as stat_call:
            self.cc.write_text("{}", encoding="utf-8")
            stat_call.side_effect = lambda path, *args, **kwargs: mock.Mock(
                st_size=mcp_config.MAX_AGENT_CONFIG_BYTES + 1,
                st_mtime_ns=1,
                st_mode=0o600,
            ) if Path(path) == self.cc else real_stat(path, *args, **kwargs)
            with self.assertRaisesRegex(ShellError, "oversized"):
                mcp_config.write_entry("claude-code")

    def test_concurrent_change_aborts_without_clobber(self):
        # audit f4: if a live agent rewrites the file between our read and our replace, abort
        # fail-closed rather than clobber its edit. Simulate by mutating the file mid-write.
        self.cc.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        real_write = mcp_config._atomic_write_text

        def racing_write(
            path, text, *, expect_mtime_ns=None, expect_sha256=None, expect_exists=None
        ):
            # a concurrent writer touches the file AFTER our snapshot, BEFORE the guarded replace
            Path(path).write_text(json.dumps({"mcpServers": {"other": {"command": "x"},
                                                             "raced": {"command": "z"}}}))
            return real_write(
                path,
                text,
                expect_mtime_ns=expect_mtime_ns,
                expect_sha256=expect_sha256,
                expect_exists=expect_exists,
            )

        with mock.patch.object(mcp_config, "_atomic_write_text", side_effect=racing_write):
            with self.assertRaises(ShellError):
                mcp_config.write_entry("claude-code")
        # the concurrent writer's edit survived; our merge did NOT land
        data = json.loads(self.cc.read_text())
        self.assertIn("raced", data["mcpServers"])
        self.assertNotIn("decision-engine", data["mcpServers"])

    def test_concurrent_creation_aborts_without_clobber(self):
        real_write = mcp_config._atomic_write_text

        def racing_write(
            path, text, *, expect_mtime_ns=None, expect_sha256=None, expect_exists=None
        ):
            Path(path).write_text(
                json.dumps({"mcpServers": {"raced": {"command": "z"}}}),
                encoding="utf-8",
            )
            return real_write(
                path,
                text,
                expect_mtime_ns=expect_mtime_ns,
                expect_sha256=expect_sha256,
                expect_exists=expect_exists,
            )

        with mock.patch.object(mcp_config, "_atomic_write_text", side_effect=racing_write):
            with self.assertRaises(ShellError):
                mcp_config.write_entry("claude-code")

        data = json.loads(self.cc.read_text())
        self.assertIn("raced", data["mcpServers"])
        self.assertNotIn("decision-engine", data["mcpServers"])


class TomlReaderCompatibilityTestCase(unittest.TestCase):
    def test_python39_falls_back_to_bundled_toml_reader(self):
        with mock.patch.dict(
            "sys.modules",
            {"tomllib": None, "tomli": None},
        ):
            reader = mcp_config._tomllib()

        self.assertTrue(reader.__name__.startswith("installer._vendor."))
        self.assertEqual(reader.loads("value = 7\n"), {"value": 7})


class WriteCodexClientTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.toml = self.tmp / "config.toml"
        self._prev = os.environ.get("CODEX_CONFIG")
        os.environ["CODEX_CONFIG"] = str(self.toml)
        self._root = mock.patch.object(mcp_config, "registration_root", return_value=self.tmp)
        self._root.start()

    def tearDown(self):
        self._root.stop()
        if self._prev is None:
            os.environ.pop("CODEX_CONFIG", None)
        else:
            os.environ["CODEX_CONFIG"] = self._prev
        self._tmp.cleanup()

    def _desired(self):
        return tomllib.loads(mcp_config.render_codex_toml())["mcp_servers"]["decision-engine"]

    def test_write_creates_fresh_toml(self):
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "added")
        parsed = tomllib.loads(self.toml.read_text())
        self.assertEqual(parsed["mcp_servers"]["decision-engine"], self._desired())

    def test_cli_explicit_codex_selection_registers_the_launcher_entry(self):
        # The documented repair for the shipped symptom — Codex skills present, no
        # [mcp_servers.decision-engine] — must leave Codex reading as ready.
        out = io.StringIO()
        with redirect_stdout(out):
            exit_code = mcp_config.main(["--write", "--client", "codex"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(mcp_config.entry_status("codex"), "ready")
        self.assertIn("codex", out.getvalue())

    def test_cli_explicit_codex_selection_fails_loudly_when_the_write_fails(self):
        # An explicitly selected host that could not be written is never reported as done.
        err, out = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                mcp_config, "write_entry", side_effect=ShellError("synthetic codex refusal")
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            exit_code = mcp_config.main(["--write", "--client", "codex"])

        self.assertEqual(exit_code, 1)
        self.assertIn("codex", err.getvalue())
        self.assertNotIn("done", out.getvalue())

    def test_write_preserves_other_tables_and_comments(self):
        self.toml.write_text(
            "# my codex config\n"
            "model = \"gpt-5.5\"\n\n"
            "[mcp_servers.other]\n"
            "command = \"x\"\n"
            "args = []\n"
        )
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "added")
        self.assertIsNotNone(result["backup"])
        text = self.toml.read_text()
        self.assertIn("# my codex config", text)        # comment survives
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["model"], "gpt-5.5")    # top-level key survives
        self.assertEqual(parsed["mcp_servers"]["other"]["command"], "x")   # sibling table survives
        self.assertEqual(parsed["mcp_servers"]["decision-engine"], self._desired())

    def test_write_is_idempotent(self):
        mcp_config.write_entry("codex")
        first = self.toml.read_text()
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.toml.read_text(), first)

    def test_write_replaces_stale_table(self):
        self.toml.write_text(
            "[mcp_servers.decision-engine]\n"
            "command = \"OLD\"\n"
            "args = [\"-m\", \"installer.launcher\"]\n\n"
            "[other]\n"
            "keep = true\n"
        )
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "updated")
        parsed = tomllib.loads(self.toml.read_text())
        self.assertEqual(parsed["mcp_servers"]["decision-engine"], self._desired())
        self.assertNotEqual(parsed["mcp_servers"]["decision-engine"]["command"], "OLD")
        self.assertTrue(parsed["other"]["keep"])   # a later table is not swallowed by the splice

    def test_write_aborts_on_invalid_toml(self):
        self.toml.write_text("this is [ not valid toml")
        with self.assertRaises(ShellError):
            mcp_config.write_entry("codex")
        self.assertEqual(self.toml.read_text(), "this is [ not valid toml")

    def test_write_into_empty_existing_file_adds(self):
        # audit f10: an existing empty/whitespace file must add cleanly, not crash.
        self.toml.write_text("   \n\t\n")
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "added")
        self.assertEqual(tomllib.loads(self.toml.read_text())["mcp_servers"]["decision-engine"],
                         self._desired())

    def test_write_aborts_when_mcp_servers_is_not_a_table(self):
        # audit f3/f4: a scalar mcp_servers must fail closed with ShellError, not AttributeError.
        self.toml.write_text('mcp_servers = "oops"\n')
        with self.assertRaises(ShellError):
            mcp_config.write_entry("codex")
        self.assertEqual(self.toml.read_text(), 'mcp_servers = "oops"\n')   # untouched

    def test_write_consumes_orphan_child_table(self):
        # audit f5: a stale [mcp_servers.decision-engine.env] sub-table is replaced WITH the parent,
        # not orphaned — the update succeeds and no leftover env key remains.
        self.toml.write_text(
            "[mcp_servers.decision-engine]\n"
            "command = \"OLD\"\n\n"
            "[mcp_servers.decision-engine.env]\n"
            "STALE = \"1\"\n\n"
            "[keep]\n"
            "x = 1\n"
        )
        result = mcp_config.write_entry("codex")
        self.assertEqual(result["action"], "updated")
        parsed = tomllib.loads(self.toml.read_text())
        self.assertEqual(parsed["mcp_servers"]["decision-engine"], self._desired())
        self.assertEqual(
            parsed["mcp_servers"]["decision-engine"]["env"],
            {mcp_config.CLIENT_HOST_ENV: "codex"},
        )
        self.assertEqual(parsed["keep"]["x"], 1)                            # unrelated table kept

    def test_codex_write_fails_closed_if_toml_reader_cannot_load(self):
        with mock.patch.object(mcp_config, "_tomllib", side_effect=ShellError("reader unavailable")):
            with self.assertRaises(ShellError):
                mcp_config.write_entry("codex")


class WriteCursorClientTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cursor = self.tmp / ".cursor" / "mcp.json"
        self.managed_home = self.tmp / "managed-home"
        self.ownership_record = (
            self.managed_home / "installations" / "decision-engine-cursor.json"
        )
        self._previous = os.environ.get("CURSOR_CONFIG")
        os.environ["CURSOR_CONFIG"] = str(self.cursor)
        self._root = mock.patch.object(
            mcp_config, "registration_root", return_value=self.tmp
        )
        self._root.start()
        self._managed_home = mock.patch.object(
            managed_install.config,
            "DEFAULT_DEEPPATTERN_HOME",
            self.managed_home,
        )
        self._managed_home.start()
        self._ownership_path = mock.patch.object(
            client_host_ownership,
            "ownership_record_path",
            return_value=self.ownership_record,
        )
        self._ownership_path.start()

    def tearDown(self):
        self._ownership_path.stop()
        self._managed_home.stop()
        self._root.stop()
        if self._previous is None:
            os.environ.pop("CURSOR_CONFIG", None)
        else:
            os.environ["CURSOR_CONFIG"] = self._previous
        self._tmp.cleanup()

    def test_cursor_write_preserves_realistic_sibling_config(self):
        self.cursor.parent.mkdir(parents=True)
        existing = {
            "version": 1,
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@example/github-mcp"],
                    "env": {"GITHUB_TOKEN": "keep-private"},
                    "disabled": False,
                }
            },
            "uiState": {"expanded": ["github"]},
        }
        self.cursor.write_text(json.dumps(existing), encoding="utf-8")

        result = mcp_config.write_entry("cursor")

        self.assertEqual(result["action"], "added")
        self.assertIsNotNone(result["backup"])
        after = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual(after["version"], 1)
        self.assertEqual(after["uiState"], existing["uiState"])
        self.assertEqual(after["mcpServers"]["github"], existing["mcpServers"]["github"])
        cursor_entry = after["mcpServers"]["decision-engine"]
        self.assertEqual(cursor_entry["env"][mcp_config.CLIENT_HOST_ENV], "cursor")
        self.assertNotIn("type", cursor_entry)
        self.assertNotIn("cwd", cursor_entry)

    def test_shared_batch_writer_isolates_one_host_failure(self):
        def write(client, *_args, **_kwargs):
            if client == "cursor":
                raise ShellError("synthetic cursor conflict")
            return {"client": client, "action": "unchanged", "backup": None}

        with mock.patch.object(mcp_config, "write_entry", side_effect=write):
            result = mcp_config.write_entries(
                ["claude-code", "cursor", "codex"]
            )

        self.assertEqual(
            tuple(item["client"] for item in result.written),
            ("claude-code", "codex"),
        )
        self.assertEqual(
            result.failed,
            (("cursor", "client MCP configuration failed"),),
        )

    def test_shared_batch_writer_redacts_filesystem_failure_and_continues(self):
        def write(client, *_args, **_kwargs):
            if client == "cursor":
                raise OSError("C:/private/profile/cursor/mcp.json: access denied")
            return {"client": client, "action": "unchanged", "backup": None}

        with mock.patch.object(mcp_config, "write_entry", side_effect=write):
            result = mcp_config.write_entries(
                ["claude-code", "cursor", "codex"]
            )

        self.assertEqual(
            tuple(item["client"] for item in result.written),
            ("claude-code", "codex"),
        )
        self.assertEqual(
            result.failed,
            (("cursor", "client MCP configuration failed"),),
        )

    def test_shared_batch_writer_returns_host_notice_only_after_a_real_write(self):
        written = {"client": "workbuddy", "action": "added", "backup": None}
        with (
            mock.patch.object(mcp_config, "write_entry", return_value=written),
            mock.patch.dict(os.environ, {"DE_UI_LOCALE": "en-US"}),
        ):
            result = mcp_config.write_entries(["workbuddy"])
            dry_run = mcp_config.write_entries(["workbuddy"], dry_run=True)

        self.assertEqual(len(result.notices), 1)
        self.assertIn("Custom connectors", result.notices[0])
        self.assertIn("Trust", result.notices[0])
        self.assertIn("restart WorkBuddy", result.notices[0])
        self.assertEqual(dry_run.notices, ())

    def test_shared_batch_writer_keeps_notice_for_successful_workbuddy_in_partial_batch(self):
        def write(client, *_args, **_kwargs):
            if client == "cursor":
                raise ShellError("synthetic cursor conflict")
            return {"client": client, "action": "added", "backup": None}

        with (
            mock.patch.object(mcp_config, "write_entry", side_effect=write),
            mock.patch.dict(os.environ, {"DE_UI_LOCALE": "en-US"}),
        ):
            result = mcp_config.write_entries(["workbuddy", "cursor"])

        self.assertEqual(
            tuple(item["client"] for item in result.written), ("workbuddy",)
        )
        self.assertEqual(
            result.failed, (("cursor", "client MCP configuration failed"),)
        )
        self.assertEqual(len(result.notices), 1)
        self.assertIn("Custom connectors", result.notices[0])

    def test_notice_factory_failure_cannot_undo_successful_write(self):
        def broken_notice():
            raise RuntimeError("synthetic optional notice failure")

        written = {"client": "workbuddy", "action": "added", "backup": None}
        spec = replace(
            mcp_config.CLIENT_SPECS["workbuddy"],
            post_mcp_write_notice=broken_notice,
        )
        with (
            mock.patch.object(mcp_config, "write_entry", return_value=written),
            mock.patch.dict(mcp_config.CLIENT_SPECS, {"workbuddy": spec}),
        ):
            result = mcp_config.write_entries(["workbuddy"])

        self.assertEqual(result.written, (written,))
        self.assertEqual(result.failed, ())
        self.assertEqual(result.notices, ())

    def test_cursor_write_updates_same_name_and_preserves_siblings(self):
        self.cursor.parent.mkdir(parents=True)
        foreign = {
            "mcpServers": {
                "decision-engine": {
                    "command": "foreign-command",
                    "args": ["--foreign"],
                    "env": {"KEEP": "user-owned"},
                },
                "other": {"command": "keep"},
            }
        }
        original = json.dumps(foreign, indent=2).encode("utf-8")
        self.cursor.write_bytes(original)

        with mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            return_value=None,
        ):
            result = mcp_config.write_entry("cursor")

        after = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertEqual(result["action"], "updated")
        self.assertEqual(after["mcpServers"]["other"], {"command": "keep"})
        self.assertNotEqual(self.cursor.read_bytes(), original)
        self.assertEqual(len(list(self.cursor.parent.glob("mcp.json.de-bak.*"))), 1)
        self.assertFalse(self.ownership_record.exists())

    def test_cursor_write_version_gate_blocks_unsupported_and_warns_unknown(self):
        evidence = mock.sentinel.evidence
        with mock.patch.object(
            mcp_config.cursor_version,
            "collect_cursor_version_evidence",
            return_value=evidence,
        ), mock.patch.object(
            mcp_config.cursor_version,
            "assess_cursor_version",
        ) as assess:
            assess.return_value = mcp_config.cursor_version.CursorVersionAssessment(
                state="unsupported_cursor_version",
                detail="private detail must not escape",
            )
            with self.assertRaisesRegex(ShellError, "unsupported_cursor_version"):
                mcp_config._cursor_write_version_gate()
            assess.return_value = mcp_config.cursor_version.CursorVersionAssessment(
                state="cursor_version_unknown",
                detail="private detail must not escape",
            )
            self.assertEqual(
                mcp_config._cursor_write_version_gate(),
                "cursor_version_unknown",
            )
            assess.return_value = mcp_config.cursor_version.CursorVersionAssessment(
                state="candidate_standard_version_floor_met",
                detail="private detail must not escape",
            )
            self.assertIsNone(mcp_config._cursor_write_version_gate())

    def test_cursor_version_probe_failure_blocks_write(self):
        with mock.patch.object(
            mcp_config.cursor_version,
            "collect_cursor_version_evidence",
            side_effect=OSError("private probe failure"),
        ):
            with self.assertRaisesRegex(
                ShellError,
                "cursor_version_probe_failed",
            ) as caught:
                mcp_config._cursor_write_version_gate()

        self.assertNotIn("private probe failure", str(caught.exception))

    def test_cursor_unsupported_version_refuses_before_lock_or_config_mutation(self):
        with mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            side_effect=ShellError("unsupported_cursor_version"),
        ):
            with self.assertRaisesRegex(ShellError, "unsupported_cursor_version"):
                mcp_config.write_entry("cursor")

        self.assertFalse(self.cursor.exists())
        self.assertFalse(
            self.managed_home
            .joinpath("installations", "decision-engine-cursor")
            .exists()
        )

    def test_cursor_unknown_version_is_explicit_warning_but_not_silent_write(self):
        with mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            return_value="cursor_version_unknown",
        ):
            result = mcp_config.write_entry("cursor")

        self.assertEqual(result["action"], "added")
        self.assertEqual(result["warning"], "cursor_version_unknown")
        self.assertTrue(self.cursor.exists())

    def test_cursor_stale_write_uses_shared_json_merge(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        old = dict(desired, args=["-m", "installer.shim"])
        old["disabled"] = False
        old["metadata"] = {"ownerNote": "keep"}
        existing = {
            "version": 1,
            "mcpServers": {
                "decision-engine": old,
                "other": {"command": "keep"},
            },
        }
        self.cursor.parent.mkdir(parents=True)
        original = json.dumps(existing).encode("utf-8")
        self.cursor.write_bytes(original)

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            return_value=self._ownership_record_for(old),
        ), mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            return_value=None,
        ):
            result = mcp_config.write_entry("cursor")

        self.assertEqual(result["action"], "updated")
        self.assertNotEqual(self.cursor.read_bytes(), original)
        self.assertEqual(len(list(self.cursor.parent.glob("mcp.json.de-bak.*"))), 1)

    def test_cursor_recordless_writer_can_update_through_shared_json_merge(self):
        # Both write-time gates are stubbed for the same reason: this exercises the JSON
        # merge, and its two marker interpreters are labels that tell the writes apart, not
        # runnable pythons. The floor gate has its own coverage in InterpreterFloorTestCase.
        with mock.patch.object(
            mcp_config,
            "_cursor_write_version_gate",
            return_value=None,
        ), mock.patch.object(
            mcp_config,
            "_interpreter_defect",
            return_value=None,
        ):
            first = mcp_config.write_entry("cursor", python="first-python")
            second = mcp_config.write_entry("cursor", python="second-python")

        self.assertEqual(first["action"], "added")
        self.assertEqual(second["action"], "updated")
        entry = json.loads(self.cursor.read_text(encoding="utf-8"))["mcpServers"]["decision-engine"]
        self.assertEqual(entry["command"], "second-python")
        self.assertFalse(self.ownership_record.exists())

    def test_cursor_cas_detects_same_size_same_mtime_content_change(self):
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_bytes(b"{}")
        before = self.cursor.stat()
        expected_hash = mcp_config.hashlib.sha256(b"{}").hexdigest()
        self.cursor.write_bytes(b"[]")
        os.utime(
            self.cursor,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )

        with self.assertRaisesRegex(ShellError, "changed while being updated"):
            mcp_config._atomic_write_text(
                self.cursor,
                '{"replacement":true}\n',
                expect_mtime_ns=before.st_mtime_ns,
                expect_sha256=expected_hash,
                expect_exists=True,
            )

        self.assertEqual(self.cursor.read_bytes(), b"[]")

    def test_cursor_unchanged_crlf_plan_keeps_exact_file_hash(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        self.cursor.parent.mkdir(parents=True)
        raw = (
            json.dumps({"mcpServers": {"decision-engine": desired}}, indent=2)
            + "\n"
        ).replace("\n", "\r\n").encode("utf-8")
        self.cursor.write_bytes(raw)

        plan = mcp_config.prepare_cursor_entry_write(
            desired,
            version_warning=None,
        )

        self.assertEqual(plan.action, "unchanged")
        self.assertEqual(plan.pre_file_sha256, plan.post_file_sha256)

    def test_cursor_entry_status_reports_stale_without_side_effects(self):
        self.cursor.parent.mkdir(parents=True)
        existing = {
            "mcpServers": {
                "decision-engine": {
                    "command": "foreign-command",
                    "args": ["--not-managed"],
                    "env": {"KEEP": "user-owned"},
                },
                "other": {"command": "keep"},
            }
        }
        original = json.dumps(existing, indent=2).encode("utf-8")
        self.cursor.write_bytes(original)

        self.assertEqual(
            mcp_config.entry_status("cursor"),
            "stale",
        )
        self.assertEqual(self.cursor.read_bytes(), original)
        self.assertEqual(
            list(self.cursor.parent.glob("mcp.json.de-bak.*")),
            [],
        )

    def test_cursor_managed_entry_preserves_opaque_extra_fields(self):
        self.cursor.parent.mkdir(parents=True)
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        existing_entry = dict(desired)
        existing_entry["disabled"] = False
        existing_entry["metadata"] = {"ownerNote": "keep"}
        existing = {"mcpServers": {"decision-engine": existing_entry}}
        original = json.dumps(existing, indent=2).encode("utf-8")
        self.cursor.write_bytes(original)

        result = mcp_config.entry_status("cursor")

        self.assertEqual(result, "ready")
        self.assertEqual(self.cursor.read_bytes(), original)

    def test_cursor_shared_status_checks_required_managed_fields(self):
        self.cursor.parent.mkdir(parents=True)
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        cases = {
            "command": dict(desired, command="foreign-command"),
            "args": dict(desired, args=["--foreign"]),
            "env": dict(desired, env={"KEEP": "foreign"}),
            "env-superset": dict(
                desired,
                env={**desired["env"], "USER_EXTENSION": "foreign"},
            ),
            "type": dict(desired, type="stdio"),
            "cwd": dict(desired, cwd=str(self.tmp)),
            "missing-command": {
                key: value for key, value in desired.items() if key != "command"
            },
            "missing-env": {
                key: value for key, value in desired.items() if key != "env"
            },
        }

        expected = {
            "command": "stale",
            "args": "stale",
            "env": "stale",
            "env-superset": "stale",
            "type": "stale",
            "cwd": "stale",
            "missing-command": "stale",
            "missing-env": "stale",
        }
        for field, existing_entry in cases.items():
            with self.subTest(field=field):
                existing = {"mcpServers": {"decision-engine": existing_entry}}
                original = json.dumps(existing, indent=2).encode("utf-8")
                self.cursor.write_bytes(original)

                self.assertEqual(
                    mcp_config.entry_status("cursor"),
                    expected[field],
                )
                self.assertEqual(self.cursor.read_bytes(), original)

    def _ownership_record_for(self, entry):
        return client_host_ownership.OwnershipRecord(
            install_id="11111111-1111-4111-8111-111111111111",
            client="cursor",
            config_path=self.cursor,
            server_name="decision-engine",
            managed_fields_sha256=client_host_ownership.managed_entry_sha256_v1(
                entry
            ),
            skill_release_id="release-synthetic",
            skill_manifest_sha256="2" * 64,
        )

    def test_cursor_owned_current_entry_is_ready(self):
        managed_root = self.managed_home / "decision-engine"
        (managed_root / ".git").mkdir(parents=True)
        with mock.patch.object(
            managed_install.config,
            "DEFAULT_DEEPPATTERN_HOME",
            self.managed_home,
        ), mock.patch.object(
            mcp_config,
            "registration_root",
            return_value=managed_root,
        ):
            install_id = managed_install.write_managed_identity(managed_root)
            desired = mcp_config.render_entry(client="cursor")["mcpServers"][
                "decision-engine"
            ]
            self.cursor.parent.mkdir(parents=True)
            self.cursor.write_text(
                json.dumps({"mcpServers": {"decision-engine": desired}}),
                encoding="utf-8",
            )
            managed_install._atomic_write_private_json(
                self.ownership_record,
                {
                    "schema": 1,
                    "install_id": install_id,
                    "client": "cursor",
                    "config_path": str(self.cursor),
                    "server_name": "decision-engine",
                    "managed_entry_schema": 1,
                    "managed_fields_sha256": (
                        client_host_ownership.managed_entry_sha256_v1(desired)
                    ),
                    "skill_release_id": "release-synthetic",
                    "skill_manifest_sha256": "2" * 64,
                },
                manage_parent=True,
            )
            self.assertEqual(mcp_config.entry_status("cursor"), "ready")

    def test_cursor_legacy_record_does_not_override_shared_stale_status(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        modified = dict(desired, command="user-command")
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": modified}}),
            encoding="utf-8",
        )

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            return_value=self._ownership_record_for(desired),
        ):
            self.assertEqual(
                mcp_config.entry_status("cursor"),
                "stale",
            )

    def test_cursor_owned_old_entry_is_stale(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        old_entry = dict(desired, args=["-m", "installer.shim"])
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": old_entry}}),
            encoding="utf-8",
        )

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            return_value=self._ownership_record_for(old_entry),
        ):
            self.assertEqual(mcp_config.entry_status("cursor"), "stale")

    def test_cursor_matching_desired_ignores_legacy_record_hash(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": desired}}),
            encoding="utf-8",
        )
        stale_record = replace(
            self._ownership_record_for(desired),
            managed_fields_sha256="0" * 64,
        )

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            return_value=stale_record,
        ):
            self.assertEqual(
                mcp_config.entry_status("cursor"),
                "ready",
            )

    def test_cursor_invalid_legacy_ownership_record_is_ignored(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": desired}}),
            encoding="utf-8",
        )

        self.ownership_record.parent.mkdir(parents=True)
        self.ownership_record.write_text('{"schema": 1}', encoding="utf-8")

        self.assertEqual(mcp_config.entry_status("cursor"), "ready")

    def test_cursor_empty_dev_root_is_stale(self):
        desired = mcp_config.render_entry(client="cursor")["mcpServers"][
            "decision-engine"
        ]
        entry = dict(desired, args=[*desired["args"], "--dev-root", ""])
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": entry}}),
            encoding="utf-8",
        )

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            side_effect=AssertionError("invalid dev root must stop first"),
        ):
            self.assertEqual(mcp_config.entry_status("cursor"), "stale")

    def test_cursor_absent_entry_does_not_consult_ownership_record(self):
        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            side_effect=AssertionError("absent entry has no ownership decision"),
        ):
            self.assertEqual(mcp_config.entry_status("cursor"), "absent")

    def test_cursor_dev_root_status_does_not_consult_managed_ownership(self):
        dev_root = self.tmp / "dev-checkout"
        (dev_root / "installer").mkdir(parents=True)
        (dev_root / "installer" / "launcher.py").write_text(
            "# launcher\n",
            encoding="utf-8",
        )
        entry = mcp_config.render_entry(
            client="cursor",
            dev_root=dev_root,
        )["mcpServers"]["decision-engine"]
        self.cursor.parent.mkdir(parents=True)
        self.cursor.write_text(
            json.dumps({"mcpServers": {"decision-engine": entry}}),
            encoding="utf-8",
        )

        with mock.patch.object(
            client_host_ownership,
            "read_record_if_present",
            side_effect=AssertionError("managed ownership must not gate dev mode"),
        ):
            self.assertEqual(mcp_config.entry_status("cursor"), "ready")


class EntryStatusTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paths = {
            "CLAUDE_CODE_CONFIG": self.tmp / ".claude.json",
            "CODEX_CONFIG": self.tmp / "config.toml",
            "CLAUDE_DESKTOP_CONFIG": self.tmp / "desktop.json",
            "CLAUDE_DESKTOP_3P_CONFIG": self.tmp / "desktop-3p.json",
        }
        self._env = mock.patch.dict(
            os.environ,
            {key: str(path) for key, path in self.paths.items()},
        )
        self._env.start()
        self._root = mock.patch.object(
            mcp_config, "registration_root", return_value=self.tmp
        )
        self._root.start()

    def tearDown(self):
        self._root.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_absent_entry(self):
        self.assertEqual(mcp_config.entry_status("claude-code"), "absent")

    def test_generated_json_entry_is_ready_and_old_shim_is_stale(self):
        path = self.paths["CLAUDE_CODE_CONFIG"]
        path.write_text(
            json.dumps(mcp_config.render_entry(cwd=self.tmp)), encoding="utf-8"
        )
        self.assertEqual(mcp_config.entry_status("claude-code"), "ready")

        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["decision-engine"]["args"] = ["-m", "installer.shim"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(mcp_config.entry_status("claude-code"), "stale")

    def test_wrong_command_is_stale(self):
        path = self.paths["CLAUDE_CODE_CONFIG"]
        data = mcp_config.render_entry(cwd=self.tmp)
        data["mcpServers"]["decision-engine"]["command"] = "/missing/python"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(mcp_config.entry_status("claude-code"), "stale")

    def test_generated_codex_entry_is_ready_and_short_timeout_is_stale(self):
        path = self.paths["CODEX_CONFIG"]
        path.write_text(
            mcp_config.render_codex_toml(cwd=self.tmp), encoding="utf-8"
        )
        self.assertEqual(mcp_config.entry_status("codex"), "ready")

        path.write_text(
            mcp_config.render_codex_toml(cwd=self.tmp, tool_timeout_sec=1),
            encoding="utf-8",
        )
        self.assertEqual(mcp_config.entry_status("codex"), "stale")

    def test_dev_root_entry_is_ready_when_the_path_still_looks_like_a_checkout(self):
        # A dev-root entry is structurally compared against a "desired" entry
        # re-derived from that same path (see mcp_config.entry_status), which
        # can never by itself catch a moved/deleted dev_root — the path is
        # independently checked for still looking like a real checkout.
        dev_root = self.tmp / "dev-checkout"
        (dev_root / "installer").mkdir(parents=True)
        (dev_root / "installer" / "launcher.py").write_text("# launcher\n", encoding="utf-8")
        path = self.paths["CLAUDE_CODE_CONFIG"]
        path.write_text(
            json.dumps(mcp_config.render_entry(dev_root=dev_root)), encoding="utf-8"
        )
        self.assertEqual(mcp_config.entry_status("claude-code"), "ready")

    def test_dev_root_entry_is_stale_when_the_path_no_longer_exists(self):
        missing_dev_root = self.tmp / "dev-checkout-that-was-deleted"
        path = self.paths["CLAUDE_CODE_CONFIG"]
        path.write_text(
            json.dumps(mcp_config.render_entry(dev_root=missing_dev_root)), encoding="utf-8"
        )
        self.assertEqual(mcp_config.entry_status("claude-code"), "stale")

    def test_invalid_config_raises_without_echoing_contents(self):
        secret_body = "private-config-body"
        self.paths["CLAUDE_CODE_CONFIG"].write_text(
            "{" + secret_body, encoding="utf-8"
        )
        with self.assertRaises(ShellError) as caught:
            mcp_config.entry_status("claude-code")
        self.assertNotIn(secret_body, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        rendered = "".join(
            traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__
            )
        )
        self.assertNotIn(secret_body, rendered)

    def test_size_limit_uses_open_file_not_prior_path_stat(self):
        path = self.paths["CLAUDE_CODE_CONFIG"]
        path.write_bytes(b" " * (mcp_config.MAX_AGENT_CONFIG_BYTES + 1))
        with mock.patch.object(
            mcp_config.os, "stat", return_value=mock.Mock(st_size=1)
        ):
            with self.assertRaisesRegex(ShellError, "oversized"):
                mcp_config.read_entry("claude-code")


class DetectAndCliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("CLAUDE_CODE_CONFIG", "CODEX_CONFIG", "CLAUDE_DESKTOP_CONFIG",
                        "CLAUDE_DESKTOP_3P_CONFIG",
                        "CURSOR_CONFIG", "TRAE_WORK_CONFIG", "TRAE_WORK_SKILLS_DIR",
                        "TRAE_WORK_APP_ROOT",
                       "TRAE_WORK_CN_CONFIG", "TRAE_WORK_CN_SKILLS_DIR",
                       "TRAE_WORK_CN_APP_ROOT", "WORKBUDDY_CONFIG",
                       "WORKBUDDY_SKILLS_DIR", "WORKBUDDY_APP_ROOT",
                       "WORKBUDDY_AI_CONFIG", "WORKBUDDY_AI_SKILLS_DIR",
                       "WORKBUDDY_AI_APP_ROOT", "CODEBUDDY_CONFIG",
                       "CODEBUDDY_SKILLS_DIR", "CODEBUDDY_CLI",
                        "QODER_CONFIG", "QODER_SKILLS_DIR", "QODER_APP_ROOT",
                        "QODER_CN_CONFIG", "QODER_CN_SKILLS_DIR",
                        "QODER_CN_APP_ROOT", "QODER_IDE_CONFIG",
                        "QODER_IDE_SETTINGS", "QODER_IDE_SKILLS_DIR",
                        "QODER_IDE_APP_ROOT", "QODER_CN_IDE_CONFIG",
                        "QODER_CN_IDE_SETTINGS", "QODER_CN_IDE_SKILLS_DIR",
                        "QODER_CN_IDE_APP_ROOT", "TRAE_CONFIG", "TRAE_SKILLS_DIR",
                        "TRAE_APP_ROOT", "TRAE_CN_CONFIG", "TRAE_CN_SKILLS_DIR",
                        "TRAE_CN_APP_ROOT",
                        "DE_UI_LOCALE")}
        # point every client at this temp tree; none exist yet
        os.environ["CLAUDE_CODE_CONFIG"] = str(self.tmp / "cc" / ".claude.json")
        os.environ["CODEX_CONFIG"] = str(self.tmp / "cx" / "config.toml")
        os.environ["CLAUDE_DESKTOP_CONFIG"] = str(self.tmp / "cd" / "claude_desktop_config.json")
        os.environ["CLAUDE_DESKTOP_3P_CONFIG"] = str(
            self.tmp / "cd-3p" / "claude_desktop_config.json"
        )
        os.environ["CURSOR_CONFIG"] = str(self.tmp / "cu" / "mcp.json")
        os.environ["QODER_CONFIG"] = str(self.tmp / "qd" / "mcp.json")
        os.environ["QODER_SKILLS_DIR"] = str(
            self.tmp / "no-qoder" / "skills"
        )
        os.environ["QODER_APP_ROOT"] = str(self.tmp / "no-qoder" / "app")
        os.environ["QODER_CN_CONFIG"] = str(self.tmp / "qd-cn" / "settings.json")
        os.environ["QODER_CN_SKILLS_DIR"] = str(
            self.tmp / "no-qoder-cn" / "skills"
        )
        os.environ["QODER_CN_APP_ROOT"] = str(
            self.tmp / "no-qoder-cn" / "app"
        )
        os.environ["QODER_IDE_CONFIG"] = str(self.tmp / "qd-ide" / "mcp.json")
        os.environ["QODER_IDE_SETTINGS"] = str(
            self.tmp / "qd-ide" / "settings.json"
        )
        os.environ["QODER_IDE_SKILLS_DIR"] = str(
            self.tmp / "no-qoder-ide" / "skills"
        )
        os.environ["QODER_IDE_APP_ROOT"] = str(
            self.tmp / "no-qoder-ide" / "app"
        )
        os.environ["QODER_CN_IDE_CONFIG"] = str(
            self.tmp / "qd-cn-ide" / "mcp.json"
        )
        os.environ["QODER_CN_IDE_SETTINGS"] = str(
            self.tmp / "qd-cn-ide" / "settings.json"
        )
        os.environ["QODER_CN_IDE_SKILLS_DIR"] = str(
            self.tmp / "no-qoder-cn-ide" / "skills"
        )
        os.environ["QODER_CN_IDE_APP_ROOT"] = str(
            self.tmp / "no-qoder-cn-ide" / "app"
        )
        os.environ["TRAE_CONFIG"] = str(self.tmp / "trae" / "mcp.json")
        os.environ["TRAE_SKILLS_DIR"] = str(
            self.tmp / "no-trae" / "skills"
        )
        os.environ["TRAE_APP_ROOT"] = str(self.tmp / "no-trae" / "app")
        os.environ["TRAE_CN_CONFIG"] = str(self.tmp / "trae-cn" / "mcp.json")
        os.environ["TRAE_CN_SKILLS_DIR"] = str(
            self.tmp / "no-trae-cn" / "skills"
        )
        os.environ["TRAE_CN_APP_ROOT"] = str(
            self.tmp / "no-trae-cn" / "app"
        )
        os.environ["TRAE_WORK_CONFIG"] = str(self.tmp / "tw" / "mcp.json")
        os.environ["TRAE_WORK_SKILLS_DIR"] = str(
            self.tmp / "no-trae-work" / "skills"
        )
        os.environ["TRAE_WORK_APP_ROOT"] = str(self.tmp / "no-trae-work" / "app")
        os.environ["TRAE_WORK_CN_CONFIG"] = str(self.tmp / "tw-cn" / "mcp.json")
        os.environ["TRAE_WORK_CN_SKILLS_DIR"] = str(
            self.tmp / "no-trae-work-cn" / "skills"
        )
        os.environ["TRAE_WORK_CN_APP_ROOT"] = str(
            self.tmp / "no-trae-work-cn" / "app"
        )
        os.environ["WORKBUDDY_CONFIG"] = str(self.tmp / "wb" / "mcp.json")
        os.environ["WORKBUDDY_SKILLS_DIR"] = str(
            self.tmp / "no-workbuddy" / "skills"
        )
        os.environ["WORKBUDDY_APP_ROOT"] = str(
            self.tmp / "no-workbuddy" / "app"
        )
        os.environ["WORKBUDDY_AI_CONFIG"] = str(
            self.tmp / "wb-ai" / "mcp.json"
        )
        os.environ["WORKBUDDY_AI_SKILLS_DIR"] = str(
            self.tmp / "no-workbuddy-ai" / "skills"
        )
        os.environ["WORKBUDDY_AI_APP_ROOT"] = str(
            self.tmp / "no-workbuddy-ai" / "app"
        )
        os.environ["CODEBUDDY_CONFIG"] = str(
            self.tmp / "cb" / "mcp.json"
        )
        os.environ["CODEBUDDY_SKILLS_DIR"] = str(
            self.tmp / "no-codebuddy" / "skills"
        )
        os.environ["CODEBUDDY_CLI"] = str(
            self.tmp / "no-codebuddy" / "bin" / "codebuddy"
        )
        os.environ["DE_UI_LOCALE"] = "en-US"
        self._root = mock.patch.object(mcp_config, "registration_root", return_value=self.tmp)
        self._root.start()

    def tearDown(self):
        self._root.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _install_workbuddy_fixture(self):
        app_root = Path(os.environ["WORKBUDDY_APP_ROOT"])
        executable = (
            app_root / "Contents" / "MacOS" / "Electron"
            if sys.platform == "darwin"
            else app_root / "WorkBuddy.exe"
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"desktop-fixture")

    def test_detect_by_file_or_app_dir(self):
        self.assertEqual(mcp_config.detect_clients(), [])   # nothing present
        # codex is detected by its parent dir (an app-specific dir), unlike claude-code
        (self.tmp / "cx").mkdir()
        self.assertEqual(mcp_config.detect_clients(), ["codex"])
        # claude-code needs the config file itself, or a sibling ~/.claude dir — NOT just a parent
        (self.tmp / "cc").mkdir()
        self.assertNotIn("claude-code", mcp_config.detect_clients())
        (self.tmp / "cc" / ".claude").mkdir()
        self.assertIn("claude-code", mcp_config.detect_clients())

    def test_cursor_detection_appends_without_reordering_existing_clients(self):
        (self.tmp / "cc").mkdir()
        (self.tmp / "cc" / ".claude").mkdir()
        (self.tmp / "cx").mkdir()
        (self.tmp / "cd").mkdir()
        before = mcp_config.detect_clients()
        self.assertEqual(before, ["claude-code", "claude-desktop", "codex"])

        (self.tmp / "cu").mkdir()

        self.assertEqual(
            mcp_config.detect_clients(),
            ["claude-code", "claude-desktop", "codex", "cursor"],
        )

    def test_claude_code_not_detected_from_bare_home(self):
        # audit f2: $HOME always exists, so the parent-dir heuristic must NOT flag claude-code
        # merely because home exists — require the config file or a ~/.claude dir.
        for k in ("CLAUDE_CODE_CONFIG", "CODEX_CONFIG", "CLAUDE_DESKTOP_CONFIG",
                  "CLAUDE_DESKTOP_3P_CONFIG",
                  "CURSOR_CONFIG", "TRAE_WORK_CONFIG", "TRAE_WORK_SKILLS_DIR",
                  "TRAE_WORK_APP_ROOT",
                  "TRAE_WORK_CN_CONFIG", "TRAE_WORK_CN_SKILLS_DIR",
                  "TRAE_WORK_CN_APP_ROOT", "WORKBUDDY_CONFIG",
                  "WORKBUDDY_SKILLS_DIR", "WORKBUDDY_APP_ROOT",
                  "WORKBUDDY_AI_CONFIG", "WORKBUDDY_AI_SKILLS_DIR",
                  "WORKBUDDY_AI_APP_ROOT", "CODEBUDDY_CONFIG",
                  "CODEBUDDY_SKILLS_DIR", "CODEBUDDY_CLI",
                  "QODER_CONFIG", "QODER_SKILLS_DIR", "QODER_APP_ROOT",
                  "QODER_CN_CONFIG", "QODER_CN_SKILLS_DIR", "QODER_CN_APP_ROOT",
                  "QODER_IDE_CONFIG", "QODER_IDE_SETTINGS",
                  "QODER_IDE_SKILLS_DIR", "QODER_IDE_APP_ROOT",
                  "QODER_CN_IDE_CONFIG", "QODER_CN_IDE_SETTINGS",
                  "QODER_CN_IDE_SKILLS_DIR", "QODER_CN_IDE_APP_ROOT",
                  "TRAE_CONFIG", "TRAE_SKILLS_DIR", "TRAE_APP_ROOT",
                  "TRAE_CN_CONFIG", "TRAE_CN_SKILLS_DIR", "TRAE_CN_APP_ROOT"):
            os.environ.pop(k, None)
        fake_home = self.tmp / "home"
        fake_home.mkdir()
        with mock.patch.object(mcp_config.Path, "home", return_value=fake_home):
            self.assertNotIn("claude-code", mcp_config.detect_clients())
            (fake_home / ".claude").mkdir()   # Claude Code's dir appears → now detected
            self.assertIn("claude-code", mcp_config.detect_clients())

    def test_cli_write_no_client_and_none_detected_errors(self):
        rc = mcp_config.main(["--write"])
        self.assertEqual(rc, 1)   # nothing detected, no --client → error exit

    def test_cli_write_single_client(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mcp_config.main(["--write", "--client", "claude-code"])
        self.assertEqual(rc, 0)
        cc = Path(os.environ["CLAUDE_CODE_CONFIG"])
        self.assertTrue(cc.exists())
        self.assertIn("decision-engine", json.loads(cc.read_text())["mcpServers"])
        self.assertIn("RESTART", buf.getvalue())   # user is told to restart the agent
        self.assertNotIn("Custom connectors", buf.getvalue())

    def test_cli_allow_unactivated_does_not_invent_a_dev_root(self):
        result = {
            "client": "claude-code",
            "action": "unchanged",
            "path": "synthetic-config.json",
            "backup": None,
        }
        with mock.patch.object(
            mcp_config, "write_entry", return_value=result
        ) as write_entry:
            rc = mcp_config.main(
                [
                    "--write",
                    "--allow-unactivated",
                    "--client",
                    "claude-code",
                    "--dry-run",
                ]
            )

        self.assertEqual(rc, 0)
        write_entry.assert_called_once_with(
            "claude-code",
            "decision-engine",
            dev_root=None,
            allow_unactivated=True,
            dry_run=True,
        )

    def test_cli_write_workbuddy_prints_manual_trust_steps(self):
        self._install_workbuddy_fixture()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mcp_config.main(["--write", "--client", "workbuddy"])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("Custom connectors", output)
        self.assertIn("decision-engine", output)
        self.assertIn("Trust", output)
        self.assertIn("restart WorkBuddy", output)

    def test_cli_write_workbuddy_uses_chinese_locale_with_bilingual_ui_labels(self):
        self._install_workbuddy_fixture()

        buf = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"DE_UI_LOCALE": "zh-CN"}),
            redirect_stdout(buf),
        ):
            rc = mcp_config.main(["--write", "--client", "workbuddy"])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("自定义连接器", output)
        self.assertIn("信任", output)
        self.assertIn("Custom connectors", output)
        self.assertIn("Trust", output)

    def test_cli_write_chinese_notice_is_safe_on_strict_legacy_stdout(self):
        self._install_workbuddy_fixture()
        raw_output = io.BytesIO()
        legacy_stdout = io.TextIOWrapper(
            raw_output, encoding="cp1252", errors="strict"
        )

        with (
            mock.patch.dict(os.environ, {"DE_UI_LOCALE": "zh-CN"}),
            mock.patch.object(sys, "stdout", legacy_stdout),
        ):
            rc = mcp_config.main(["--write", "--client", "workbuddy"])
            legacy_stdout.flush()

        self.assertEqual(rc, 0)
        output = raw_output.getvalue().decode("cp1252")
        self.assertIn("de-mcp-config:", output)
        self.assertIn("Custom connectors", output)
        self.assertIn("Trust", output)

    def test_cli_write_workbuddy_dry_run_omits_manual_trust_steps(self):
        self._install_workbuddy_fixture()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mcp_config.main(
                ["--write", "--client", "workbuddy", "--dry-run"]
            )

        self.assertEqual(rc, 0)
        self.assertNotIn("Custom connectors", buf.getvalue())

    def test_cli_write_workbuddy_absent_omits_manual_trust_steps(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = mcp_config.main(["--write", "--client", "workbuddy"])

        self.assertEqual(rc, 1)
        self.assertIn("workbuddy_not_installed", stderr.getvalue())
        self.assertNotIn("Custom connectors", stdout.getvalue())

    def test_cli_write_cursor_is_an_explicit_supported_target(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mcp_config.main(["--write", "--client", "cursor"])
        self.assertEqual(rc, 0)
        cursor = Path(os.environ["CURSOR_CONFIG"])
        entry = json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"][
            "decision-engine"
        ]
        self.assertEqual(entry["env"][mcp_config.CLIENT_HOST_ENV], "cursor")
        self.assertIn("RESTART", buf.getvalue())

    def test_cli_write_dry_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mcp_config.main(["--write", "--client", "codex", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(Path(os.environ["CODEX_CONFIG"]).exists())
        self.assertIn("dry-run", buf.getvalue())


class InterpreterGateTestCase(unittest.TestCase):
    """The interpreter RECORDED in a client entry must be able to host the launcher.

    `render_entry` bakes in `python or sys.executable` verbatim, so whatever interpreter
    happened to run the registration becomes the one every Agent launches forever. Two ways
    that value goes wrong in the field, both observed: an interpreter below `requires-python`
    (a stale registration recorded 3.9.6), and an externally-managed one that clears the
    version floor but can never receive a dependency (Homebrew's python@3.13, whose missing
    pywebview surfaced only as a popup failure). The write path refuses both up front, the
    way `_cursor_write_version_gate` refuses an unsupported Cursor.

    Fixtures ANSWER the probe rather than merely exiting: a gate that trusts an exit code
    alone accepts any executable that ignores `-c`, which is what these pin down.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cc = self.tmp / ".claude.json"
        self._prev = os.environ.get("CLAUDE_CODE_CONFIG")
        os.environ["CLAUDE_CODE_CONFIG"] = str(self.cc)
        self._root = mock.patch.object(mcp_config, "registration_root", return_value=self.tmp)
        self._root.start()

    def tearDown(self):
        self._root.stop()
        if self._prev is None:
            os.environ.pop("CLAUDE_CODE_CONFIG", None)
        else:
            os.environ["CLAUDE_CODE_CONFIG"] = self._prev
        self._tmp.cleanup()

    def _stub(self, name: str, body: str) -> str:
        if os.name == "nt":
            path = self.tmp / (name + ".cmd")
            if body.startswith("echo '") and body.endswith("'"):
                body = "@echo off\n@echo " + body[6:-1]
            elif body == "exit 0":
                body = "@exit /b 0"
            path.write_text(body + "\n", encoding="utf-8")
            return str(path)
        path = self.tmp / name
        path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def _healthy(self) -> str:
        return self._stub("healthy", "echo 'DE_PY 3 13'")

    def test_healthy_interpreter_has_no_defect(self):
        self.assertIsNone(mcp_config._interpreter_defect(self._healthy()))

    def test_interpreter_below_the_floor_is_named_as_too_old(self):
        defect = mcp_config._interpreter_defect(self._stub("old", "echo 'DE_PY 3 9'"))
        self.assertIn("3.12", defect)

    def test_an_executable_that_is_not_python_is_refused(self):
        # exits 0 but never answers the probe — an exit code is not evidence of a Python
        self.assertIsNotNone(mcp_config._interpreter_defect(self._stub("silent", "exit 0")))

    def test_an_executable_answering_gibberish_is_refused(self):
        self.assertIsNotNone(
            mcp_config._interpreter_defect(self._stub("noise", "echo 'hello there'"))
        )

    def test_an_interpreter_that_cannot_run_is_refused(self):
        self.assertIsNotNone(mcp_config._interpreter_defect(str(self.tmp / "absent")))

    def test_a_relative_command_is_refused_because_the_agent_resolves_it_elsewhere(self):
        # the gate probes with the installer's PATH and cwd; the Agent resolves the recorded
        # string later, often from a GUI launch with a minimal PATH
        self.assertIsNotNone(mcp_config._interpreter_defect("python3"))

    def test_write_refuses_a_bad_interpreter_and_writes_nothing(self):
        with self.assertRaises(ShellError) as caught:
            mcp_config.write_entry("claude-code", python=self._stub("old2", "echo 'DE_PY 3 9'"))
        self.assertIn("unsupported_python", str(caught.exception))
        self.assertIn("nothing was written", str(caught.exception))
        self.assertFalse(self.cc.exists())

    def test_rejection_does_not_leak_the_interpreter_path(self):
        # host-facing failures in this module deliberately withhold filesystem paths
        bad = self._stub("old3", "echo 'DE_PY 3 9'")
        with self.assertRaises(ShellError) as caught:
            mcp_config.write_entry("claude-code", python=bad)
        self.assertNotIn(bad, str(caught.exception))

    def test_write_refuses_before_clobbering_an_existing_good_entry(self):
        mcp_config.write_entry("claude-code", python=self._healthy())
        good = self.cc.read_text()
        with self.assertRaises(ShellError):
            mcp_config.write_entry("claude-code", python=self._stub("old4", "echo 'DE_PY 3 9'"))
        self.assertEqual(self.cc.read_text(), good)

    def test_write_accepts_a_healthy_interpreter(self):
        result = mcp_config.write_entry("claude-code", python=self._healthy())
        self.assertEqual(result["action"], "added")

    def test_the_running_interpreter_is_probed_not_assumed(self):
        """`sys.executable` gets no shortcut — a running process outlives its own binary.

        Trusting `sys.version_info` for the default case would happily record a path that no
        longer exists after an upgrade moved or removed it: the same unusable entry this gate
        exists to prevent.
        """
        with mock.patch.object(mcp_config.sys, "executable", str(self.tmp / "vanished")):
            with self.assertRaises(ShellError):
                mcp_config.write_entry("claude-code")

    def test_dry_run_is_gated_too(self):
        with self.assertRaises(ShellError):
            mcp_config.write_entry(
                "claude-code", python=self._stub("old5", "echo 'DE_PY 3 9'"), dry_run=True
            )

    def test_codex_toml_client_is_gated_on_the_same_path(self):
        codex = self.tmp / "codex.toml"
        prev = os.environ.get("CODEX_CONFIG")
        os.environ["CODEX_CONFIG"] = str(codex)
        try:
            with self.assertRaises(ShellError):
                mcp_config.write_entry("codex", python=self._stub("old6", "echo 'DE_PY 3 9'"))
            self.assertFalse(codex.exists())
        finally:
            if prev is None:
                os.environ.pop("CODEX_CONFIG", None)
            else:
                os.environ["CODEX_CONFIG"] = prev

    def test_a_venv_interpreter_is_accepted_by_path_and_by_resolved_path(self):
        """Neither spelling of a real venv's interpreter may be refused.

        This gate deliberately does not test PEP 668, and this is why: the marker lives in
        the BASE stdlib, so a venv reports it as its own, and `Path(...).resolve()` on a
        venv's `bin/python3` yields the base interpreter outright. A marker test here would
        therefore refuse a working venv whenever a caller normalised its path — which
        `cursor_activation` does — turning a good interpreter into a registration failure.
        """
        with TemporaryDirectory() as venv_tmp:
            venv_root = Path(venv_tmp) / "venv"
            venv.create(venv_root, with_pip=False)
            interpreter = (
                venv_root / "Scripts" / "python.exe"
                if os.name == "nt"
                else venv_root / "bin" / "python3"
            )
            self.assertIsNone(mcp_config._interpreter_defect(str(interpreter)))
            self.assertIsNone(mcp_config._interpreter_defect(str(interpreter.resolve())))

    def test_the_running_interpreter_registers_cleanly(self):
        # the default path every ordinary registration takes; if this machine's own Python
        # could not be recorded, every install here would fail
        self.assertIsNone(mcp_config._interpreter_defect(sys.executable))


if __name__ == "__main__":
    unittest.main()
