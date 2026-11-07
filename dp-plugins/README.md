# Deep Pattern Plugins

`dp-plugins` is the plugin marketplace subtree of the Decision Engine
repository. It is a distribution and integration boundary, not part of the
Decision Engine server runtime.

## Boundary

- Plugin sources live below `dp-plugins/plugins/<plugin-directory>/`.
- Plugin code must not import Decision Engine server modules or change the
  original `/mcp` implementation.
- DE server, installer, database, production configuration, and Java
  operations services remain outside this subtree.
- Plugin versions, remote endpoint contracts, marketplace indexes, and release
  notes are maintained independently from the DE server version.
- Plugin contract tests and package builders validate the remote contracts and
  deterministic portable packages independently of DE server workflows.

## Marketplace

The repository-root `.claude-plugin/marketplace.json` is the unified GitHub
entrypoint for Claude Code and Claude Desktop's Code surface. Its `audit` and
`visual` sources point to `dp-plugins/plugins/de-audit` and
`dp-plugins/plugins/de-visual` respectively.

The repository-root `.agents/plugins/marketplace.json` is the Codex GitHub
entrypoint. It selects the Codex-specific packages at
`dp-plugins/plugins/de-audit/codex` and `dp-plugins/plugins/de-visual/codex`,
and declares `ON_INSTALL` authentication so Codex can reuse a valid host OAuth
credential or open the browser flow when no reusable credential exists. The equivalent
subtree entry is `dp-plugins/.agents/plugins/marketplace.json` for callers that
import `dp-plugins` directly.

The Claude-compatible subtree entry is
`dp-plugins/.claude-plugin/marketplace.json` for callers that import
`dp-plugins` directly. These entries resolve to the same plugin implementations;
the repository-root entries do not create duplicate plugins. All catalogs
point to local plugin directories, so adding `de-xxx` or `aqg-xxx` adds a
directory and a manifest rather than another repository.

Claude Cowork currently maintains a separate plugin runtime and OAuth store.
It therefore uses the controlled local entries at
`dp-plugins/plugins/de-audit/cowork` and `dp-plugins/plugins/de-visual/cowork`;
installing the Code marketplace entry does not install or authorize Cowork.
Each runtime-free source package includes its own Cowork entry, which must be
installed and accepted in that Claude surface.

## Remote release contract

The Audit and Visual portable packages contain host manifests, skills, and
their isolated remote MCP resource URLs. OAuth credentials remain in the host
credential store; neither package contains a Python runtime, native executable, activation code, device
token, access token, or refresh token. One platform-independent package serves
macOS, Linux, and Windows hosts that support Streamable HTTP MCP and OAuth.

The current plugins are `plugins/de-audit` and `plugins/de-visual`. Visual uses
the independent `/plugin/mcp/v1/visual` resource for diagrams, infographics,
and comics; it does not inherit Audit scopes or credentials. Both plugins
remain runtime-free and never fall back to the original device-token routes.
A successful CI build is a candidate artifact only; it
does not publish a release, register a public marketplace entry, enable the A
endpoint, or change the original DE installation. Endpoint deployment, host
acceptance, release publication, and public directory registration remain
separate gates.
