import assert from 'node:assert/strict'
import test from 'node:test'

import { emptyGrantPayload, parseGrantPayload } from '../dist/oauth-store.js'
import { SnapshotOAuthProvider } from '../dist/oauth-provider.js'

test('accepts a versioned grant and returns an independent copy', () => {
  const source = {
    ...emptyGrantPayload(),
    tokens: {
      access_token: 'secret-access',
      refresh_token: 'secret-refresh',
      token_type: 'Bearer',
      expires_in: 300,
      issuer: 'https://endpoint.deeppattern.ai/plugin/oauth',
    },
  }
  const parsed = parseGrantPayload(source)
  source.tokens.access_token = 'changed'
  assert.equal(parsed.tokens.access_token, 'secret-access')
})

test('rejects wrong resources, unexpected top-level keys, and malformed tokens', () => {
  assert.throws(
    () => parseGrantPayload({ version: 1, resourceUrl: 'https://attacker.invalid/mcp' }),
    /unsupported version or resource/,
  )
  assert.throws(
    () => parseGrantPayload({ ...emptyGrantPayload(), access_token: 'leak' }),
    /invalid shape/,
  )
  assert.throws(
    () => parseGrantPayload({ ...emptyGrantPayload(), tokens: { access_token: 'only' } }),
    /tokens are invalid/,
  )
})

test('accepts discovery only from the production HTTPS origin', () => {
  assert.doesNotThrow(() =>
    parseGrantPayload({
      ...emptyGrantPayload(),
      discovery: {
        authorizationServerUrl: 'https://endpoint.deeppattern.ai/plugin/oauth',
      },
    }),
  )
  for (const authorizationServerUrl of [
    'http://endpoint.deeppattern.ai/plugin/oauth',
    'https://attacker.invalid/plugin/oauth',
    'not-a-url',
  ]) {
    assert.throws(
      () =>
        parseGrantPayload({
          ...emptyGrantPayload(),
          discovery: { authorizationServerUrl },
        }),
      /discovery state is invalid/,
    )
  }
})

test('normalizes optional SDK discovery fields before credential persistence', () => {
  const redirectUri = 'http://127.0.0.1:54321/deeppattern/oauth/callback'
  const payload = {
    ...emptyGrantPayload(),
    pending: {
      state: 'x'.repeat(43),
      redirectUri,
      createdAt: new Date().toISOString(),
    },
  }
  const provider = new SnapshotOAuthProvider(payload, redirectUri)
  provider.saveDiscoveryState({
    authorizationServerUrl: 'https://endpoint.deeppattern.ai/plugin/oauth',
    resourceMetadataUrl: undefined,
    resourceMetadata: {
      resource: 'https://endpoint.deeppattern.ai/plugin/mcp/v1/audit',
      authorization_servers: ['https://endpoint.deeppattern.ai/plugin/oauth'],
    },
    authorizationServerMetadata: {
      issuer: 'https://endpoint.deeppattern.ai/plugin/oauth',
      authorization_endpoint: 'https://endpoint.deeppattern.ai/plugin/oauth/authorize',
      token_endpoint: 'https://endpoint.deeppattern.ai/plugin/oauth/token',
    },
  })

  const snapshot = provider.snapshot()
  assert.equal('resourceMetadataUrl' in snapshot.discovery, false)
  assert.doesNotThrow(() => parseGrantPayload(snapshot))
})

test('rejects pending redirects outside an ephemeral IPv4 loopback callback', () => {
  const invalidRedirects = [
    'http://localhost:1234/deeppattern/oauth/callback',
    'http://127.0.0.1:80@attacker.invalid/deeppattern/oauth/callback',
    'http://127.0.0.1:1234/wrong/callback',
  ]
  for (const redirectUri of invalidRedirects) {
    assert.throws(
      () =>
        parseGrantPayload({
          ...emptyGrantPayload(),
          pending: {
            state: 'x'.repeat(32),
            redirectUri,
            createdAt: new Date().toISOString(),
          },
        }),
      /pending state is invalid/,
    )
  }
})
