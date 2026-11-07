import assert from 'node:assert/strict'
import test from 'node:test'

import { exposeProgressLink } from '../dist/progress-link.js'

const PROGRESS_URL =
  'https://endpoint.deeppattern.ai/plugin/visual/view/exchange#t=opaque-ticket'

test('places the trusted progress link first without removing the canonical resource block', () => {
  const original = {
    content: [
      { type: 'text', text: 'Visual submitted.' },
      { type: 'resource_link', name: 'visual-progress', uri: PROGRESS_URL },
    ],
    structuredContent: { task_id: 'vis_123' },
  }
  const projected = exposeProgressLink(original)

  assert.notEqual(projected, original)
  assert.deepEqual(projected.content[0], {
    type: 'text',
    text: `Open visual progress: [Open live visual progress](${PROGRESS_URL})`,
  })
  assert.deepEqual(projected.content.slice(1), original.content)
  assert.deepEqual(projected.structuredContent, original.structuredContent)
  assert.deepEqual(exposeProgressLink(projected), projected)
})

test('does not surface untrusted or malformed progress links', () => {
  for (const uri of [
    'https://attacker.invalid/plugin/visual/view/exchange#t=opaque-ticket',
    'http://endpoint.deeppattern.ai/plugin/visual/view/exchange#t=opaque-ticket',
    'https://endpoint.deeppattern.ai/not-visual/view',
    'not a URL',
  ]) {
    const original = {
      content: [{ type: 'resource_link', name: 'visual-progress', uri }],
    }
    assert.equal(exposeProgressLink(original), original)
  }
})
