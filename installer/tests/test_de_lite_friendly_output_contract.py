"""Behavior contracts for DE Lite user-facing degraded responses."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
AUDIT_ENTRY = ROOT / "skills" / "audit" / "SKILL.md"
HOSTED = ROOT / "skills" / "audit" / "references" / "hosted-workflow.md"
RESULT = ROOT / "skills" / "audit" / "references" / "result-contract.md"
ROUTING = ROOT / "skills" / "audit" / "references" / "de-lite-routing.md"
RESPONSES = ROOT / "skills" / "audit" / "references" / "localized-responses.md"
GRAPHIC = ROOT / "skills" / "graphic-explanation" / "SKILL.md"
BOARD = ROOT / "skills" / "discussion-board" / "SKILL.md"

UNACTIVATED_FINAL = (
    "以上审核结果基于本地单模型模拟多声部审核来完成，如需更可信的审核结果，"
    "您可以注册激活Decision Engine后再来提交审核。"
)
UNACTIVATED_FINAL_EN = (
    "This review was completed using a local single-model simulation of a multi-voice "
    "review. For a more trustworthy review, register and activate Decision Engine, "
    "then submit the review again."
)
UNACTIVATED_BOARD = (
    "我这边现在没法直接调用讨论板：Decision Engine 尚未激活，DE Lite 不具备讨论板能力。"
    "如果您愿意先激活这台设备，我就能继续。"
)
UNACTIVATED_BOARD_EN = (
    "I can't open a discussion board right now: Decision Engine is not activated, and "
    "DE Lite does not support discussion boards. If you activate this device first, "
    "I can continue."
)


class DeLiteFriendlyOutputContractTests(unittest.TestCase):
    def test_reason_aware_response_contracts_live_in_the_selected_skills(self):
        audit = RESPONSES.read_text(encoding="utf-8")
        for required in (
            "## DE Lite friendly final response",
            "MCP 不可用",
            "服务不可用",
            "订阅已到期",
            "积分不足",
            "本地单模型模拟多声部审核",
            "仅供参考",
            "不能关闭 AQG gate",
        ):
            self.assertIn(required, audit)
        for path in (GRAPHIC, BOARD):
            text = path.read_text(encoding="utf-8")
            self.assertIn("DE Lite unsupported-capability response", text)
            self.assertIn("不得启动 DE Lite 本地审核", text)

    def test_audit_completion_has_all_degraded_reason_templates(self):
        text = RESPONSES.read_text(encoding="utf-8")
        for reason in (
            "未激活",
            "MCP 不可用",
            "服务不可用",
            "订阅到期",
            "积分不足",
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, text)

    def test_received_hosted_balance_marker_routes_once_and_fails_closed(self):
        text = ROUTING.read_text(encoding="utf-8")
        for required in (
            "authenticated Hosted MCP submit",
            "wait/status/result/events follow-up",
            "literal `insufficient_balance`",
            "credits_exhausted/add_credits",
            "one local advisory conversion",
            "never submits Hosted again",
            "Do not match language",
            "wrapper",
            "run IDs",
            "`isError`",
            "timeout",
            "reset",
            "5xx",
            "401",
            "TLS/redirect",
            "no-replay",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

    def test_insufficient_credit_templates_are_bilingual_and_aligned(self):
        text = RESPONSES.read_text(encoding="utf-8")
        self.assertIn("**积分不足**", text)
        self.assertIn("由于 Decision Engine 积分不足，补充积分后", text)
        self.assertIn("**insufficient credits**", text)
        self.assertIn(
            "Because there are insufficient Decision Engine credits, add credits",
            text,
        )
        self.assertNotIn("积分耗尽", text)
        self.assertNotIn("积分已用完", text)
        self.assertNotIn("credits are exhausted", text)

    def test_hosted_success_excludes_all_lite_notes(self):
        localized = RESPONSES.read_text(encoding="utf-8")
        result = RESULT.read_text(encoding="utf-8")
        self.assertIn("A hosted result must not append a", localized)
        self.assertIn("entitlement", localized)
        self.assertIn("hosted result must not append any DE Lite", result)
        self.assertIn("server result in the current conversation language", result)

    def test_green_de_lite_completion_is_not_a_pass_verdict(self):
        routing = ROUTING.read_text(encoding="utf-8")
        responses = RESPONSES.read_text(encoding="utf-8")
        self.assertIn("green `✓`", routing)
        self.assertIn("execution completion only", routing)
        self.assertIn("never carries a `clean` or `pass` verdict", routing)
        self.assertIn("never\n  audit approval", routing)
        for required in (
            "审核结果基于 DE Lite 本地单模型模拟多声部审核完成",
            "不等同于 Decision Engine 服务端审核",
            "不能关闭 AQG gate",
            "注册激活Decision Engine",
            "最终说明必须作为最后一段输出",
        ):
            self.assertIn(required, responses)

    def test_unactivated_final_template_is_exact_and_does_not_expose_internal_terms(self):
        text = RESPONSES.read_text(encoding="utf-8")
        self.assertIn(UNACTIVATED_FINAL, text)
        self.assertIn("未激活优先于其他本地降级原因", text)
        self.assertNotIn("AQG gate", text[text.index(UNACTIVATED_FINAL):
                                             text.index(UNACTIVATED_FINAL) + len(UNACTIVATED_FINAL)])

        # The unactivated product response is intentionally short and product-facing. Internal
        # routing/tool words belong in the contract, never in the exact final paragraph.
        for forbidden in ("DE Lite", "AQG", "MCP", "audit_skill", "local-bridge", "工具"):
            self.assertNotIn(forbidden, UNACTIVATED_FINAL)

    def test_unactivated_audit_sanitizes_the_entire_user_response(self):
        text = RESPONSES.read_text(encoding="utf-8")
        heading = "## Unactivated user-response boundary"
        self.assertIn(heading, text)
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]

        for required in (
            "entire user-facing response",
            "not only the final paragraph",
            "plain-language findings",
            "local_*",
            "run ID",
            "advisory-only",
            "external panel",
            "DE Lite",
            "MCP",
            "AQG",
            "Skill",
            "bridge",
            "tool or command names",
            "gate",
            "Nothing may follow it",
        ):
            with self.subTest(required=required):
                self.assertIn(required, section)
        self.assertIn(
            "The only permitted product-name occurrence is `Decision Engine` inside the exact "
            "final template",
            " ".join(section.split()),
        )
        self.assertNotIn(
            "Product names may remain `Decision Engine` and `DE Lite`",
            section,
        )

    def test_unactivated_audit_english_template_is_exact_and_single_language(self):
        text = RESPONSES.read_text(encoding="utf-8")
        self.assertIn(UNACTIVATED_FINAL_EN, text)
        self.assertNotRegex(UNACTIVATED_FINAL_EN, r"[\u3400-\u9fff]")
        self.assertIn("all findings and the final paragraph", text)

    def test_complete_unactivated_advisory_must_submit_completed_status(self):
        text = ROUTING.read_text(encoding="utf-8")
        heading = "## DE Lite / unactivated authorized audit"
        self.assertIn(heading, text)
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(section.split())

        for required in (
            "all five review dimensions",
            "ledger `validate` succeeds",
            "audit_skill_complete(local_id=<the returned local_* id>, status=completed)",
            "must use `completed`",
            "output sanitization",
            "single-model capability limit",
            "reference-only",
            "cannot close the gate",
            "are not reasons to use `partial`",
            "`partial` is allowed only when review work, one or more dimensions, or the ledger "
            "is actually incomplete",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

    def test_contract_requires_current_session_locale_and_english_counterpart(self):
        hosted = HOSTED.read_text(encoding="utf-8")
        responses = RESPONSES.read_text(encoding="utf-8")
        self.assertIn("current conversation", hosted)
        self.assertIn("ui_locale", hosted)
        self.assertIn("This review was completed using a local single-model simulation", responses)
        self.assertIn("do not mix", responses.lower())
        self.assertIn("findings", responses)

    def test_hosted_audit_does_not_append_a_lite_or_unactivated_final_note(self):
        text = RESULT.read_text(encoding="utf-8")
        self.assertTrue(
            "hosted" in text.lower() and "must not append" in text.lower()
            or "hosted" in text.lower() and "do not append" in text.lower()
            or "hosted" in text.lower() and "不得追加" in text
        )
        self.assertTrue(
            "hosted result" in text.lower() or "server result" in text.lower()
            or "服务端结果" in text
        )

    def test_graphic_lite_failure_is_friendly_and_never_audit_fallback(self):
        text = GRAPHIC.read_text(encoding="utf-8")
        for required in (
            "de_lite_capability_status.py",
            "status=unactivated",
            "status=unknown",
            "takes priority over a missing MCP tool",
            "DE Lite 暂不具备图解能力",
            "DE Lite 暂不具备漫解能力",
            "如果您愿意先激活这台设备",
            "不得启动 DE Lite 本地审核",
            "不得调用 audit_skill_submit",
        ):
            self.assertIn(required, text)

    def test_board_lite_failure_is_friendly_and_never_audit_fallback(self):
        text = BOARD.read_text(encoding="utf-8")
        for required in (
            "de_lite_capability_status.py",
            "status=unactivated",
            "status=unknown",
            "takes priority over a missing MCP tool",
            "DE Lite 不具备讨论板能力",
            "如果您愿意先激活这台设备",
            "不得启动 DE Lite 本地审核",
            "不得调用 audit_skill_submit",
        ):
            self.assertIn(required, text)

    def test_board_unactivated_templates_are_fixed_and_language_pure(self):
        text = BOARD.read_text(encoding="utf-8")
        self.assertIn(f"> {UNACTIVATED_BOARD}", text)
        self.assertIn(f"> {UNACTIVATED_BOARD_EN}", text)
        self.assertNotRegex(UNACTIVATED_BOARD_EN, r"[\u3400-\u9fff]")
        self.assertNotIn("DE Lite 暂不具备讨论板能力", text)
        self.assertIn("output only the matching template and stop", text)
        self.assertIn("Do not mix the two languages", text)

    def test_board_capability_probe_precedes_tool_checks_and_calls(self):
        text = BOARD.read_text(encoding="utf-8")
        gate_heading = "## Mandatory capability-state gate"
        gate_start = text.index(gate_heading)
        workflow_start = text.index("## Workflow")
        self.assertLess(gate_start, workflow_start)

        gate = text[gate_start:workflow_start]
        normalized_gate = " ".join(gate.split())
        for required in (
            "For every discussion-board request",
            "Before inspecting the current task's MCP/tool list",
            "checking whether `open_db_board` exists",
            "assembling the board spec",
            "calling `open_db_board`",
            "de_lite_capability_status.py",
            "do not inspect the tool list",
            "do not call `open_db_board`",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized_gate)

        self.assertLess(
            gate.index("de_lite_capability_status.py"),
            gate.index("do not inspect the tool list"),
        )

    def test_capability_preflight_is_in_each_display_skill(self):
        for path in (GRAPHIC, BOARD):
            text = path.read_text(encoding="utf-8")
            normalized = " ".join(text.split())
            for required in (
                "de_lite_capability_status.py",
                "status=unactivated",
                "status=activated",
                "status=unknown",
                "unactivated` is authoritative",
                "unknown` must never be rewritten as unactivated",
            ):
                self.assertIn(required, text)
            self.assertIn("cannot run", text)
            self.assertIn("non-zero", text)
            self.assertIn("unparseable", text)
            self.assertIn("treat the result as `status=unknown`", normalized)

    def test_reason_labels_do_not_replace_the_authorized_audit_title(self):
        responses = RESPONSES.read_text(encoding="utf-8")
        routing = ROUTING.read_text(encoding="utf-8")
        self.assertIn("reason belongs in the status line", responses)
        self.assertIn("title remains the authorized audit title", responses)
        self.assertIn("native Stopper keeps the authorized audit title", routing)
        self.assertIn("authorized local advisory", responses)
        self.assertNotIn("标题显示为 MCP 不可用", responses)
        self.assertNotIn("标题显示为 服务不可用", responses)


if __name__ == "__main__":
    unittest.main()
