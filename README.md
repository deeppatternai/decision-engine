# Decision Engine

**English** · [简体中文](README.zh-CN.md) · **v0.2.96**

> **A second opinion you can actually trust — because it comes from many
> independent minds, not one model agreeing with itself.**

**Decision Engine (DE)** is a hosted engine for the decisions that are expensive
to get wrong. Put your code, your plan, your idea, your market question, or your
forecast in front of a **cross-vendor panel of frontier models** — genuinely
independent reviewers drawn from different training distributions — and get back a
structured, adjudicated read: what's wrong, what's strong, where the reviewers
agree, and where they split. You call it in plain language from the coding agent
you already use; all of the intelligence runs server-side.

The premise is simple: **one model can't see its own blind spots.** Ask a single
model to check its own work and it leans on the very assumptions it was trained
on. Put the same question to reviewers that *don't* share a training distribution,
and the points where they **independently converge** are real signal — while the
points where they diverge are exactly where you should look harder. Everything in
Decision Engine is built around that difference.

And Decision Engine does more than *review* a decision. It can also **draw** one
for you — turning a tangled idea into a picture you grasp at a glance — and hand
you a **whiteboard** where you and the AI work a plan out by hand.

## Why Decision Engine — the design philosophy

**Independent minds, not one mind louder.** The value isn't more model calls —
it's *uncorrelated* ones. A panel that spans different vendors and training
distributions catches what any single lineage systematically misses. Agreement
across genuinely independent reviewers earns trust in a way one confident answer
never can; the method is the moat, not any one model's name.

**You are the adjudicator, not the audience.** DE surfaces opinions; it never
overrules your judgment. Findings come back as structured input for *you* — or
your agent — to accept, reject, or escalate, not as a verdict handed down. The
panel advises; you decide. A review is the start of a decision, not the end of
one.

**Honest about uncertainty.** DE tells you when the panel *genuinely* agrees
versus when it merely anchored on a shared phrase, and it labels that convergence
quality instead of faking confidence. Forecasts pool what real forecasters and
markets already predict — the engine never invents a number of its own. A trust
signal you can't calibrate is worse than no signal at all.

**Discipline, not reaction.** The most valuable review happens at the chokepoint —
before you commit, before you ship — where a second opinion can still change the
outcome, not as a post-mortem after it's live. DE is built to sit at that moment.

Decision Engine runs standalone, and it's also the hosted engine that
**completes [Agent Quality Gates (AQG)](https://github.com/deeppatternai/agent-quality-gates)** —
the local, no-account engineering-discipline toolkit. Install either one and bring
the other along in the same step.

> **Status:** public source preview. Access to the hosted Decision Engine service
> remains invite-only and requires an owner-issued endpoint + activation secret.
> AQG is local and needs no account.

## Not just reviewed — *seen*, and *shaped*

Most AI answers in one shape: more text. But the hard part of a decision is often
not getting an *opinion* — it's *understanding* the thing under the jargon, and
*saying* what you actually mean. Decision Engine gives you a tool for each, and
neither is a chat window.

### 🎨 See the idea — don't decode it — `/graphic-explanation`

Ask the engine to explain a concept, a piece of logic, or the conversation you're
already in, and instead of another wall of prose it hands you a **picture**: a
**comic** that tells the story, an **infographic** that lays the structure out, or
a crisp **diagram** of the exact flow, sequence, state machine, or architecture.
Understanding stops being a decode-the-jargon chore and becomes something you
simply *see*.

The image opens in its own window with a **follow-up chat right beside it** — point
at any part, ask "wait, why this?", and keep pulling the thread until it clicks. No
more guessing your way through someone else's industry shorthand.

### ✍️ Think it through on a whiteboard — *with* the AI — `/discussion-board`

Some things you can't write in a paragraph but understand the moment you can move
the pieces. Open a discussion board and the AI has **already laid your topic out**
for you — as cards to reorder and reprioritize, or an image or a document to mark
up. Then you take over: **drag, rank, scribble, circle, cross out, drop a note in
the margin.**

Hand the marked-up board back and the AI reads your intent from what you *did*, not
from what you managed to type. It's a real working session with the AI on a shared
canvas — the fuzzy idea in your head comes back a **concrete, shared plan.** (Kanban
today; structured diagram boards — flow, mind-map, journey, and more — are landing
next.)

## Bring a half-formed idea — leave with one you can defend

Not every decision starts as a finished thing to review. Sometimes all you have is
a hunch. Two skills take it from there — and both think *with* you, not *at* you.

### 🌱 Shape a vague idea into a testable one — `/audit-explore`

You've got a rough intuition — *"maybe we should…"* — but you can't yet say
what would make it right or wrong. `/audit-explore` develops it into a **formed,
falsifiable hypothesis**: a cross-vendor panel explores the problem space from
genuinely different angles, a premortem asks how it fails *before* you've spent a
thing, and you come out with a claim sharp enough to test — and ready to hand to
any of the skills below.

### 🎯 Pressure-test it with a room of independent minds — `/audit-brainstorming`

Once an idea has a shape, `/audit-brainstorming` puts it in front of a
thinking-partner panel — not to hunt bugs, but to think it through *with* you:
where it's strong, where it's fragile, the counter-arguments you're not making, the
assumptions you didn't notice you'd baked in. It also explains how to test the
idea: what would *falsify* it, how often similar ideas have worked before, and
whether the evidence should raise or lower your confidence.

Together they turn *"I think we should…"* into *"here's our choice, here's why,
and here's what evidence would make us reconsider."*

## Point it at the world outside — the market, and the odds

Some questions aren't about your artifact at all — they're about what's happening
*out there*. Two skills turn the engine outward.

### 📊 A market read that isn't one model's guess — `/audit-market-research`

Ask a market, GTM, or positioning question and `/audit-market-research` builds you
an **insight doc** — but not from a single model's imagination. A cross-vendor panel
analyzes the question from independent angles, **grounded in real retrieval**
across web, social, and financial sources. The resulting analysis can optionally
be reviewed by a **synthetic customer panel**. It also tells you *how much to trust it*: an honest
**convergence-quality** signal that separates "the reviewers genuinely agree" from
"they just latched onto the same phrase."

### 🎲 The odds, pooled — never invented — `/audit-forecast`

For a specific, verifiable, time-bound outcome — a match result, an election, an
earnings beat, a price threshold — `/audit-forecast` reads how the **world already
predicts it**: bookmaker odds, real-money prediction markets, data and model lines,
expert and media picks — pooled into one structured read (direction, probability,
where the consensus is strong, where it splits, the key catalysts, the base rate,
what would flip it). It **never manufactures a number of its own** — every figure
traces back to a real source, enforced server-side. An honest aggregate beats a
confident hallucination.

## What is in this repository, and what runs on the server?

This repository contains **the DE client source and supporting files**. The client
handles installation, connects AI tools, opens local windows, and communicates with
DE. The full implementation of hosted services such as reviews and research is
not included here.

| Where it lives | What it contains | Example in use |
|---|---|---|
| **In this repository: client source and supporting files** | Installer, AI tool integrations, skill instructions, local window and display code, icons, tests, and documentation. | An AI tool submits a review request; your computer shows progress, a diagram, or a discussion board. |
| **On the DE server: hosted implementations, not shipped here** | Dedicated review prompts, model selection and task orchestration, research and forecast workflows, board and diagram content generation, and server-held model-provider credentials. | The server receives a review request, coordinates multiple models, and returns the results. |
| **On your computer: generated during installation or activation, not shipped here** | The DE service address, device identity, and device access credentials issued during activation. | You enter the service address and activation secret; the client saves the configuration this device needs for later connections. |

The MCP shim bridges AI tools and DE. It forwards hosted tool requests and also
handles local tools and display requests. The client includes local windows,
interaction code, and some tool definitions. Hosted board and diagram content is
generated by the server and displayed by the client; this does not mean the client
has no user-interface code.

Distributed source does not include a user's real service address or secrets.
Those values and the issued device credentials are runtime configuration, distinct
from the credentials the server uses to call model providers.

## What you can do with it

Installing DE routes a set of skills into your agent. Invoke one by name (e.g.
`/audit`) or just describe the task — the agent picks the matching skill. Hosted
workflows send requests to DE's service; local options are explained below the table.
A one-line tour:

| skill | what it does |
|---|---|
| `/audit` | External-auditor review of an artifact (code, doc, plan, migration, design). A cross-vendor panel finds defects; your agent adjudicates them into one verdict. |
| `/audit-adjudication` | Fold one or more prior audit results into a single accept / reject decision table. |
| `/audit-brainstorming` | Stress-test an idea, strategy, or proposal with a thinking-partner panel — strengths, risks, counter-arguments, assumptions — instead of defect-finding. |
| `/audit-explore` | Develop a vague, unformed idea into a formed, falsifiable hypothesis, with premortem scaffolding, ready to hand off to the skills below. |
| `/audit-forecast` | Aggregate how existing external forecasters and markets *currently* predict a specific, verifiable, time-bound outcome. It pools existing predictions — it never invents its own. |
| `/audit-market-research` | Generate a market / GTM insight doc: cross-vendor analysis + search-grounded retrieval + an honest convergence-quality signal. |
| `/audit-writing-plans` | Turn upstream audit conclusions into structured engineering / implementation docs, cross-validated by the panel before promotion. |
| `/graphic-explanation` | Visually explain the current conversation or a decision — the engine returns a finished comic, infographic, or SVG diagram, opened in a native popup. |
| `/discussion-board` | Open a decision or set of items as an interactive board popup (draggable cards, priorities, inline edits, notes, freehand annotation) you adjust by hand, then read back. |
| `/layer-check` | Local reasoning-discipline that catches category errors in comparative / competitive analysis (the "different-layer product treated as a substitute" trap). Runs entirely on your machine. |

One install serves every registered host below; there is no per-agent Decision
Engine variant to pick. Before device activation, the installed MCP can start in
**DE Lite** mode: an explicitly requested `/audit` can return advisory review from
the current agent session, without calling DE's cross-vendor panel. This still
uses the host agent's model and does not imply offline inference.
`/layer-check` also works locally without a DE account. Hosted reviews, research,
forecasts, graphics, boards, and popup follow-up require device activation
(below) and the corresponding service access.

### Supported AI tools, grouped by family

All products below support MCP, native windows, popup follow-up, and Stop Panel.
This table summarizes them by family; installation detects the current host and
configures the MCP and Skills it supports.

| Family | Supported products | Differences to note |
|---|---|---|
| Claude | Claude Code, Claude Desktop, Claude third-party provider profile | Claude Code also supports Skills; the third-party profile is an independent macOS MCP location. |
| Codex | Codex | Supports Skills. |
| Cursor | Cursor | Supports Skills. |
| Tencent | CodeBuddy Agent CLI, WorkBuddy Desktop, WorkBuddy AI Desktop | CodeBuddy support is limited to the standalone Agent CLI, not CodeBuddy Studio. |
| Alibaba Qoder | Qoder Desktop, Qoder CN Desktop, Qoder IDE, Qoder CN IDE | Desktop/IDE and standard/CN variants are distinct; IDE variants have separate MCP identities and share Skills and audit hooks with the corresponding Qoder variant. |
| TRAE | TRAE Desktop, TRAE CN Desktop, TRAE Work, TRAE Work CN | Desktop/Work and standard/CN variants are handled separately. |

### Audit depth

**`/audit` offers three review modes:**

- **fast** — a quick check explicitly requested by the user, such as a surface
  review of prose or a typo sweep.
- **standard** (default) — a regular review of substantive issues in code,
  documents, and plans.
- **deep** — an in-depth review for security-sensitive work, complex architecture,
  or irreversible operations.

AQG separately determines whether to initiate an audit. **Trivial, non-sensitive
changes such as renaming, formatting, or comments normally skip auditing.** Use
`fast` when the user explicitly requests a quick check. Even a small change still
requires deeper review under the policy when it affects permissions, secrets,
installation integrity, or another sensitive area.

### Diagrams and discussion boards

**`/graphic-explanation`** turns a concept or plan into a comic, infographic, or SVG
diagram, such as a flowchart, sequence, state machine, or architecture diagram.
The server generates the content, which opens in a local window.

**`/discussion-board`** helps you organize and revise a plan together. Drag cards
and change priorities on a kanban board, or draw, highlight, and add text on an
image or a document's page images. Submit your changes to continue the discussion.

**Document annotation inputs:** original PDF, DOCX, PPTX, and XLSX files must first
be converted to page images for the document annotation interface. This repository's
`installer.office` only detects or helps install LibreOffice; it does not provide
the complete file-conversion and import workflow. This section therefore does not
promise automatic board import when an original PDF or Office file is dropped in.

See the [discussion-board guide](skills/discussion-board/SKILL.md) for structured
diagram boards and layout usage. Availability depends on the server and the
current AI tool.

## Install

There are two supported installation methods. Both install Decision Engine and
Agent Quality Gates, connect detected AI hosts, and run post-install checks.
Choose either method; do not edit MCP configuration by hand.

### Option 1: ask an AI agent to install it (recommended)

Open the complete guide for your preferred language:

- [English AI setup guide](AI_SETUP.md)
- [中文 AI 安装指南](AI_SETUP.zh-CN.md)

Give the entire guide to an AI agent that can operate your local terminal, then
say: "Install Decision Engine according to this guide." The agent checks the
machine, obtains trusted installer source, installs and verifies AQG and Decision
Engine, configures MCP and Skills, and activates the device when the required
owner values are available.

The DE service administrator issues the device activation key. Never paste a
real endpoint, activation key, device token, or Git credential into chat. The AI
setup guide contains the complete credential-handling and failure rules.

### Option 2: run the one-click installer

Download the installer for your platform only from the official trusted source,
then run it in a local terminal. The installer checks required dependencies,
installs AQG and Decision Engine, configures supported hosts, and opens the
masked activation window.

#### macOS

Download [dp-install.sh](dp-install.sh), then run it in an interactive Terminal:

```bash
bash ./dp-install.sh
```

The script does not accept command-line arguments.

#### Native Windows

Download [dp-install.ps1](dp-install.ps1), then run it in 64-bit Windows
PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\dp-install.ps1
```

Both methods are for first-time installation. If the fixed installation directory
already exists or an earlier installation was interrupted, preserve the existing
files and follow the recovery instructions reported by the installer; do not
manually overwrite or delete the installation directory.

## Activate & use

Decision Engine binds per device. Both supported installation methods register
MCP, configure Skills, run Doctor, and handle permanent setup as part of the
installation flow. Do not edit host configuration or run lower-level installer
commands by hand.

On macOS, permanent setup opens a masked pywebview desktop window and uses Tk
only as a fallback when pywebview is unavailable before native registration.
Windows uses the same default, except WorkBuddy opens the supported masked Tk
form directly. Enter the owner endpoint and secret once. The per-user config
stores only the endpoint and server-issued device credentials, so future Agent
or machine restarts need no repeated input. The activation key does not enter
chat, command arguments, environment variables, or persistent config.

Rerunning permanent setup on an already activated device only repairs MCP wiring
and runs Doctor; it does not rotate or re-bind the device and does not consume a
new device slot. Revoked-device or owner-endpoint replacement requires a separate,
explicit owner-guided recovery flow. The existing first-use activation popup is
still retained as a fallback for older/preconfigured installations; Agent-launched
permanent setup is the preferred auto-updating installation path.

If Cursor is installed, permanent setup also merges the launcher into
`~/.cursor/mcp.json` and routes its supported skills. Ordinary agent restarts and
automatic updates do not add a new host relationship or rewrite Cursor's MCP
config; host onboarding remains part of explicit install/setup.

Full details, including the fail-closed cleartext-credential rules, are in
[`installer/README.md`](installer/README.md).

## Check your setup

`de doctor` — a one-shot diagnostic (like AQG's) that tells you, in one place,
whether the client is ready and what to fix if not:

```bash
python3 -m installer.doctor          # human-readable report
python3 -m installer.doctor --json   # machine-readable
```

It checks: Python is 3.12+, the skills are linked into your agent, the native
popup backend (pywebview) can open a window, LibreOffice is available for Office
conversion (a WARN if not — it's optional and installed on demand), and whether
this device is activated. It exits `0` when everything is PASS or WARN, `1` when
something is genuinely broken. It never prints your endpoint or activation
secret — only whether they are set. Both supported installation methods run it
at the end of an install.

## Installed copy and updates

A supported installation places the signed managed copy at the fixed path
`~/.deeppattern/decision-engine`. The MCP entry runs that copy through
`installer.launcher`, which checks and applies signed `stable` updates before
startup under the bounded GitHub→Gitee policy. Users do not need to keep the
originally downloaded one-click installer.

Do not handle legacy installations, failed migrations, or recovery by manually
overwriting the installation directory. Preserve existing files and follow the
recovery instructions reported by Doctor or the installer. `python3 -m
installer.doctor` reports installed, target, running, and last-result state, so
a fetched version is not mistaken for the version actually serving.

## Verify the shell is clean

Source prepared for distribution must not contain real service addresses, secrets,
or device tokens, or the hosted server's dedicated prompts, layout templates,
orchestration, or GUI/ad source. The client's own window and display code belongs
in the distribution; local configuration generated after installation is separate.
These checks detect the leak patterns the scanner recognizes:

```bash
python3 -m installer.leak_scan                       # exit 0 = clean
python3 -m unittest installer.tests.test_leak_scan
```

## Run the tests

Run the unittest suite in the Python environment prepared above, with the declared
package dependencies installed:

```bash
python3 -m unittest \
  installer.tests.test_install \
  installer.tests.test_shim \
  installer.tests.test_activate \
  installer.tests.test_mcp_config \
  installer.tests.test_leak_scan \
  installer.tests.test_stopper_fallback \
  installer.tests.test_window_icon \
  client.popup.tests.test_visual_capture
```

## License

Open source under the [MIT License](LICENSE.md).
