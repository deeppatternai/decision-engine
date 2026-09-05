# Codex integration

Run the Decision Engine display tools (graphic explanation + discussion board) from **Codex**, with
the in-popup follow-up chat on codex-cli (gpt-5.5). Two installed surfaces:

## 1. Register the shim in `~/.codex/config.toml`

Emit the ready-to-paste block and add it to your `~/.codex/config.toml`:

```bash
python3 -m installer.mcp_config --codex
```

It prints a `[mcp_servers.decision-engine]` table — the resolved interpreter, `-m installer.shim`, a
pinned `cwd` at the shell root, and a generous `tool_timeout_sec` (headroom over Codex's 60s per-tool
default so the bounded `db_board_result` poll never trips it). Example:

```toml
[mcp_servers.decision-engine]
command = "/usr/bin/python3"
args = ["-m", "installer.shim"]
cwd = "/path/to/decision-engine"
tool_timeout_sec = 120
```

This is **print-to-paste** — the installer never edits your `config.toml`; you paste the block in
yourself. It bakes in no endpoint / token / secret; those stay in the per-device `config.json` the
shim reads at runtime (run `python3 -m installer.activate` once to activate the device).

## 2. Install the Decision Engine skills

The installer routes the shipped skills into `~/.codex/skills`. Their frontmatter descriptions
select the matching workflow, and the selected `SKILL.md` owns its current tool and safety contract.
Decision Engine does not add instructions to project or global `AGENTS.md` files.

## What Codex sees

The `decision-engine` shim advertises the hub's forwarded tools plus the three local display tools
(`open_ge_popup`, `open_db_board`, `db_board_result`). The rendered bytes are byte-isolated — the shim
fetches them with the device token and shows them in a native popup; they never enter the model
context. The GE popup's follow-up chat runs on codex-cli (caller=codex → gpt-5.5).
