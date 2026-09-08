# Codex integration

Run Decision Engine's graphic explanation and discussion board tools from
**Codex**. Popup follow-up uses the hosted API by default and does not launch a
local Codex agent.

## 1. Register the launcher in `~/.codex/config.toml`

Normal installation detects Codex and merges the Decision Engine launcher into
`~/.codex/config.toml`, preserving unrelated MCP entries. Permanent setup can
repair that registration. Manual pasting is not required for the normal flow.

To inspect the generated TOML, run this from the prepared managed checkout,
using the Python environment selected during installation:

```bash
python3 -m installer.mcp_config --codex
```

The command prints a `[mcp_servers.decision-engine]` table with the resolved
interpreter, `-m installer.launcher`, the managed working directory, and
`tool_timeout_sec`. The launcher handles managed startup and then starts the
shim. Use the generated paths rather than a hard-coded interpreter or a direct
`installer.shim` entry. This command only prints; `installer.mcp_config --client codex --write`
is the explicit configuration-writing path.

Generating a managed entry requires a prepared installation with its launcher
protocol marker; an explicit `--dev-root` selects a developer checkout.
Device activation is separate: the MCP entry contains no endpoint, token, or
activation secret. Without activation, DE Lite can provide advisory audit in
the current agent session; hosted tools require permanent device activation.
See [installation and activation](../../installer/README.md).

## 2. Install the Decision Engine skills

The installer routes the shipped skills into `~/.codex/skills`. Their frontmatter descriptions
select the matching workflow, and the selected `SKILL.md` owns its current tool and safety contract.
Decision Engine does not add instructions to project or global `AGENTS.md` files.

## What Codex sees

After activation, the shim exposes the hosted tool catalog available to the
device, along with local tools for native display and result handling. Rendered
content is fetched with the device credentials and shown in a native popup.
The selected skill defines which tool to call and how to read back the result.

## Legacy local follow-up

The local Codex CLI bridge remains a compatibility path. It is used only when
`ge_chat_transport` is explicitly set to `legacy` in the local device config
and the host supports that path. The default hosted follow-up does not require
a local CLI login or a Codex CLI model setting.
