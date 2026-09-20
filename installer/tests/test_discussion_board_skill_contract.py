"""Contract tests for the discussion-board skill's progressive disclosure."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "discussion-board"
DIAGRAM = SKILL / "references" / "diagram-spec.md"


class DiscussionBoardSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entry = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        cls.diagram = DIAGRAM.read_text(encoding="utf-8") if DIAGRAM.exists() else ""

    def test_description_selects_interactive_user_editing_in_english(self):
        description = json.loads(self.entry.splitlines()[2].removeprefix("description: "))
        self.assertIn("interactive discussion board", description)
        self.assertIn("by hand and submit", description)
        self.assertIn("board-based rearrangement", description)
        self.assertIn("in any language", description)
        self.assertIn("Decision Engine board popup only", description)
        self.assertIn("do not substitute an inline board", description)
        self.assertIn("static visual explanation", description)
        self.assertIn("bare mention of a board without interactive intent", description)
        self.assertTrue(description.isascii())
        self.assertNotIn("/discussion-board", description)
        self.assertNotIn("audit-", description)

    def test_entrypoint_routes_only_diagram_details(self):
        self.assertLess(len(self.entry.splitlines()), 220)
        self.assertIn("references/diagram-spec.md", self.entry)
        self.assertIn("only when", self.entry)
        for stage in ("kanban", "image", "document", "diagram"):
            self.assertIn(f"`{stage}`", self.entry)
        self.assertNotIn("`entity-relationship`", self.entry)
        self.assertTrue(self.diagram.startswith("# Diagram Board Specification"))

    def test_diagram_reference_preserves_every_supported_family(self):
        families = (
            "flow", "mindmap", "tree", "cycle", "concept-map", "org",
            "sequence", "state-machine", "entity-relationship", "truth-table",
            "decision-table", "decision-matrix", "swot", "affinity", "canvas",
            "funnel", "journey", "swimlane", "story-map", "gantt", "venn",
            "fishbone", "quadrants", "timeline",
        )
        for family in families:
            with self.subTest(family=family):
                self.assertIn(f"`{family}`", self.diagram)
        for field in (
            "`nodes`", "`edges`", "`attrs`", "`inputs`", "`outputs`",
            "`values`", "`rules`", "`conditions`", "`actions`", "`cells`",
            "`axes`", "`cluster`", "`lane`", "`t`", "`t1`", "`region`",
            "`col`", "`order`", "`kind`", "`marker`", "`cat`", "`tier`",
            "`value`", "`color`", "`dash`",
        ):
            name = field.strip("`")
            self.assertRegex(self.diagram, rf"`(?:diagram\.)?{re.escape(name)}(?=[:.`])")
        for family, fields in {
            "sequence": ("col:", "order:", "kind:"),
            "state-machine": ("marker:", "shape:"),
            "truth-table": ("inputs:", "outputs:", "values:"),
            "decision-table": ("rules:", "conditions:", "actions:"),
            "decision-matrix": ("diagram.axes", "cells:"),
            "funnel": ("tier:", "axes.tiers", "axes.orientation"),
            "journey": ("col:", "lane:", "axes.emotion"),
            "gantt": ("lane:", "t:", "t1:", "axes.ticks"),
            "venn": ("axes.sets", "region:"),
            "fishbone": ("axes.effect", "axes.cats", "cat:"),
            "quadrants": ("axes.q", "value:"),
            "timeline": ("lane:", "t:", "axes.ticks"),
        }.items():
            section = re.search(
                rf"(?ms)^- `{re.escape(family)}`:(.*?)(?=^- `|^## |\Z)",
                self.diagram,
            )
            self.assertIsNotNone(section, family)
            for field in fields:
                with self.subTest(family=family, field=field):
                    self.assertIn(field, section.group(1))
        self.assertIn("Do NOT pass coordinates", self.diagram)
        self.assertIn("server", self.diagram.lower())

    def test_probe_is_first_and_has_posix_and_powershell_forms(self):
        gate = self.entry.split("## Mandatory capability-state gate", 1)[1].split(
            "## Workflow", 1
        )[0]
        self.assertIn("de_lite_capability_status.py", gate)
        self.assertIn("```bash", gate)
        self.assertIn("```powershell", gate)
        self.assertIn("$env:CODEX_HOME", gate)
        self.assertIn("status=unactivated", gate)
        self.assertIn("status=activated", gate)
        self.assertIn("status=unknown", gate)
        self.assertIn("do not inspect the tool list", gate)
        self.assertIn("If the probe cannot run", gate)
        self.assertLess(
            gate.index("de_lite_capability_status.py"),
            gate.index("do not inspect the tool list"),
        )

    def test_probe_command_runs_on_the_host_with_synthetic_unactivated_config(self):
        if os.name == "nt":
            shell = shutil.which("powershell") or shutil.which("pwsh")
        else:
            shell = shutil.which("bash")
        if not shell:
            self.skipTest("host shell unavailable")
        language = "powershell" if os.name == "nt" else "bash"
        block = self.entry.split(f"```{language}\n", 1)[1].split("\n```", 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["CODEX_HOME"] = str(ROOT)
            env["DE_DEVICE_CONFIG_PATH"] = str(Path(temp) / "missing.json")
            args = [shell, "-NoProfile", "-Command", block] if os.name == "nt" else [shell, "-c", block]
            result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "unactivated"})

    def test_result_and_failure_contracts_stay_in_entrypoint(self):
        for phrase in (
            "open_db_board", "db_board_result", "popup_id", "status:\"done\"",
            "status:\"dismissed\"", "status:\"unknown\"", "Fail-closed, never degrade",
            "Never a browser fallback", "DE Lite 不具备讨论板能力",
        ):
            self.assertIn(phrase, self.entry)
        self.assertIn(
            "Do not turn a failed board request into an audit of the board content.",
            " ".join(self.entry.split()),
        )

    def test_reference_and_contract_test_are_in_public_release_inventory(self):
        allowlist = (ROOT / "internal/public_release/allowlist.txt").read_text(
            encoding="utf-8"
        )
        manifest = (ROOT / "internal/public_release/content-manifest.sha256").read_text(
            encoding="utf-8"
        )
        digests = dict(
            (path, digest) for digest, path in (
                line.split("  ", 1) for line in manifest.splitlines()
                if line and not line.startswith("#")
            )
        )
        for path in (
            "skills/discussion-board/references/diagram-spec.md",
            "installer/tests/test_discussion_board_skill_contract.py",
        ):
            self.assertIn(path, allowlist.splitlines())
            canonical_bytes = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(digests[path], hashlib.sha256(canonical_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
