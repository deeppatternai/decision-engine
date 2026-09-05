---
name: audit-writing-plans
description: "Producer skill that transforms upstream audit conclusions (from /audit-brainstorming, /audit-market-research, or /audit-adjudication) into structured engineering / implementation documentation — primary human-readable doc first (in the user's input language), then on-demand an agent-executable companion. Differentiator: runs multi-LLM cross-validation (a cross-vendor panel of frontier models) on the drafted plan with pre-mortem framing before promoting it to implementable. Distinct from /audit (which reviews existing artifacts for defects, not produces new ones). Triggers on producer-direction phrases: 中文 \"根据头脑风暴的结果出一份文档\" / \"把 audit 结论转成实施文档\" / \"起草一份实施 spec\" / \"落实施\" / \"/audit-writing-plans\"; English \"produce an implementation doc from audit findings\" / \"convert audit results into implementation plan\" / \"/audit-writing-plans\". Short standalone words (plan, doc, spec, 计划, 文档, 实施) DO NOT auto-trigger — picker must see producer-direction + upstream-audit co-occurrence."
---

# /audit-writing-plans — Producer: audit conclusions → engineering docs

> **Thin client routing skill** for the Decision Engine hub server.
> PRODUCER skill: you (the client) draft the doc; the SERVER runs the Phase 3
> plan-quality VALIDATE panel (prescriptive intent + server-side pre-mortem
> framing — that template is NOT in this file and never reaches the client).

## When to use vs NOT

| Use this skill | Use instead |
|---|---|
| Settled `/audit-brainstorming` / `/audit-market-research` conclusions → impl doc | Review artifact for defects → `/audit` |
| `/audit-adjudication` table → operationalize | Stress-test an unsettled idea → `/audit-brainstorming` |
| Want a plan carrying multi-LLM convergence trust signal | Code-only plan, no upstream audit → superpowers `writing-plans` |

**Dedup**: same upstream audit_id set + intent within 5 turns, no upstream change → offer re-render only (mechanism impl-dependent, v2 MVP).

## Workflow — client drafts, server validates

- **[1] INGEST** (client): detect input language → BCP-47 (`und-*` → ASK, never silent-fallback). Upstream: **(a)** audit_id list (highest trust) · **(b)** adjudication table · **(c)** manual paste (low — caveat "no upstream convergence"). Extract per source: accepted decisions / open questions / convergence signal / divergent items. Set `profile` ∈ {software, ops, research, content, ml, mixed}.
- **[2] DRAFT** (client): write `docs/plans/YYYY-MM-DD-<slug>-doc.md`. Headings + body in user's language, frontmatter keys English. **All sections required — no TBD/placeholder.** Every Goal/Decision `traces_to:` an upstream id or is `[author-added]`.
- **[3] VALIDATE** (SERVER — the differentiator): see calling pattern below.
- **[4] PRESENT + ASK** (client): show doc path + validation summary (convergence, 0 blocking, divergent count, run_id), then **ask in the user's language** whether to derive the agent companion. Do NOT auto-derive.
- **[5a] EXTRACT** (client, only on explicit yes): `…-tasks.md` — **pure mechanical restructure** of the Implementation (task list) section's task fields (already validated); net-new content → STOP, loop to [2]. Agent doc inherits `linked_doc_validated_by` + status (primary `review` → agent `review` = non-executable).
- **[5b] STOP** (no): primary doc only.

### Phase 3 calling pattern
```
mcp__decision-engine__audit_skill_submit(
    skill_name="audit-writing-plans",
    args={
        "title": "<project> — plan quality validation",
        "content": "<full primary doc>",
        "context": "Upstream audit refs: <ids + convergence>",  # optional
        "stakes": "medium",       # severity hint (low/medium/high/irreversible) — trust signal only
        "mode": "standard",       # fast | standard | deep — controls panel size (default standard)
        # artifact_intent FORCED "prescriptive"; pre-mortem framing + plan-quality
        # focus are injected SERVER-side — what you pass for those is overridden.
    },
)  → envelope {run_id, trust_signals, payload}
# Wait by POLLING (wait_audit is deprecated): loop check_audit_status(run_id) with backoff
# (~10s cadence), re-render per-auditor auditors[] each round; on terminal status fetch the
# payload with audit_skill_result. Unknown/error status (run_not_found after the run was seen,
# or a transport error) → surface + STOP; keep a total timeout / stay interruptible. Full loop
# + fallback nuance: see /audit "Waiting for the panel".
audit_skill_status / _result / _events / _cancel   (LLM-driven 30-60s; poll check_audit_status)
```

### You're the author — stay the adjudicator
The panel is your INDEPENDENT pre-mortem of a plan YOU drafted, so don't double-count yourself (the `convergent_count` "X/N" is the panel's, not +you). While it runs, pre-mortem your own draft too — catch failure modes before results land. (Validating someone ELSE's plan? Then also contribute your own review.)

### Phase 3.1 outcome routing (client adjudication)
| Outcome (from trust_signals) | Action |
|---|---|
| All clear (0 blocking, convergent_verdict ≥ majority) | Write `validated_by`; primary `status: accepted`; → [4] |
| Convergent blocking (≥2 auditors, major/critical) | Back to [2] to fix; re-validate. **No Owner override.** |
| Divergent on key claim (panel split) | STOP — Owner decides: accept-residual (`status: review`) / iterate / cancel |
| Audit failed / cancelled / timeout | Surface reason + run_id; don't ship as validated; offer retry |

## Primary doc sections (user's language; frontmatter keys English)
1 背景/Context · 2 目标+非目标/Goals (`g<N>` ids) · 3 Upstream Conclusions (frozen, `traces_to`) · 4 架构/Architecture · 5 关键决策/Decisions (`d<N>`, Y-statement ADR) · 6 横切关注点/Cross-Cutting (6 fixed slots `cc-security/privacy/observability/cost/dependencies/reversibility`; empty → `N/A — <reason>`) · 7 实施路径/Implementation (task list: `id/inputs/outputs/verification/acceptance/dependencies/traces_to/risk_class/estimated_effort/description`) · 8 风险/Risks (tail risks) · 9 开放问题/Open Questions.

`validated_by` (Phase 3 output): `audit_id` · `validated_at` · `artifact_sha256` (canonical payload, excludes mutable list) · `auditors` · `convergence` signal · `pre_mortem_pass` (true|false|accepted_with_residual) · `divergent_items`. Editing any IMMUTABLE field after validation breaks the sha → re-run Phase 3.

## Trust signals (envelope.trust_signals)
| Signal | Meaning |
|---|---|
| `canonical_sha` | sha256 over payload (always present) |
| `overall_verdict` | solid / has-gaps / has-serious-issues / fundamentally-flawed |
| `findings_count` / `blocking_findings_count` / `dimension_blocking_count` | defect counts |
| `pre_mortem_applied` | `true` — server confirms it applied pre-mortem framing |
| `panel_size` / `convergent_verdict` / `convergent_count` | multi-審 only (X/N) |

Convergence is high-CONFIDENCE not truth (convergence-quality discipline); divergence on a key claim is high-INFORMATION → surface as gap signal, not noise.

## Anti-patterns
- ❌ Auto-derive agent doc without asking (Phase 4 prompt mandatory)
- ❌ Silently accept a convergent blocking finding (fix or surface)
- ❌ Silent language fallback; manual-paste without the no-trust-signal caveat
- ❌ TBD / placeholder / empty cross-cutting slot
- ❌ Edit immutable primary-doc field after `validated_by` without re-running Phase 3

## Routing (chain)
```
vague idea → /audit-explore → /audit-brainstorming OR /audit-market-research
          → /audit-writing-plans (this skill) → /audit-adjudication → /audit (if code)
```

## DOCX report table style (shared convention)
The primary doc is Markdown; but when you render/export it (or a deliverable table) to a Word
(.docx) report, use this cell style — same convention as /audit-market-research — so text is
vertically centered and never presses/overflows the border (the yoga-socks-report fix):
- **Every cell `vAlign=center`** — docx-js `verticalAlign: VerticalAlign.CENTER`; raw XML = a `<w:vAlign w:val="center"/>` in every `<w:tcPr>`.
- **In-cell paragraphs: `spacing { after: 0 }`, single line** — no `w:after`, no `w:line` > 240 inside a cell; after-space + an oversized line multiplier push text low and onto the bottom border.
- **Symmetric cell margins ≈ top/bottom 100 twips, left/right 120–160** — docx-js `margins: { top: 100, bottom: 100, left: 140, right: 140 }`.
- Plus the standard docx-skill table rules: DXA widths on cell + columnWidths, `ShadingType.CLEAR`, never `WidthType.PERCENTAGE`; no hard cell borders (clean look).
- **Font**: body Georgia (Latin) + LiSong Pro (CJK) 12pt (`sz 24`); title ~28pt.
- **Palette** (warm brown/cream house style; a customer may override): header fill `5E4A22` + white text `FFFFFF`; rows alternate white / cream `FAF6EE`; brown labels/accents `8C6A2E`; secondary gray `595959`/`808080`.
- **Semantic colors** (shared meaning; each skill maps to its own content): success/high `1E7D32` · medium `9A6700` · warning/low `B3261E`.
- **Scope**: cell style + visual identity ONLY — does NOT dictate the document framework; each skill keeps its own sections/content.

## Pre-flight
- Hub reachable (`mcp__decision-engine__check_provider_health`); device token configured
- Want the full panel → `mode="deep"`; `stakes` is a severity hint, not a panel control
- `validated_by` must come from a terminal `audit_skill_result` (poll `check_audit_status`
  to terminal first); never from a `pending` / non-terminal result
