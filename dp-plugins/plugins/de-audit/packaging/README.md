# Remote package builder

`build_package.py` creates deterministic, platform-independent host packages. It
rejects any bundled runtime, local launcher, activation UI, audit-progress UI,
or credential-bearing MCP manifest.

```text
python3 packaging/build_package.py --output-dir /tmp/de-audit-remote
```

The output contains two archives plus SHA-256 sidecars:

- `de-audit-0.2.13-remote.zip` is the complete repository/host package. Do not
  upload it to Claude Cowork.
- `de-audit-cowork-0.2.13-remote.zip` is the dedicated Claude Cowork desktop
  upload. Its plugin manifest, URL-only MCP manifest, and skills are at ZIP root.

Signing and public marketplace submission remain release gates; native platform
builds are not part of the remote plugin architecture.

The complete archive includes the normal Claude Code entry and a nested
`cowork/` source entry. The dedicated Cowork archive contains only that source
entry flattened to ZIP root. Both are runtime-free. They differ in OAuth
discovery because Claude Code and Cowork currently use separate plugin/session
credential domains.

The archive version is read from `plugin.json`. CI and release automation must
discover the generated archive instead of copying this example filename into a
second version source.

`build_harness_package.py` creates the separate npm-compatible Harness tarball
after `deepseek-harness/dist/` has been built:

```text
cd deepseek-harness
pnpm install --ignore-scripts
pnpm test
cd ..
python3 packaging/build_harness_package.py --output-dir /tmp/de-audit-harness
```

The result is `de-audit-harness-0.2.13-remote.11.tgz` plus its SHA-256 sidecar.
It has the npm `package/` root and includes only package metadata, Cordis patch,
compiled JS/type declarations, README, and Harness skills. It deliberately does
not share the broad ZIP artifact pattern, and contains no `node_modules`,
executable runtime, native binary, or credential.
