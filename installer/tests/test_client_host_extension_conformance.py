"""Architecture tripwires for normal client-host extensions."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from installer.client_hosts import contract, registry


class ClientHostExtensionConformanceTestCase(unittest.TestCase):
    def test_trae_product_literals_do_not_escape_its_host_adapter(self):
        installer = Path(__file__).resolve().parents[1]
        shared_orchestration = tuple(installer.glob("*.py")) + tuple(
            path
            for path in (installer / "client_hosts").glob("*.py")
            if path.name != "registry.py"
        )
        forbidden = (
            "trae-work",
            "trae_work",
            "trae solo",
        )

        violations = []
        for path in shared_orchestration:
            text = path.read_text(encoding="utf-8").lower()
            if any(literal in text for literal in forbidden):
                violations.append(str(path.relative_to(installer)))

        self.assertEqual(violations, [])

    def test_workbuddy_product_literals_do_not_escape_its_host_adapter(self):
        installer = Path(__file__).resolve().parents[1]
        allowed = {
            installer / "client_hosts" / "hosts" / "codebuddy.py",
            installer / "client_hosts" / "hosts" / "workbuddy.py",
            installer / "client_hosts" / "hosts" / "workbuddy_ai.py",
            installer / "client_hosts" / "hosts" / "workbuddy_ai_prompt_hook.py",
            installer / "workbuddy_audit_prompt_hook.py",
            installer / "client_hosts" / "registry.py",
        }
        shared_orchestration = tuple(
            path
            for path in installer.rglob("*.py")
            if "tests" not in path.relative_to(installer).parts
            and path not in allowed
        )

        violations = []
        for path in shared_orchestration:
            if "workbuddy" in path.read_text(encoding="utf-8").lower():
                violations.append(str(path.relative_to(installer)))

        self.assertEqual(violations, [])

    def test_codebuddy_product_literals_do_not_escape_its_host_adapter(self):
        installer = Path(__file__).resolve().parents[1]
        allowed = {
            installer / "client_hosts" / "hosts" / "codebuddy.py",
            installer / "client_hosts" / "hosts" / "workbuddy_ai.py",
            installer / "client_hosts" / "registry.py",
        }
        shared_orchestration = tuple(
            path
            for path in installer.rglob("*.py")
            if "tests" not in path.relative_to(installer).parts
            and path not in allowed
        )

        violations = []
        for path in shared_orchestration:
            if "codebuddy" in path.read_text(encoding="utf-8").lower():
                violations.append(str(path.relative_to(installer)))

        self.assertEqual(violations, [])

    def test_qoder_product_literals_do_not_escape_its_host_adapter(self):
        installer = Path(__file__).resolve().parents[1]
        allowed = {
            installer / "client_hosts" / "hosts" / "qoder.py",
            installer / "client_hosts" / "hosts" / "qoder_cn.py",
            installer / "client_hosts" / "hosts" / "qoder_ide.py",
            installer / "client_hosts" / "hosts" / "qoder_cn_ide.py",
            installer / "client_hosts" / "hosts" / "qoder_ide_common.py",
            installer / "client_hosts" / "hosts" / "qoder_prompt_hook.py",
            installer / "qoder_audit_prompt_hook.py",
            installer / "client_hosts" / "registry.py",
        }
        shared_orchestration = tuple(
            path
            for path in installer.rglob("*.py")
            if "tests" not in path.relative_to(installer).parts
            and path not in allowed
        )

        violations = []
        for path in shared_orchestration:
            if "qoder" in path.read_text(encoding="utf-8").lower():
                violations.append(str(path.relative_to(installer)))

        self.assertEqual(violations, [])

    def test_registry_order_and_host_ids_are_stable_and_unique(self):
        self.assertEqual(tuple(registry.CLIENT_SPECS), registry.CLIENTS)
        self.assertEqual(len(registry.CLIENTS), len(set(registry.CLIENTS)))
        self.assertTrue(
            all(
                client == spec.id
                for client, spec in registry.CLIENT_SPECS.items()
            )
        )

    def test_shared_contract_accepts_any_positive_number_of_project_skill_paths(self):
        cursor = registry.CLIENT_SPECS["cursor"]
        for paths in (
            (".cursor/skills",),
            (".cursor/skills", ".agents/skills", ".future/skills"),
        ):
            with self.subTest(paths=paths):
                contract.validate_host_specs(
                    {"future-host": replace(cursor, id="future-host", skills_project_paths=paths)}
                )


if __name__ == "__main__":
    unittest.main()
