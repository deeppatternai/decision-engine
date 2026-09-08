---
name: audit-market-research
description: "Research a market, competitor set, commercial opportunity, customer segment, or go-to-market question using retrieval-grounded evidence plus independent external analysis. Use for 市场调研、竞品研究、商业洞察、趋势分析、GTM、TAM/SAM/SOM, “research this market”, “compare competitors”, or /audit-market-research. Produce sourced insights, risks, and implications. Do not use for defect review, pure forecasting, or an unformed idea that first needs hypothesis development."
---

# /audit-market-research — Commercial insight generator (multi-LLM, convergence-meta-aware)

> **Thin client routing skill** for the Decision Engine hub server.
> You (the client) run the public parts — P0 consent, P1 scoping, the regulated-local
> degraded brief, and the final user-language presentation. The SERVER runs P2-P5 and
> holds ALL the deep methodology IP — none of that lives here.
>
> ⚠️ **BUILD STATUS (tier-aware)**: **premium** has REAL ground-truth retrieval
> (search-grounded retrieval across multiple web, social, Q&A, and financial sources,
> including deeper regional-language research) AND a REAL synthetic-
> customer persona panel (P4). **deep is now REAL too** (real server-side retrieval fanned
> out over the sub-questions with cross-vendor grounding); **only quick still uses the MOCK fact pack** →
> `degraded_mode=mock_ground_truth` (INFRASTRUCTURE, **NOT real insight**). So GATE ON THE TIER:
> on QUICK surface the prominent mock banner + do NOT chain downstream as validated; on
> deep + premium the pack is real (still honor `degraded_reasons[]` for partial source failures).
> Some premium sources depend on server-side API keys (managed on the hub); a consumer-voice
> source attaches on `research_type=consumer_voc` (or consumer keywords).

## When to use vs NOT

| Use this skill | Use instead |
|---|---|
| Market / GTM / commercial-research question → produce an insight doc | Defect review of a shipping artifact → `/audit` |
| Need ground-truth-anchored multi-vendor analysis + honest cross-vendor trust | Stress-test an ALREADY-formed hypothesis → `/audit-brainstorming` |
| Have an upstream `/audit-explore` hypothesis to validate commercially | Vague idea not yet hypothesis-class → `/audit-explore` |
| Want synthetic-customer GO/PIVOT/KILL on candidate insights | Operationalize settled conclusions into an impl doc → `/audit-writing-plans` |

Dedup: same question explored within 5 turns + no material change → re-render, don't re-run.

## Workflow (5 phases — CLIENT orchestrates ordering; SERVER runs P2-P5)

Client-orchestrated, **no server state machine**. Each server phase is its own
`audit_skill_submit` → run. **Server-owned artifacts**: to trigger the next phase pass only the
prior phase's `run_id` + `artifact_sha` POINTER — the server fetches + verifies it; you never
round-trip the fact pack / analyses through your context.

- **[P0 CONSENT]** (client, MANDATORY): data sensitivity (public/internal/confidential/regulated) +
  cost/mode + convergence meta-risk notice if the topic touches AI/LLM vendor self-interest + BCP-47 language.
  `regulated` → handle ENTIRELY client-side as a generic degraded brief (do NOT call the server).
- **[P1 SCOPE]** (client): BCP-47 gate, input mode (text/audit_id/URL/topic), research type+method,
  scope envelope. Pass the SAME scope fields on every server call (server binds all phases to one `scope_sha`).
- **[P2 GROUND TRUTH]** (server): `phase=ground_truth` → fact pack + `artifact_sha`. **premium = REAL
  full retrieval across the broadest source set; deep = REAL (real server-side retrieval fanned
  out over the sub-questions with cross-vendor grounding); quick = MOCK →
  `degraded_mode=mock_ground_truth`.** Quick mode STOPS here (render the evidence brief client-side).
  *Deep retrieval is server-side: P2 fans out over the core sub-questions (size / players+share /
  growth / moat), pooled + deduped into one pack — no extra caller round-trips.*
- **[P3 MULTI-LENS]** (server, deep+): `phase=multi_lens` + P2 pointer → FRAMEWORK-FREE panel (vendors
  self-declare frameworks; server runs `framework_diversity_check` POST). Do NOT pass frameworks/lenses.
- **[P4 SYNTHETIC CUSTOMER]** (server, conditional, optional): `phase=synthetic_customer` + P2+P3
  pointers → **premium = REAL fixed-vendor persona panel** (GO/PIVOT/KILL); deep = deterministic mock;
  OR auto-skipped with `p4_skipped_reason`.
- **[P5 SYNTHESIZE]** (server): `phase=synthesize` + P2+P3(+P4) pointers → synthesis panel + server
  deterministic enforcement → final insight doc in `payload.synthesis`. **Render it in the user's
  BCP-47 language** (jargon glossed once; no raw schema as prose).

### Calling pattern
`mcp__decision-engine__audit_skill_submit(skill_name="audit-market-research", args={...})`,
one call per phase, then `audit_skill_status / _result / _events / _cancel` (panel runs ~30-60s).
Wait by POLLING (`wait_audit` is deprecated): loop `check_audit_status(run_id)` with backoff
(~10s cadence), re-render per-auditor `auditors[]` each round; on terminal status fetch the phase
payload with `audit_skill_result`. Unknown/error status (`run_not_found` after the run was seen, or
a transport error) → surface + STOP; keep a total timeout / stay interruptible. Full loop + fallback
nuance: see /audit "Waiting for the panel". Common args (per phase):
- **all phases**: `phase` (ground_truth | multi_lens | synthetic_customer | synthesize), `mode`
  (quick | deep | premium — NOT regulated_local), `title`, + the SCOPE fields verbatim on every
  call (`research_type`, `research_method`, `language`, `question`, `topic`, `framework_hints`).
- **P2**: `content` = topic+question (the retrieval query). → terminal `trust_signals.artifact_sha` = `sha_P2`.
- **P3/P4/P5**: pass the upstream POINTERS, never the content — `upstream_p2_run_id` + `upstream_p2_artifact_sha`
  (P3+), `upstream_p3_*` (P4/P5), `upstream_p4_*` (P5, optional). P3: `stakes=high` or `contrarian=true`
  → server forces a contrarian vendor; `vendor_self_interest_topic=true` if the vendor-self-interest trigger fired.
  P4: `calibration_present=true` unlocks caveat domains.

**Runtime-LLM aware panel**: the calling LLM is orchestrator+adjudicator — keep it OUT of the panel
(no double-counting). Self-identify your runtime + pick mode so the server composes a complementary
cross-vendor panel that excludes your own model family (regional-language voices added on relevant
topics). Can't self-identify → ASK, never silently default.

### Search coverage — must-check routing + tier philosophy (P2)
No single source reaches every high-value site, so the server routes each high-value source to
whichever panel voice can actually reach it; coverage spans mainstream review, social, Q&A, and
financial sites across languages, and premium widens this to paid / professional / broader sources.
- **Tier philosophy**: **Deep** covers the mainstream must-check sites via the panel's search-capable
  voices + your own caller-side search — mainstream coverage = 合格. **Premium = Deep + 付费/专业/更广**
  (broader paid / professional sources + future regional paid APIs); do NOT degrade Deep just because
  it's the cheaper tier.
- **YOUR must-check (do this during P3 self-participation)**: a few sites are reachable ONLY by you, the
  calling agent — fetch them via `WebSearch` `allowed_domains`: **小红书 xiaohongshu.com**, **quora.com**
  (UGC), **xueqiu.com**, **coinglass.com** (financial). The server voices can't reach these; cite what
  you find as normal provenance.
- **Local-language WebSearch rule (HARD, Owner 2026-05-29 — applies to BOTH Deep AND Premium; every tier
  that runs caller-side WebSearch. Quick = MOCK = N/A.)** — for every international slice present in
  the topic you MUST issue queries in the LOCAL language; translated-English on a non-English regional
  source loses 3-5× recall (same caller code path in both Deep and Premium — not a Premium-only rule).
  **Japan** (Kabutan / Nikkei / 株探 / 外国人投資家 日本株) → 日文; **China** (eastmoney /
  同花顺 / 北向资金 / 雪球) → 中文; **Korea** (Naver Finance / KOSPI 외국인) → 한국어 + EN dual-shot;
  **Europe** → EN + 法/德 selectively; **LatAm** → português / español + EN; **MENA** → العربية + EN.
  ❌ **Anti-pattern**: 用户中文出题 → caller 全用英文 query 搜海外本地源 → 召回率严重不足
  (verified 2026-05-29 Premium run; missed 北向资金 + 外国人投資家 major stories — but the same gap
  exists on Deep, so the rule applies to both tiers).

## Trust signals (envelope.trust_signals + payload.synthesis)
- `artifact_sha` — binding sha of THIS phase's core artifact → pass as the next phase's `upstream_*_artifact_sha`.
- `scope_sha` / `consumed_input_run_ids` — server binds all phases to one scope + a verified dependency chain (mix-and-match rejected).
- `degraded_mode` / `degraded_reasons[]` — `mock_ground_truth` (deep/quick floor) / `ground_truth_partial` / `panel_failure` / `p5_enforcement_partial` / …
- `framework_diversity_check` (HIGH/MEDIUM/LOW) · `convergent_*` (X/N) · `p4_skipped_reason`.
- `payload.synthesis.insights[]`: server-computed `trust_tier` (HIGH/MEDIUM/LOW/VERY_LOW — you CANNOT override) + per-insight flags `q_u_failed` / `quantified_threshold_missing` / `tension_analysis_missing`.

## Convergence-quality discipline (apply client-side when presenting)
- **Cross-vendor convergence is a WARNING, not automatic high-confidence** — render the server's
  `trust_tier`, `framework_diversity_check`, and `degraded_reasons` PROMINENTLY; never bury a low / `VERY_LOW` value.
- **Mandatory final round is the quality bar** — always show the falsify / not-represented / base-rate answers, even on 3/3 convergence.
- **User-language-first** — render the insight doc in the user's BCP-47 language; explain each technical term once; never paste raw schema as a prose sentence.

### Run-quality preamble — report the SIGNALS, not the MECHANISM (Owner 2026-07-24)
The trust/quality preamble that opens a delivered report (the "运行质量与信任分布 / Run quality & trust"
block) states the run's honest quality PARAMETERS — it does **NOT** narrate the internal pipeline
architecture, and it does **NOT** headline analysis-voice / panel-return COUNTS. The reader needs the
trust IMPACT, not the machinery. Keep it to these one-liners, in `scope.language`, and skip any line the
envelope doesn't carry:

- One honesty framing line only — e.g. "以下是本次运行的真实质量参数，不作美化。" / "The run's real quality
  parameters, unvarnished." **Do NOT** precede it with a stage-by-stage walkthrough of the pipeline
  (retrieval → panel → synthetic-customer → synthesis). The reader is buying an insight, not a tour.
- **检索层 / Retrieval** — real vs searchless, source count, query-passes, and any coverage GAP (failed
  SEARCH sources / `ground_truth_partial`). For a fast/quick run: "based on model prior · no retrieval".
- **框架多样性 / Framework diversity** — `framework_diversity_check` (HIGH/MEDIUM/LOW) + one clause on what
  it costs independence. (You MAY note generically that "several voices reached for similar frameworks" —
  a diversity statement, NOT a return-count tally.)
- **最终轮质量 / Final-round quality** — `final_round_quality`; anchor-facts count if `panel_anchored`.
- **结论信任分布 / Per-conclusion trust distribution** — the tier spread across insights (e.g. "1 HIGH · 3
  LOW · 1 divergent") + the one-line "this spread is itself a finding" honesty note when it skews low.

**Do NOT** write a "分析面板：N 个声部中仅 M 个成功返回…" line, or any sentence that discounts convergence by
a raw voice-return count. A voice that failed to return is ALREADY priced into `framework_diversity_check`
and the per-insight `trust_tier`; surface it through those signals. This also upholds Voice-name privacy —
a non-debug reader never sees a vendor name **or a voice count**, only the derived trust signals.

## Adjudicator-side checklist — 2026-05-29 Owner hardening (caller verification on server-returned envelopes)
- [ ] **Premium tier voice composition**: on `mode=premium` runs, verify `envelope.trust_signals.sources_failed[]` is empty (or, if not empty, EXPLICITLY surface a count-free warning that the run is no longer Premium-quality). The Premium panel spans the full cross-vendor reasoner + searcher roster (held server-side, with regional-language voices on relevant topics). Missing sources MUST appear in `sources_failed`; surface the quality impact without exposing vendor names or a raw voice count — NEVER silently drop the downgrade.
- [ ] **Premium P3 panel is the full reasoner set**: a missing reasoner in Premium P3 = silent tier-downgrade to the Deep panel — a smaller panel weakens the cross-vendor agreement signal (fewer independent votes). Verify the full panel fired, or the missing voice is surfaced in `sources_failed`.
- [ ] **P3 returned `insights[]` (GENERATION shape), NOT audit verdicts**: if any vendor's `per_vendor_analysis[i]` carries `overall_verdict: fundamentally-flawed` or similar audit-frame output (instead of `insights`), the server used the wrong `artifact_intent` — STOP and surface the panel failure to the user. Do NOT fall back to caller-solo synthesis.
- [ ] **P5 synthesis-of-record came from the non-caller frontier**, NOT orchestrator-solo: verify `payload.synthesis.synthesizer` is a non-caller frontier model, NOT the caller itself. Orchestrator-solo P5 is the worst self-serving fail mode.
- [ ] **P4 ran or has a VALID skip reason**: `p4_skipped_reason` must be one of `regulated_no_calibration` (regulated data + no calibration) OR `mode_skip` (Fast/Deep). **`not_consumer_purchase` is NOT a valid skip reason** — for capital-flow / B2B / supply-chain / regulatory / institutional research topics the server should reframe to institutional personas (LP / PM / CIO / SWF / family-office / procurement / regulator) and run P4. If the server returns `not_consumer_purchase` skip → surface as a server bug to user.
- [ ] **Caller-side WebSearch ran in the LOCAL language** for every international slice present in the topic (per the multilingual rule in the "YOUR must-check" section above).
- [ ] **P2 phase fired as ONE `audit_skill_submit(phase="ground_truth", ...)` submission, NOT N per-voice splits** — on the thin client `audit_skill_submit(phase=...)` is naturally ONE submission per phase (server handles voice routing internally). Adjudicator verifies `panel_participation` entries for P2 collection are produced by ONE phase call (one phase run_id), not by N independent ground-truth submissions. Issuing separate per-voice submissions defeats consolidated panel results and produces N independent envelopes — one submission per phase, always.
- [ ] **After delivering insights, ASKED the user** (in `scope.language`): "出一份 DOCX 报告吗？" / "Generate a DOCX report?" using `scripts/render_report.py` + `assets/report_style.json`. Skipping this ask = MR run not closed-out (verified fail mode 2026-05-29).

## DOCX report table style (shared convention)

**Style config — `assets/report_style.json` (改造方案 2026-05-27, Owner spec):** the report style (fonts / palette / semantic colors / cell metrics) lives in a **user-editable JSON that ships with this thin client** — every machine defaults to the same format, and the user edits it locally to change the house style WITHOUT touching code. **On this thin client the config is CLIENT-SIDE by design** (the server holds no style — style is presentation, not IP). The renderer reads `report_style.json`; the values below are its shipped **defaults** (keep in sync with the JSON). **Deterministic generator (改造方案 2026-05-27):** `scripts/render_report.py` reads this config + a structured report JSON → a repeatable styled `.docx` for the same resolved font environment (the yoga-socks vAlign/margins fix is baked in). Install once: `pip install -r scripts/requirements.txt` (python-docx). Use: `python scripts/render_report.py <report.json> <out.docx> --lang <bcp47>`. `--lang` AUTO-SELECTS a RESIDENT on-machine font for the run's script and RECORDS it (per-user cache, `scripts/font_resolver.py`), reducing local 方块 (tofu) when the house CJK face is absent or is a macOS on-demand asset (LiSong Pro). DOCX fonts are not embedded: a different reader machine still needs that family or a compatible substitute. Font metrics, pagination, and DOCX bytes may therefore differ across machines; subsequent reports on one machine reuse its recorded choice. The house font stays the first preference for Chinese (kept when resident); a missing one falls through to the next installed candidate (Songti / PingFang / Microsoft YaHei / Noto CJK, region-ordered SC/TC/JA/KO). Omit `--lang` to use the style fonts verbatim. Ships **CLIENT-SIDE** with this thin client (style + renderer both client-side; server holds neither).

When you render a deliverable table into a Word (.docx) report, use this cell style so text is
vertically centered and never presses/overflows the border (the yoga-socks report fix):
- **Every cell `vAlign=center`** — docx-js `verticalAlign: VerticalAlign.CENTER`; raw XML = a `<w:vAlign w:val="center"/>` in every `<w:tcPr>`.
- **In-cell paragraphs: `spacing { after: 0 }`, single line** — no `w:after`, no `w:line` > 240 inside a cell; after-space + an oversized line multiplier push text low and onto the bottom border.
- **Symmetric cell margins ≈ top/bottom 100 twips, left/right 120–160** — docx-js `margins: { top: 100, bottom: 100, left: 140, right: 140 }` — for breathing room.
- Plus the standard docx-skill table rules: DXA widths on cell + columnWidths, `ShadingType.CLEAR`, never `WidthType.PERCENTAGE`; no hard cell borders (clean look).
- **Font**: body Georgia (Latin) + LiSong Pro (CJK) 12pt (`sz 24`); title ~28pt — these are the FIRST-CHOICE faces; with `--lang` the renderer swaps in a resident on-machine face when one is absent (reducing tofu on that machine), recorded after the first report. Fonts are not embedded, so portability still depends on the reader machine.
- **Palette** (warm brown/cream house style; a customer may override): header fill `5E4A22` + white text `FFFFFF`; rows alternate white / cream `FAF6EE`; brown labels/accents `8C6A2E`; secondary gray `595959`/`808080`.
- **Semantic colors** (shared meaning; each skill maps to its own content): success/high `1E7D32` · medium `9A6700` · warning/low `B3261E`.
- **Scope**: cell style + visual identity ONLY — does NOT dictate the document framework; each skill keeps its own sections/content.

## Anti-patterns
- ❌ Skip P0 consent / data-safety before any server call
- ❌ Route `regulated` data to the server (it's client-only — a generic degraded brief)
- ❌ Round-trip the fact pack / analyses through your context — pass run_id + artifact_sha POINTERS
- ❌ Pass frameworks/lenses to P3 — the panel is framework-free by design (server checks diversity POST)
- ❌ Treat `degraded_mode=mock_ground_truth` (deep/quick) as real insight, or chain it downstream as validated
- ❌ Override a server `trust_tier` / mark `exit` yourself — the enforcement is server-side, deterministic

**2026-05-29 Owner hardening (caller-side fail modes verified on a Premium MR run):**

- ❌ **Silently accepting Premium runs missing voices** — on `mode=premium`, if `envelope.trust_signals.sources_failed[]` is non-empty, caller MUST surface a count-free warning that the run is no longer Premium-quality. Do not expose vendor names or a raw voice count; hiding the quality downgrade = misrepresenting Premium output.
- ❌ **Premium run where a reasoner is missing from the P3 panel** — silent tier-downgrade; a smaller panel weakens the cross-vendor agreement signal. Verify the full reasoner panel fired, or the missing voice is surfaced in `sources_failed`.
- ❌ **Caller-side WebSearch all-English on non-English regional sources (applies in BOTH Deep AND Premium)** — 用户中文出题 → caller 全用英文 query 搜海外本地源 (日股/A股/KOSPI/EM) → 召回率严重不足. Per the "YOUR must-check" multilingual rule above, caller MUST translate each international slice into its local language. Same caller code path in Deep and Premium — recall gap is identical; not a Premium-only rule.
- ❌ **Falling back to caller-solo synthesis when P3 panel returned audit-frame verdicts instead of insights** — STOP, surface the panel fail mode to user, re-submit P3 (server-side fix may be needed); do NOT short-circuit P5 by writing the insight doc yourself. Caller-solo synthesis concentrates synthesizer + adjudicator roles (the worst self-serving fail).
- ❌ **Accepting `p4_skipped_reason: not_consumer_purchase` from the server** — non-consumer-purchase topics (capital flow / B2B / supply chain / regulatory / institutional research) MUST be reframed to institutional personas by the server, NOT skipped. Surface as a server bug to user.
- ❌ **Ending an MR run without offering the DOCX report** — caller MUST ask the user (in `scope.language`) "出一份 DOCX 报告吗？" / "Generate a DOCX report?" per the `scripts/render_report.py` + `assets/report_style.json` workflow. Skipping the ask = MR run not closed-out (verified on 2026-05-29; Owner explicitly called it out).
- ❌ **Issuing N separate `audit_skill_submit(phase="ground_truth", ...)` calls (one per voice subset) instead of ONE consolidated submission** — on the thin client `audit_skill_submit(phase=...)` is naturally ONE submission per phase; the server handles voice routing internally. Splitting into N submissions defeats consolidated panel results and produces N independent envelopes. **ONE submission per phase, NOT N.**

## Routing (chain)
```
/audit-explore (formed hypothesis) → /audit-market-research (this skill, commercial validation)
   → HIGH-trust + concrete actions → /audit-writing-plans (impl)
   → MEDIUM-trust + unresolved → /audit-brainstorming (stress-test)
```
Recommend as an ASK (do NOT auto-chain); the panel is one input, not a verdict.

## Pre-flight
- Device token configured
- P0 consent gate is mandatory — never bypass
- deep/quick = MOCK ground truth → surface the `mock_ground_truth` banner (treat as infra, not insight); premium = REAL sources
- `panel_participation` audit_ids enable 7-day server-side re-query
