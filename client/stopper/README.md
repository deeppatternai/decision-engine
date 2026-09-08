# Stop panel — how to show time

Rules for anyone editing `panel.py`, or writing the next panel (another platform, a rewrite, a
web view). One invariant, learned the expensive way.

## The rule

**A finished run's duration comes from the hub's own timestamps. Never from this machine's clock.**

```python
# RIGHT — two hub clocks. The answer is the same no matter who is looking, or when.
duration = run["completed_at"] - run["started_at"]

# WRONG — mixes this machine's clock with the hub's, and dates the answer to the moment we looked.
duration = time.time() - run["started_at"]
```

The same rule, stated as a question to ask of any timing code you write here:

> If the panel were opened for the first time tomorrow, would this number still be right?

If the answer is no, the number is wrong today too — you just haven't noticed yet.

## Why — what `now - started_at` actually computes

It answers **"how long ago did it start"**, not **"how long did it take"**. The two agree only while
the panel is watching at the instant the run finishes. That is a coincidence, not a guarantee:

- the panel is **not always running** — it exits when idle, and starts fresh when a run appears;
- a **restart drops any in-memory freeze cache**, so every already-finished run is re-measured on
  first sight;
- the on-disk run registry is **only pruned when the shim next saves or clears a run**, and its
  entries still carry the status seeded at *submit*. A finished run pruned from memory therefore
  gets re-seeded as "running", which restarts its timer and re-freezes it at the current time on the
  next poll — **every linger cycle, without bound**;
- the user's clock is **not the hub's clock**. Any skew lands straight in the number.

It fails silently. Nothing errors; the panel just reports a bigger number, and the bigger it gets the
more it looks like a real performance problem in something else.

## The incident this came from

Testers reported Standard audits taking 12–61 minutes and escalated it as a server performance
problem. The hub had run every one of them in **139–240 seconds**, with all voices in parallel and a
sub-0.1s queue. The panel invented the rest.

Every row on screen had frozen at the **same wall-clock instant** — the moment the screenshot was
taken — each showing `that instant - its own started_at`:

| what the hub actually took | what the panel showed |
|---|---|
| 224s | 12m 26s |
| 210s | 24m 40s |
| 208s | 46m 30s |

The hub had been sending `completed_at` in the `/v1/audits/<id>` payload the whole time. The panel
just never read it.

Cost of the bug: an engineer-day chasing a server that was never slow — voices, reasoning effort,
CPU, memory, queueing, auth cost, payload size — all measured, all fine, because the number that
started the hunt was fiction.

## Prior art: the macOS panel got this right

`desktop/macos/DecisionEngineStopper.swift` renders a finished voice from the hub's `duration_ms`
and only falls back to a local-clock tick while a voice is still running. It has never had this bug.
The Python panel was written as its counterpart but did not carry this part across — that is exactly
how the bug entered. **When porting a panel, port the time source first.**

## Live rows, and being offline

A *running* row has no `completed_at` yet, so it must tick from the local clock — that is fine, it is
an estimate of something still in flight, and it is replaced by the hub's answer the moment the run
finishes.

But a poll failure leaves the previous view in place, so a running row must not keep ticking a
confident timer while the panel is blind — the run may already be finished. Rows go
`Connection interrupted · Last known <t>` after `STALE_AFTER_S` without a successful poll, anchored to the last real
poll rather than to now. **Terminal rows are never stale**: their duration is hub-derived, cannot
change, and needs no network to stay correct. An outage of any length therefore self-heals — on
reconnect the hub's timestamps give the exact answer, with no catch-up logic anywhere.

## Enforcement

`installer/tests/test_stopper_panel.py` locks all of it against the real numbers from the incident:
late observation, restart, ±1h clock skew, disk resurrection, disconnect, reconnect self-heal, and
the fallback for a row the hub never answered for. Each test was mutation-verified — reverting the
behavior turns it red.

If you change how time is displayed and those tests go red, the tests are almost certainly right.
