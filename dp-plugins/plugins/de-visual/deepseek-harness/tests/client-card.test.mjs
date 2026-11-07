import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import test from 'node:test'

async function loadClient() {
  const source = await readFile(new URL('../dist/client.js', import.meta.url), 'utf8')
  let registration
  const window = { __ModuleLoader__: { load(value) { registration = value } } }
  runInNewContext(source, { URL, window })
  assert.equal(registration.id, '@deeppattern/de-visual-harness')
  const react = {
    createElement(type, props, ...children) { return { type, props: { ...props, children } } },
  }
  return registration.factory((name) => {
    if (name === 'react') return react
    throw new Error(`unexpected client dependency: ${name}`)
  })
}

test('client bundle depends only on baseline React and no Harness-private module', async () => {
  const source = await readFile(new URL('../dist/client.js', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /require\(["']@deepseek-ai\//)
  await loadClient()
})

test('registers replay-safe cards for the exact five visual wire tools', async () => {
  const client = await loadClient()
  const keys = []
  client.apply({
    slots: {
      inject(name, callback) {
        assert.equal(name, 'tool.call.toolview')
        callback()
      },
      register(options, component) {
        keys.push(options.key)
        assert.equal(options.name, 'tool.call.toolview')
        assert.equal(typeof component, 'function')
        return () => undefined
      },
    },
  })
  assert.deepEqual(keys, [
    'mcp__visual__visual_submit',
    'mcp__visual__visual_status',
    'mcp__visual__visual_result',
    'mcp__visual__visual_cancel',
    'mcp__visual__visual_open_view',
  ])
  assert.deepEqual(Array.from(client.visualToolViewNames), keys)
})

test('card model covers running, completion, cancellation, and artifact availability', async () => {
  const { visualCardModel } = await loadClient()
  assert.equal(visualCardModel('mcp__visual__visual_submit', 'preparing', {}).state, 'preparing')
  assert.equal(visualCardModel('mcp__visual__visual_status', 'start', {}).state, 'running')

  const running = visualCardModel('mcp__visual__visual_status', 'result', {
    isError: false,
    meta: {
      schema_version: 'deep-pattern-visual-card.v1',
      task_id: 'vis_123',
      title: 'Quarterly flow',
      mode: 'diagram',
      status: 'running',
      terminal: false,
      progress_url: 'https://endpoint.deeppattern.ai/plugin/visual/view/exchange#t=opaque',
    },
  })
  assert.deepEqual(JSON.parse(JSON.stringify(running)), {
    title: 'Quarterly flow',
    status: 'running',
    state: 'running',
    taskId: 'vis_123',
    mode: 'diagram',
    progressUrl: 'https://endpoint.deeppattern.ai/plugin/visual/view/exchange#t=opaque',
  })

  const completed = visualCardModel('mcp__visual__visual_result', 'result', {
    isError: false,
    meta: {
      schema_version: 'deep-pattern-visual-card.v1',
      status: 'completed',
      terminal: true,
      artifact_available: true,
      summary: 'Visual is ready.',
    },
  })
  assert.equal(completed.state, 'done')
  assert.equal(completed.artifactAvailable, true)
  assert.equal(completed.summary, 'Visual is ready.')

  const cancelled = visualCardModel('mcp__visual__visual_cancel', 'result', {
    isError: false,
    meta: { schema_version: 'deep-pattern-visual-card.v1', status: 'cancelled', terminal: true },
  })
  assert.equal(cancelled.state, 'warning')

  const failed = visualCardModel('mcp__visual__visual_result', 'result', { isError: true })
  assert.equal(failed.state, 'error')
})

test('old, malformed, or spoofed metadata degrades to a bounded generic card', async () => {
  const { visualCardModel } = await loadClient()
  const old = visualCardModel('mcp__visual__visual_status', 'result', {
    isError: false,
    meta: { status: 'running', progress_url: 'javascript:alert(1)' },
  })
  assert.equal(old.progressUrl, undefined)
  assert.equal(old.status, 'Response available')

  const spoofed = visualCardModel('mcp__visual__visual_status', 'result', {
    isError: false,
    meta: {
      schema_version: 'deep-pattern-visual-card.v1',
      task_id: 'aud_123',
      status: 'running',
      progress_url: 'https://attacker.invalid/plugin/visual/view/exchange#t=token',
    },
  })
  assert.equal(spoofed.taskId, undefined)
  assert.equal(spoofed.progressUrl, undefined)
})
