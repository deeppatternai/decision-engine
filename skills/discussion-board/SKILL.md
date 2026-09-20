---
name: discussion-board
description: "Open an interactive discussion board when the user wants to edit an existing plan, item set, diagram, image, or document by hand and submit the result. Use for board-based rearrangement, prioritization, or annotation in any language. Use the Decision Engine board popup only; do not substitute an inline board, static visual explanation, or text-only revision. A bare mention of a board without interactive intent is not a trigger."
---

# /discussion-board — Hand-adjust a decision on an interactive board (server-rendered)

> **Thin client routing skill** for the Decision Engine hub server.
> This file is **routing-only**: it says WHEN to trigger, HOW to open the board, and HOW to
> read the adjusted board back. It holds **NO rendering IP** — the board template, its layout
> engine, and the injection seam all live ONLY on the server (`POST /db/render`, served from a
> stripped template) and never reach the client. You call `open_db_board`; the **shim** fetches
> the finished board HTML with the device token (which never enters your context) and opens a
> detached popup — the **board HTML never reaches you**. You only ever get back a `popup_id`, and
> later the user's edited board (user content) via `db_board_result`. Internal-test scope: four
> stages (kanban / image / document / diagram); nothing else.

## Mandatory capability-state gate

For every discussion-board request, run this gate before continuing. Before inspecting the current
task's MCP/tool list, checking whether `open_db_board` exists, assembling the board spec, or calling
`open_db_board`, run the local read-only probe:

On macOS/Linux (bash):

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/audit/scripts/de_lite_capability_status.py"
```

On Windows (PowerShell):

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$probe = Join-Path $codexHome "skills/audit/scripts/de_lite_capability_status.py"
$managedPython = Join-Path $HOME ".deeppattern/de-python/Scripts/python.exe"
if (Test-Path -LiteralPath $managedPython -PathType Leaf) { & $managedPython $probe }
elseif (Get-Command py -ErrorAction SilentlyContinue) { py -3 $probe }
else { python $probe }
```

The probe returns only `status=unactivated`, `status=activated`, or `status=unknown`; it does not
contact the Hub or print endpoint/token data. `unactivated` is authoritative for this local
capability boundary and takes priority over a missing MCP tool, `mcp_unavailable`,
`service_unavailable`, and entitlement wording. `unknown` must never be rewritten as unactivated.
If the probe cannot run, exits non-zero, or returns unparseable output, treat the result as
`status=unknown`; continue to the normal capability checks and never claim the device is unactivated.
This probe is not an audit and must not open a Stopper.

If the result is `status=unactivated`, do not inspect the tool list, do not call `open_db_board`,
do not assemble a board spec, and do not continue to any other pre-flight or fallback. Select the
fixed template for the current conversation language, output only the matching template and stop.
Do not mix the two languages. Only `status=activated` or `status=unknown` may continue below.

## When to use vs NOT

| Use this skill | Use instead |
|---|---|
| "开个讨论板调一下 / put this on a board I can edit" — hand-adjust an existing decision, reorder / reprioritize / edit cards, then read the revision back | Generate a picture to LOOK at → `/graphic-explanation` |
| Turn a plan / audit findings / a set of options into cards the user drags + reprioritizes by hand | Review an existing artifact for defects → `/audit` |
| Let the user annotate an image or a document page and send the markup back | Generate commercial insight → `/audit-market-research` |

Use the Decision Engine board popup only. Do not substitute an inline board, static diagram,
or text-only revision for the interactive hand-adjustment workflow.

**Dedup**: don't re-open the same unchanged items turn after turn. When you do need
the board again, call `open_db_board` again with the same spec — a fresh server render is
the ONLY supported path; never cache or re-open the prior board HTML locally or in a system
browser (you never hold the HTML anyway — the shim does).

## Workflow (client assembles the spec; SERVER renders; user hand-adjusts)

1. **Pick the stage** — the board carries ONE of four internal-test stages:
   - `kanban` — draggable cards in columns (the default: reorder / reprioritize / edit
     a decision or plan). Spec: `columns`.
   - `image` — one inert `data:image/…` picture as the annotatable stage (pen / shape /
     text drawn ON it, marked-up image comes back). Spec: `image`.
   - `document` — a stack of page images (each a `data:` URI) to mark up. Spec: `pages`.
   - `diagram` — an interactive diagram the user adjusts and Submits. Spec: `diagram`.
     Read [diagram-spec.md](references/diagram-spec.md) only when assembling a `diagram`
     board; it covers every supported family and field. The server computes all geometry.
   - **Out of scope for the current release**: `embed`, `liveUrl`. The server **fails
     cleanly** on these (a `400` with a stable error code) — surface that the stage is
     unsupported and stop; do **NOT** fall back.
2. **Assemble the board `spec`** from the current decision — this is **user content
   only**, not IP:
   ```json
   {"title": "Q3 计划评审",
    "stage": "kanban",
    "columns": [
      {"id": "todo", "title": "待办", "cards": [
        {"id": "c1", "text": "打通支付回调", "priority": "high"},
        {"id": "c2", "text": "灰度 5%", "priority": "mid"}]},
      {"id": "done", "title": "已完成", "cards": [
        {"id": "c3", "text": "调研竞品", "priority": "low"}]}],
    "notes": "", "comments": []}
   ```
   `priority` ∈ `high | mid | low`. Each card / column needs a unique `id` (any string) —
   order + ids are how the round-trip diff is read. `notes` (an overall 备注) and
   `comments` (anchored annotations) are shared across every stage; leave them empty to
   start. For `image`, drop the `columns` and pass `"image": "data:image/png;base64,…"`;
   for `document`, pass `"pages": ["data:…", …]`. Do NOT invent rendering instructions —
   the server owns how the spec becomes a board.

   For a **`diagram`** board, read [diagram-spec.md](references/diagram-spec.md) only when
   `stage=diagram`. If the reference cannot be read, stop and report that the diagram stage
   cannot be assembled; do not guess a spec. Drop `columns` and pass a `diagram` object
   containing user content. Do NOT pass geometry; the server computes it.
3. **Open** the board (`open_db_board`) — the shim fetches the finished board and pops it
   detached, returning a `popup_id` immediately. The user drags / reprioritizes / edits /
   annotates on their own time, then hits Submit.
4. **Poll `db_board_result(popup_id)`** (bounded ≤ ~50s each, the `wait_s` default; the server
   caps it < 60) for the user's revision and apply it (update the plan / re-audit / rewrite the
   decision).

### Calling pattern
Two shim tools: `open_db_board` pops the board (quick-return with a `popup_id`); the bounded
`db_board_result` poll reads the user's edited board when they Submit. The shim holds the
device token and does the server render — you never fetch, never see the board HTML.
```
# 1. Open — the shim POSTs /db/render and pops the popup detached; returns at once.
mcp__decision-engine__open_db_board(
    spec={...},              # user content only (see step 2); NOT IP
    title="Q3 计划评审",      # optional; shown on the popup titlebar
)  # → {status:"open", popup_id}  |  {status:"failed", reason:"render-fetch-failed" | ...}

# 2. Poll for the outcome — bounded (≤ ~50s per call); re-call while it says "open".
mcp__decision-engine__db_board_result(
    popup_id="<popup_id>",
    wait_s=50,               # optional; capped < 60
)  # → {status:"open"}  |  {status:"done", board:{...}}  |  {status:"dismissed"}  |  {status:"unknown"}
```
Poll up to ~4 rounds. If it is still `open` after that, **surface the `popup_id` to the user**
("the board's still open — Submit when ready, then ask me to check") and stop; the result stays
retrievable by `popup_id` until it expires, so a later `db_board_result` re-reads it.

### Read the adjusted board (via db_board_result — never a browser, never the HTML)
- `{status:"done", board}` → `board` carries the user's adjusted board (the revised `columns`
  / `notes` / anchored `comments`, and the marked-up `image` / `pages` when the user
  annotated) — **user content only**, never server HTML. Each anchored comment comes back as
  `{anchor, text}` — the `anchor` ties the note to a card / a region of cards / a free region,
  so ingest comments as targeted feedback ON specific items, not just a flat list. Ingest the
  whole `board` as the user's revised decision + feedback and apply the adjustments.
- `{status:"dismissed"}` → the user closed without Submit — do **not** invent changes; surface
  it and stop.
- `{status:"open"}` → not yet Submitted; poll again (respect the ~4-round cap above).
- `{status:"unknown"}` → a bad / expired `popup_id`; surface it and stop.
- `open_db_board` `{status:"failed", reason}` (e.g. `render-fetch-failed` — unreachable / non-200
  / timeout / an out-of-scope stage rejected `400`) → surface the redacted reason and **stop**;
  never open a system browser, never render the board locally.

## Anti-patterns
- ❌ Put ANY rendering content in this file — board HTML, layout/geometry rules, the
  spec-to-board algorithm, or a provider/model call. That is server IP; this skill
  is pure routing.
- ❌ Render the board client-side, or fall back to a local renderer / a system browser
  when the server rejects an out-of-scope stage or the fetch fails — surface it and stop.
- ❌ Try to fetch or inline the board HTML yourself, or call the client popup launcher
  (`client.popup.*`) directly — display goes through the `open_db_board` shim tool, which
  holds the device token you never see; the board HTML never enters your context.
- ❌ Offer `embed` or `liveUrl` — they are out of internal-test scope and the server
  refuses them; tell the user they are unsupported, do not work around it.
- ❌ Put layout geometry in the `diagram` spec — coordinates, node sizes, edge routing,
  or a tidy/positioning rule. That is server IP; you pass only `layout` + `nodes` + `edges`
  (user content) and the server computes every coordinate.
- ❌ Invent an adjusted board when the user closed without Submit (`dismissed`) — only a
  `db_board_result` `{status:"done"}` carries real changes.

## Boundary declaration
- **Zero rendering IP in this skill.** The board template, its layout engine, and the
  injection seam are server-held (the stripped template behind `POST /db/render`) and
  excluded from the client bundle by construction. This file only: builds a user-content
  spec, calls `open_db_board`, and reads the `db_board_result` back.
- **Fail-closed, never degrade.** On ANY failure at ANY stage — a `render-fetch-failed`, an
  out-of-scope stage rejection, a launch failure, a `dismissed`, or a missing native backend —
  surface it and stop. Never a browser fallback, never a fabricated / cached board, never a
  local render (Owner constraint). This failure rule applies only after the mandatory capability
  gate has allowed the request to continue; `status=unactivated` uses the fixed product template.
- **Internal-test scope**: kanban / image / document / diagram only. `embed` / `liveUrl`
  are out of scope and rejected server-side. For `diagram`, the layout ALGORITHM runs only
  on the server — the client places server-computed coordinates + free-drags; no geometry IP
  is in this skill or the spec you build.

## Pre-flight
- This section is reached only after the mandatory capability-state gate returns `status=activated`
  or `status=unknown`. Hub reachable + device token configured (the shim holds the token and does
  the render).
- `open_db_board` pops the popup and returns a `popup_id` immediately; `db_board_result` is the
  bounded poll for the user's Submit. Act on the returned `status`, never assume success.

## DE Lite unsupported-capability response

DE Lite currently has no local discussion-board renderer. If the device is unactivated, MCP is
unavailable, the Decision Engine service is unavailable, or an allow-listed entitlement boundary
blocks the board request, do not start `audit_skill_submit`, `de_audit`, the local bridge, a local
text audit, or a Stopper audit row. 不得启动 audit_skill_submit，不得启动 DE Lite 本地审核，不得打开
审核 Stopper。 Return the capability response directly and stop. Do not turn a
  failed board request into an audit of the board content. 不得调用 audit_skill_submit。

For an unactivated device in a Chinese conversation, output exactly this line and nothing else:

> 我这边现在没法直接调用讨论板：Decision Engine 尚未激活，DE Lite 不具备讨论板能力。如果您愿意先激活这台设备，我就能继续。

For an unactivated device in an English conversation, output exactly this line and nothing else:

> I can't open a discussion board right now: Decision Engine is not activated, and DE Lite does not support discussion boards. If you activate this device first, I can continue.

Choose the template from the current conversation language; an explicit user language preference
wins. Do not mix the two languages. Do not add a preface, explanation, missing-tool/native-capability
message, internal error, or follow-up after the template.

For `mcp_unavailable`, `service_unavailable`, `subscription_expired`, `credits_exhausted`, and
`rate_limited`, preserve the same sentence shape, state the real reason in plain Chinese, and give
the matching next action (restore MCP, retry after service recovery, renew, add credits, or wait).
Never expose missing tool names, shell commands, stack traces, or `render-fetch-failed`. 401,
TLS/redirect, unknown entitlement, and request-outcome-unknown remain ordinary fail-closed errors,
not Lite capability responses. No audit popup is created for any of these board failures.
