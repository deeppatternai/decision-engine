import assert from 'node:assert/strict'
import test from 'node:test'

import { exposeProgressLink } from '../dist/progress-link.js'

const PROGRESS_URL =
  'https://endpoint.deeppattern.ai/plugin/audit/progress/exchange?t=opaque-ticket'

test('places the trusted progress link first without removing the canonical resource block', () => {
  const original = {
    content: [
      { type: 'text', text: 'Audit submitted.' },
      { type: 'resource_link', name: 'audit-progress', uri: PROGRESS_URL },
    ],
    structuredContent: { task_id: 'aud_example' },
  }
  const projected = exposeProgressLink(original)

  assert.notEqual(projected, original)
  assert.deepEqual(projected.content[0], {
    type: 'text',
    text: `Open audit progress: [Open live audit progress](${PROGRESS_URL})`,
  })
  assert.deepEqual(projected.content.slice(1), original.content)
  assert.deepEqual(projected.structuredContent, original.structuredContent)
  assert.deepEqual(exposeProgressLink(projected), projected)
})

test('does not surface untrusted or malformed progress links', () => {
  for (const uri of [
    'https://attacker.invalid/plugin/audit/progress/exchange?t=opaque-ticket',
    'http://endpoint.deeppattern.ai/plugin/audit/progress/exchange?t=opaque-ticket',
    'https://endpoint.deeppattern.ai/not-audit/progress',
    'not a URL',
  ]) {
    const original = {
      content: [{ type: 'resource_link', name: 'audit-progress', uri }],
    }
    assert.equal(exposeProgressLink(original), original)
  }
})
