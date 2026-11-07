---
name: setup
description: Connect Deep Pattern Audit in DeepSeek Harness through browser OAuth.
---

# Set up Deep Pattern Audit for Harness

Call `mcp__audit__connect` with `action: status` first. If it reports
`connected`, do not open another activation page. If it reports disconnected or
failed, call it once with `action: connect`.

The plugin opens the complete HTTPS authorization URL in the system browser and
listens only on `127.0.0.1` for the temporary OAuth callback. Ask the user to
enter the owner-issued activation code on that page. Never ask for the
activation code, access token, or refresh token in chat.

While authorization is active, call `mcp__audit__connect` with `action: status`
and `wait_ms: 20000`. Repeat only while the state remains `authorizing` or
`connecting`. Once it reports `connected`, continue the original request in the
same conversation. Cancellation or timeout applies only to that attempt; a
later explicit request may call `connect` again.

Use `action: logout` only when the user explicitly asks to remove this Harness
profile's Deep Pattern authorization. Logout removes the stored OAuth grant and
remote audit tools. It does not uninstall the plugin or alter Codex, Claude,
Cursor, legacy runtime, or another Harness profile.

The grant is kept by the Harness credentials service, not in the plugin package
or YAML. The default Harness backend is a private per-user credential file; do
not describe it as OS Keychain-backed or inaccessible to every same-user
process. Never fall back to a static bearer header, DCR, or legacy device-token
activation.
