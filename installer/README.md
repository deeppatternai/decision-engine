# Decision Engine — client shell (staging draft)

> **Status:** private-staging draft inside the server repo. This directory is
> the candidate content for the future public, MIT-licensed
> `decision-engine` shell repo. It is lifted into a clean-room public repo at
> release step P7 — never by copying git history (release design §11 P7 / §14).

This is a **thin shell**. It contains an installer, a transport-only MCP shim,
and these docs — and **no** product intelligence. Every hosted audit / market-research
/ forecast / plan / image the Decision Engine produces is computed on the
hosted server; the shell only authenticates your device and forwards requests.

What ships here, and what does not:

| ships in this shell | lives only on the server (never here) |
|---|---|
| installer, MCP transport shim, docs | prompts, voice roster, orchestration |
| your local device config | layout definitions, research/forecast pipelines |
| — | ad logic, GUI source, server address, secrets |

## Layout

```
installer/
  install.py                     # body/config installer: de | all; AQG is handled by install.sh
  bootstrap_managed_install.py   # clone stable + verify signature + prepare managed install, or repair wiring
  managed_install.py             # fail-closed identity contract (.managed-install.json marker)
  managed_activation.py          # publish signed checkout state and launcher readiness, not device activation
  launcher.py                    # MCP entry point: bounded managed-update gate, then the shim
  mcp_config.py                  # print/--write the Agent MCP registration
  doctor.py                      # one-shot diagnostic (python/skills/mcp/dev-mode/update/...)
  updater.py                     # read-only managed-update inspection
  update_transaction.py          # crash-safe transactional update apply
  update_coordination.py         # locking/session coordination for the above
  release_acquisition.py         # signed manifest/signature discovery + trust store
  release_contract.py            # release manifest/signature parsing + RSA verification
  shim.py                        # MCP-over-HTTP forwarding shim (transport only)
  config.py                      # small stdlib filesystem/config helpers
  config.example.json            # device config template (no real values)
  leak_scan.py                   # red-line scanner (release design §14)
  LEAK_SCAN.md                   # leak-scan method + latest result
  tests/                         # stdlib unittest suite
```

## Install

**Normal path**: `./install.sh de` (see the repo root README) — this calls
`bootstrap_managed_install`, which clones `stable` straight into the fixed
canonical root (`~/.deeppattern/decision-engine`), verifies its signature,
lands the body via `installer.install` underneath (see below), and prepares the
managed identity, protected release state, and launcher protocol marker.
The outer `install.sh` then registers the launcher with detected Agent hosts,
even when device activation is pending; those MCP entries contain no endpoint
or credentials. Device activation is a separate `installer.permanent_setup`
step. Wherever you ran `install.sh` *from* is irrelevant to where the product
ends up — see the root README's recommended install section.

```bash
# Fresh install / resume an interrupted one, or repair MCP wiring:
python3 -m installer.bootstrap_managed_install install --server-endpoint https://<owner-issued-endpoint>
python3 -m installer.bootstrap_managed_install repair-wiring   # rediscover python, rewrite MCP entries
```

**Developer mode**: pass `--dev-root <path>` to `installer.mcp_config` (or run
`install.sh` with `DE_DEV_MODE=1`) to wire the Agent straight at a source
checkout instead — no signature verification, no auto-update at all
(`installer.launcher`'s own explicit developer-mode semantics).

**Lower-level primitive**: `installer.install` itself only lays a body down
from an already-unpacked bundle root and writes the device config — it does no
Git, no signature verification, and is what `bootstrap_managed_install` calls
once the checkout is in place. It lays the real bodies down side-by-side under
`~/.deeppattern/` and routes their skills into every detected registered host
that declares a managed Skills directory in the table below. Here, `de` installs
**Decision Engine only**; `all` also includes EAF if its body is present.
The outer `install.sh de` handles AQG separately by default, unless
`WITH_AQG=0` is set. AQG needs no DE account; DE's hosted features require
an owner-issued device activation key.

| Host | Installer ID | User MCP config | Managed Skills | Launch/render contract |
|---|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude.json` | `~/.claude/skills/` | direct Python; JSON includes `cwd`; includes `type` |
| Claude Desktop | `claude-desktop` | Windows: `%APPDATA%/Claude/claude_desktop_config.json`<br>macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`<br>Linux: `~/.config/Claude/claude_desktop_config.json` | none | direct Python; JSON includes `cwd`; includes `type` |
| Claude third-party provider profile | `claude-desktop-3p` | `~/Library/Application Support/Claude-3p/claude_desktop_config.json` | none | direct Python; JSON includes `cwd`; includes `type` |
| Tencent CodeBuddy Agent CLI | `codebuddy` | `~/.codebuddy/mcp.json` | `~/.codebuddy/skills/` | direct Python; JSON omits `cwd`; omits `type`; CodeBuddy Studio is not installation evidence |
| Codex | `codex` | `~/.codex/config.toml` | `~/.codex/skills/` | direct Python; TOML launcher |
| Cursor | `cursor` | `~/.cursor/mcp.json` | `~/.cursor/skills/` | direct Python; JSON omits `cwd`; omits `type` |
| Alibaba Qoder Desktop | `qoder` | `~/.qoder/mcp.json` (Windows); `~/.qoder/settings.json` (macOS) | `~/.qoder/skills/` | absolute `installer/mcp_bootstrap.py`; JSON omits `cwd`; omits `type` |
| Alibaba Qoder CN Desktop | `qoder-cn` | `~/.qoder-cn/settings.json` | `~/.qoder-cn/skills/` | absolute `installer/mcp_bootstrap.py`; JSON omits `cwd`; omits `type` |
| Alibaba Qoder IDE | `qoder-ide` | `~/.qoder/mcp.json` | `~/.qoder/skills/` | absolute `installer/mcp_bootstrap.py`; JSON omits `cwd`; omits `type` |
| Alibaba Qoder CN IDE | `qoder-cn-ide` | `~/.qoder-cn/mcp.json` | `~/.qoder-cn/skills/` | absolute `installer/mcp_bootstrap.py`; JSON omits `cwd`; omits `type` |
| TRAE Desktop | `trae` | `~/Library/Application Support/Trae/User/mcp.json` | `~/.trae/skills/` | desktop Python (macOS direct); JSON includes `cwd`, omits `type` |
| TRAE Work | `trae-work` | Windows: `%APPDATA%/TRAE SOLO/User/mcp.json`<br>macOS: `~/Library/Application Support/TRAE SOLO/User/mcp.json` | `~/.trae/skills/` | desktop Python (Windows space-safe, macOS direct); JSON includes `cwd`, omits `type` |
| TRAE CN Desktop | `trae-cn` | Windows: `%APPDATA%/Trae CN/User/mcp.json`<br>macOS: `~/Library/Application Support/Trae CN/User/mcp.json` | `~/.trae-cn/skills/` | desktop Python (Windows space-safe, macOS direct); JSON includes `cwd`, omits `type` |
| TRAE Work CN | `trae-work-cn` | Windows: `%APPDATA%/TRAE SOLO CN/User/mcp.json`<br>macOS: `~/Library/Application Support/TRAE SOLO CN/User/mcp.json` | `~/.trae-cn/skills/` | desktop Python (Windows space-safe, macOS direct); JSON includes `cwd`, omits `type` |
| Tencent WorkBuddy Desktop | `workbuddy` | `~/.workbuddy/mcp.json` | `~/.workbuddy/skills/` | desktop Python (Windows space-safe, macOS direct); JSON omits `cwd`; omits `type` |
| Tencent WorkBuddy AI Desktop | `workbuddy-ai` | `~/.workbuddy-ai/mcp.json` | `~/.workbuddy-ai/skills/` | desktop Python (macOS direct); JSON omits `cwd`; omits `type` |

The Qoder and TRAE families are product-specific Desktop contracts. Claude's
third-party provider profile, Qoder CN, and TRAE are currently macOS-only;
Qoder IDE and Qoder CN IDE are not
silently accepted as their Desktop counterparts. Their host
modules own the exact installed-product/version guards and launch shape; the
macOS paths are architecture-neutral for Intel and Apple Silicon.

On macOS, wiring Qoder or Qoder CN also merges the owned
`decision-engine-audit-routing-v1` `UserPromptSubmit` hook into the same
`settings.json` transaction. Existing AQG and user hooks are preserved,
repeated installs remain idempotent, and the DE uninstaller removes only a
hook whose name and script shape prove DE ownership. Windows Qoder has no
equivalent verified hook contract, so its `mcp.json` receives only the MCP
entry.

```bash
# Lay down the Decision Engine body only (pre-provisioned automation):
python3 -m installer.install de --bundle-root ./bundle

# Everything (reserves a slot for the future engine):
python3 -m installer.install all --bundle-root ./bundle
```

This lower-level primitive does not establish the signed managed-install
identity required by `installer.permanent_setup`. Interactive users should use
the normal managed `install.sh` path above, then open permanent setup. The
primitive remains for already-unpacked, pre-provisioned automation; never put
an activation secret in argv.

AQG is not a target here: `install.sh aqg` clones the AQG repo to
`~/.deeppattern/agent-quality-gates` and runs AQG's own `scripts/install.sh`, which lays down its
body and routes its skills. This module never installs it. (It used to list an `aqg` target and
component, which looked for a body at `~/.deeppattern/aqg` — a path nothing creates — so it always
skipped and installed nothing.)

`--bundle-root` points at the unpacked shell bundle, laid out as
`<bundle>/decision-engine/skills/…`. The installer is
idempotent — re-running refreshes bodies and re-routes skills without
duplicating, and it will **not** overwrite a real (non-symlink) skill directory
you already have.

The installer writes your device config to:

```
~/.deeppattern/decision-engine/config.json   # 0600, dir 0700
```

Normal managed onboarding stores a device fingerprint and, after successful
activation, the endpoint plus server-issued device credentials. The legacy
pre-staged path may temporarily contain an API key. **Do not copy this file between machines** —
activate each device separately. Nothing here is a shared secret you should
commit or paste anywhere.

## Activate

For normal managed onboarding, ask the Agent to open permanent setup:

```bash
( cd "$HOME/.deeppattern/decision-engine" && python3 -m installer.permanent_setup )
```

On macOS and Windows, setup prefers a pywebview desktop window, with Tk as a
fallback when pywebview is unavailable before native registration. Windows
WorkBuddy uses the supported Tk form directly. The activation-secret field is
masked. The human enters the endpoint and secret directly; the secret
never enters argv, environment variables, shell history, or persistent config.
On success the endpoint and server-issued device credentials are stored in the
per-user config, MCP wiring is repaired, and Doctor runs. Future Agent and
machine restarts require no repeated input.

Rerunning this command on an already activated device repairs MCP/Doctor only.
It does not rotate or re-bind credentials; a revoked binding or endpoint change
requires a separate owner-guided recovery because re-binding consumes a device
slot. If activation succeeds but MCP or Doctor fails, the issued device
credentials remain saved; fix the reported stage and rerun this command without
entering the owner values again.

Managed bootstrap also supports non-interactive adoption of values that are
already present in its current process environment:

```bash
python3 -m installer.permanent_setup --from-env
```

This reads `DE_ENDPOINT` and `DE_ACTIVATION_SECRET` without copying either into
argv. Missing values are a successful activation skip; rejected values leave
the base installation, MCP wiring, and automatic updates intact. The original
secret is never persisted.

If remote activation may have succeeded but the device credentials could not
be saved, setup creates `.runtime/activation-recovery-required.json` and refuses
future automatic activation. Preserve that marker and use owner-guided recovery;
do not rerun `--from-env`. Doctor reports this state separately from an ordinary
missing or rejected value.

All configured hosts share that one device activation; no host receives a
separate token. Explicit setup uses the static host registry to merge the
host-specific launcher entry and route the supported Skills without replacing
unrelated MCP servers or user Skills. The server-backed popup follow-up chat
(hub-hosted, no local agent) is available on every host that can render a GE
popup, including the registered Qoder and TRAE Desktop families plus WorkBuddy; the older
local-CLI follow-up route is used only when `ge_chat_transport` is explicitly
set to `legacy` in the local device config, and only where the host's local CLI
contract is available. All hosts expose the shared local GE/DB windows
and audit Stop Panel. A normal managed update changes the managed target in
place and does not create or rewrite host MCP registrations.

The legacy pre-staged-config path remains available for automation that has
already written endpoint + API key into the protected config:

```bash
python3 -m installer.activate            # API key -> device token
python3 -m installer.activate --force     # re-bind this device (uses a device slot)
```

`activate` reads `config.json`, POSTs your API key (as the server's
`activation_secret`) plus this device's fingerprint to
`POST /v1/devices/activate`, and writes the returned `access_token` +
`device_id` back into the **same** `config.json` (0600). It refuses to send the
API key over a plaintext `http://` endpoint (localhost excepted for dev) —
including across an https→http redirect — and, on an invalid/expired/at-capacity
key, prints the server's reason and exits nonzero **without** writing a token.
Here, the legacy name `API key` means the DE **device activation secret**, not a
model provider's API key. Once device credentials are issued, the activation
secret is removed from the config. For interactive activation, use the masked
permanent-setup window described above; do not pass the secret through `--api-key`
or paste it into a shell command.

If a token already exists, `activate` is a no-op unless `--force` is used. That
legacy operation creates a new device binding and requires the activation secret
again. Use the service administrator's recovery procedure for a rebind; reopening
permanent setup does not force a rebind of an already-activated device. The
installer preserves the token on later reinstalls.

> Before device activation, the shim can start in DE Lite mode. An explicitly
> requested `/audit` can use the current agent session for advisory review;
> it does not call DE's cross-vendor panel or imply offline model inference.
> `/layer-check` is also local. Hosted reviews, research, forecasts, graphics,
> boards, and popup follow-up still require activation and service access.

## Use — wire the launcher into your agent

The launcher performs the bounded managed-update gate and then starts the local
shim that forwards to the hosted Decision Engine. Add
it to your agent's MCP config so tools appear in-session. Print a **resolved**
entry whose command, arguments, environment, optional `cwd`, and optional
`type` are selected by the registered host's launch/render contracts in the
table above. Direct-Python hosts use `-m installer.launcher`; the TRAE and
WorkBuddy adapters use desktop Python (Windows space-safe, macOS direct); Qoder uses the
checkout-bound absolute `installer/mcp_bootstrap.py` entry.
This requires either a prepared managed install with a valid launcher protocol
marker or an explicit developer root. The marker confirms that the managed
launcher is ready; it is separate from server-issued device credentials.
MCP registration does not require a device token and does not silently fall
back to wherever this source checkout happens to sit:

```bash
python3 -m installer.mcp_config                       # prepared managed root; --name to rename the server
python3 -m installer.mcp_config --dev-root             # this checkout, explicitly, in developer mode
python3 -m installer.mcp_config --dev-root /some/path  # a different developer checkout
```

A bare call with no prepared managed install and no `--dev-root` prints an
error naming both remedies rather than guessing.

For example, Claude Code emits the following shape (paths resolved for your
machine). Do not copy this shape into Qoder or another host; let
`installer.mcp_config` render the selected contract:

```jsonc
// Claude Code MCP server entry
{
  "mcpServers": {
    "decision-engine": {
      "command": "/abs/path/to/python3",
      "args": ["-m", "installer.launcher"],
      "cwd": "/home/user/.deeppattern/decision-engine"
    }
  }
}
```

The launcher is fail-closed: an old copied install with no managed control plane
continues to serve without network or Git mutation; a managed install checks for
an authorized release within one shared acquisition budget. If recovery or an
update changes HEAD, a fresh child Python interpreter inherits the same MCP stdio
connection and imports the selected checkout, so no second Agent restart is
needed and old/new modules are never mixed. Once a filesystem mutation begins it
must finish or roll back rather than being killed at the acquisition deadline.
For hosted requests, the shim reads
`~/.deeppattern/decision-engine/config.json`, attaches the device token, and
communicates with the server's `/mcp` endpoint over HTTPS. It **refuses** to send
the token to a plaintext `http://` endpoint (localhost excepted for dev).
The server supplies the hosted tool catalog for the device's tier. The shim also
defines local tools and handles display requests, so not every JSON-RPC message
is forwarded unchanged to the server.

### Managed-update rollout gate

`python3 -m installer.managed_activation` is only for a checkout already
prepared at an officially signed stable tag with the exact GitHub/Gitee remotes.
It writes protected release state and publishes the launcher protocol marker.
It does not activate a device or write Agent MCP entries. The outer installer
registers the prepared launcher; permanent setup activates the device and
repairs host configuration.
It deliberately refuses when the production
public key or formal stable/mirror release is absent. It does not guess-reset an
arbitrary legacy directory; the signed legacy bootstrap remains a release
prerequisite.

In practice you don't call this directly — `installer.bootstrap_managed_install`
is the thing that first produces a "prepared" checkout (clone + verify) and then
calls this to finish the job. It's documented here because it's the actual
enforcement point: the checks above are what make a `bootstrap_managed_install`
run refuse, not that module's own logic.

## Verify the shell is clean

The shell must never carry server addresses, secrets, device tokens, prompts,
layouts, orchestration, or GUI/ad source (release design §14). Prove it:

```bash
python3 -m installer.leak_scan          # scans this dir; exit 0 = clean
python3 -m unittest installer.tests.test_leak_scan
```

See [LEAK_SCAN.md](LEAK_SCAN.md) for what the scanner covers and its limits.

## Run the tests

Run these unittest checks in the Python environment used for installation and MCP,
with the dependencies declared in the root `pyproject.toml` installed:

```bash
python3 -m unittest installer.tests.test_install \
                    installer.tests.test_shim \
                    installer.tests.test_activate \
                    installer.tests.test_mcp_config \
                    installer.tests.test_bootstrap_managed_install \
                    installer.tests.test_leak_scan
```

`test_bootstrap_managed_install` is the one end-to-end (real Git, real RSA
signature verification against a local fixture, no network) test in the suite —
everything else mocks the Git/crypto boundary.

(The api-key→activate→audit-tool-visible end-to-end smoke against a real server
lives in the server repo at `tests/test_de_client_smoke.py`; it imports the
server and so stays out of this shell, which must never depend on it.)

## Honest limits

Device binding is software-level (no hardware root of trust); copying the
config or resetting a VM can defeat it. That residual is accepted upstream and
bounded by max-devices + revoke + per-account daily caps (release design §9).
This shell does not attempt cryptographic binding or anti-tamper — those are
post-internal-test concerns tied to the ad subsystem.
