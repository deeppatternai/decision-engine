# Ground Truth and Retrieval

Read this file only after P0 authorization and P1 scope approval for a non-regulated run. It owns
the common hosted lifecycle, P2 Ground Truth, tier-specific retrieval, and coverage reporting.

## Bind the Host Tools

The logical operations are `audit_skill_submit`, `audit_skill_status`, `audit_skill_result`,
`audit_skill_events`, and `audit_skill_cancel`. Bind them to the exact callable names exposed by the
host; never invoke explanatory placeholders literally.

Use one submission for P2. The server routes sources internally, so never split P2 into one
submission per source or panel voice.

After submission:

1. Preserve the returned `run_id`.
2. Poll with `audit_skill_status` at a bounded backoff, normally about ten seconds, while remaining
   interruptible. Do not start duplicate runs while status is pending or running.
3. On terminal completion, fetch `audit_skill_result`.
4. On failure or cancellation, surface the reason and stop the phase.
5. If a previously observed run becomes unknown or transport fails, reconcile with
   `audit_skill_events` and the result endpoint. Stop without guessing if state remains uncertain.
6. Use `audit_skill_cancel` only when the user requests cancellation and the host supports it.

## P2 Submission

Submit one request:

```text
SUBMIT_TOOL(
    skill_name="audit-market-research",
    args={
        "phase": "ground_truth",
        "mode": "quick" | "deep" | "premium",
        "title": "<short research title>",
        "research_type": "<approved scope value>",
        "research_method": "<approved scope value>",
        "language": "<BCP-47>",
        "question": "<approved research question>",
        "topic": "<approved topic>",
        "framework_hints": "<approved scope value>",
        "content": "<topic and question as the retrieval query>"
    }
)
```

The scope fields must match P1 verbatim. `content` is used only for P2 retrieval. Later phases pass
server-owned artifacts by pointers, not by copying their content.

## Current Tier Behavior

### Quick

Quick uses mock ground truth and stops after P2 (`mock_ground_truth` (quick only)). The result must carry
`degraded_mode=mock_ground_truth` or the equivalent degraded reason. Display a prominent statement
that every mock finding and URL is infrastructure output, not real insight. Do not chain Quick output
downstream as validated market evidence.

### Deep

Deep uses real server-side retrieval. The server fans the question out over core sub-questions such
as market size, participants and share, growth, and moat, then pools and deduplicates the evidence.
It covers mainstream web and relevant targeted domains through the sources currently available to
the server.

### Premium

Premium uses expanded real retrieval. It adds broader paid, professional, social, financial,
regional, and agentic sources when the server has the required credentials and the topic qualifies.
Premium is broader than Deep; it is not permission to hide partial source failures.

## Supplemental Caller-Side Search

No retrieval system reaches every high-value site. During Deep or Premium analysis, use caller-side
search for relevant sources the server cannot reach, including `xiaohongshu.com`, `quora.com`,
`xueqiu.com`, and `coinglass.com`. If the caller lacks search access, disclose the coverage gap.

For every international slice, issue local-language queries as well as any useful English query.
This local-language queries requirement applies to both Deep and Premium. Use Japanese for Japanese
sources, Chinese for Chinese sources, Korean plus English for Korean sources, and selectively use
French, German, Portuguese, Spanish, or Arabic for the corresponding regional sources.

Keep supplemental caller evidence separate from the server-owned P2 artifact. Cite it normally in
the client presentation and identify it as caller-side search rather than implying that P3 or P5
consumed it.

## P2 Result and Gate

Validate and retain:

- `run_id`;
- `artifact_sha`, the binding digest for the P2 fact pack;
- `scope_sha`;
- retrieval provenance and citations;
- source and query-pass coverage;
- `degraded_mode`, `degraded_reasons`, and `sources_failed` when present.

For a real pack, partial source failures must appear as `ground_truth_partial` or another returned
degraded reason. Never infer that missing signals are clean.

Quick stops here and renders an evidence brief. Deep and Premium may continue to P3 only with the
actual P2 `run_id` and `artifact_sha` returned by the server.

When `panel_participation` contains audit IDs, retain them with the run record. They may be used to
re-query server-side audit results for up to seven days, subject to the server's retention policy.
