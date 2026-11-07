import assert from 'node:assert/strict'
import test from 'node:test'

import { deferVisualContinuation } from '../dist/visual-continuation.js'

function capture(rawName, structuredContent, { supported = true } = {}) {
  const contexts = []
  const execution = supported ? { deferContext(context) { contexts.push(context) } } : {}
  deferVisualContinuation(rawName, { content: [], structuredContent }, execution)
  return contexts
}

function captureResult(rawName, result) {
  const contexts = []
  deferVisualContinuation(rawName, result, {
    deferContext(context) { contexts.push(context) },
  })
  return contexts
}

test('submit defers a visible progress update before polling the same task', () => {
  const contexts = capture('visual_submit', {
    task_id: 'vis_101', status: 'queued', terminal: false,
  })
  assert.equal(contexts.length, 1)
  assert.deepEqual(contexts[0].source, {
    kind: 'deep-pattern-visual', taskId: 'vis_101', action: 'show-progress-and-poll',
  })
  assert.match(contexts[0].content[0].text, /exact `Open visual progress:` Markdown line/)
  assert.match(contexts[0].content[0].text, /mcp__visual__visual_status/)
  assert.match(contexts[0].content[0].text, /"task_id":"vis_101"/)
  assert.match(contexts[0].content[0].text, /"wait_seconds":20/)
})

test('non-terminal status defers exactly one bounded continuation for the same task', () => {
  const contexts = capture('visual_status', {
    task_id: 'vis_102', status: 'running', terminal: false,
  })
  assert.equal(contexts.length, 1)
  assert.equal(contexts[0].source.action, 'poll')
  assert.match(contexts[0].content[0].text, /mcp__visual__visual_status/)
  assert.match(contexts[0].content[0].text, /"task_id":"vis_102"/)
  assert.doesNotMatch(contexts[0].content[0].text, /visual_result/)
})

test('terminal status defers one result read and does not request another poll', () => {
  for (const status of ['completed', 'failed', 'cancelled']) {
    const contexts = capture('visual_status', {
      task_id: 'vis_103', status, terminal: true,
    })
    assert.equal(contexts.length, 1)
    assert.equal(contexts[0].source.action, 'fetch-result')
    assert.match(contexts[0].content[0].text, /mcp__visual__visual_result/)
    assert.doesNotMatch(contexts[0].content[0].text, /visual_status/)
  }
})

test('other tools, malformed responses, and older hosts do not defer context', () => {
  assert.deepEqual(capture('visual_result', { task_id: 'vis_103', status: 'completed' }), [])
  assert.deepEqual(capture('visual_cancel', { task_id: 'vis_103', status: 'cancelled' }), [])
  assert.deepEqual(capture('visual_open_view', { task_id: 'vis_103', status: 'running' }), [])
  assert.deepEqual(capture('visual_status', { status: 'running' }), [])
  assert.deepEqual(capture('visual_status', { task_id: 'aud_103', status: 'running' }), [])
  assert.deepEqual(
    captureResult('visual_status', {
      isError: true,
      content: [{ type: 'text', text: 'status failed' }],
      structuredContent: { task_id: 'vis_104', status: 'running' },
    }),
    [],
  )
  assert.deepEqual(
    capture('visual_status', { task_id: 'vis_105', status: 'running', terminal: false }, { supported: false }),
    [],
  )
})
