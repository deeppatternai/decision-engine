import assert from 'node:assert/strict'
import test from 'node:test'

import { SnapshotOAuthProvider } from '../dist/oauth-provider.js'
import { emptyGrantPayload } from '../dist/oauth-store.js'

function provider() {
  const payload = emptyGrantPayload()
  payload.pending = {
    state: 's'.repeat(43),
    redirectUri: 'http://127.0.0.1:34567/deeppattern/oauth/callback',
    createdAt: new Date().toISOString(),
  }
  return new SnapshotOAuthProvider(payload, payload.pending.redirectUri)
}

test('uses the dedicated public client and exact loopback redirect', () => {
  const value = provider()
  assert.equal(value.clientInformation().client_id, 'deep-pattern-visual-harness')
  assert.deepEqual(value.clientMetadata.redirect_uris, [
    'http://127.0.0.1:34567/deeppattern/oauth/callback',
  ])
  assert.equal(value.state(), 's'.repeat(43))
})

test('accepts only the production authorization origin and visual resource', async () => {
  const value = provider()
  value.redirectToAuthorization(new URL('https://endpoint.deeppattern.ai/plugin/oauth/authorize'))
  assert.equal(value.takeAuthorizationUrl().origin, 'https://endpoint.deeppattern.ai')
  assert.throws(
    () => value.redirectToAuthorization(new URL('https://attacker.invalid/authorize')),
    /unexpected authorization URL/,
  )
  await assert.rejects(
    value.validateResourceURL('https://endpoint.deeppattern.ai', 'https://attacker.invalid/mcp'),
    /does not match/,
  )
})
