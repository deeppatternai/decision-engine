import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { runInNewContext } from 'node:vm'
import test from 'node:test'

async function loadClient() {
  const source = await readFile(new URL('../dist/client.js', import.meta.url), 'utf8')
  let registration
  const window = { __ModuleLoader__: { load(value) { registration = value } } }
  runInNewContext(source, { URL, window })
  assert.equal(registration.id, '@deeppattern/de-audit-harness')
  const react = {
    createElement(type, props, ...children) { return { type, props: { ...props, children } } },
    useState(initial) { return [initial, () => undefined] },
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

test('registers replay-safe cards for the exact four audit wire tools', async () => {
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
    'mcp__audit__audit_skill_submit',
    'mcp__audit__audit_skill_status',
    'mcp__audit__audit_skill_result',
    'mcp__audit__audit_skill_events',
  ])
  assert.deepEqual(Array.from(client.auditToolViewNames), keys)
})

test('card model covers preparing, running, reviewer progress, completion, and stopped calls', async () => {
  const { auditCardModel } = await loadClient()
  assert.equal(auditCardModel('mcp__audit__audit_skill_submit', 'preparing', {}).state, 'preparing')
  assert.equal(auditCardModel('mcp__audit__audit_skill_status', 'start', {}).state, 'running')

  const running = auditCardModel('mcp__audit__audit_skill_status', 'result', {
    kind: 'tool-result',
    isError: false,
    meta: {
      schema_version: 'deep-pattern-audit-card.v1',
      task_id: 'aud_live',
      status: 'running',
      tier: 'standard',
      progress_url: 'https://endpoint.deeppattern.ai/plugin/audit/progress/exchange?t=opaque',
      progress: {
        terminal: false,
        phase: 'reviewing',
        elapsed_ms: 42000,
        auditors: [{ label: 'Reviewer 1', model: 'Model A', status: 'running' }],
      },
    },
  })
  assert.equal(running.state, 'running')
  assert.equal(running.taskId, 'aud_live')
  assert.equal(running.auditors[0].model, 'Model A')
  assert.equal(running.elapsedMs, 42000)

  const completed = auditCardModel('mcp__audit__audit_skill_result', 'result', {
    kind: 'tool-result', isError: false,
    meta: { schema_version: 'deep-pattern-audit-card.v1', status: 'completed', summary: 'No blockers.' },
  })
  assert.equal(completed.state, 'done')
  assert.equal(completed.summary, 'No blockers.')

  const stopped = auditCardModel('mcp__audit__audit_skill_status', 'result', {
    kind: 'tool-result', isError: true, error: { code: 'interrupted' },
  })
  assert.equal(stopped.state, 'warning')
  assert.equal(stopped.status, 'Stopped')
})

test('old and malformed metadata degrade to a bounded generic card model', async () => {
  const { auditCardModel } = await loadClient()
  const old = auditCardModel('mcp__audit__audit_skill_status', 'result', {
    kind: 'tool-result', isError: false, meta: { status: 'running', progress_url: 'javascript:alert(1)' },
  })
  assert.equal(old.auditors.length, 0)
  assert.equal(old.progressUrl, undefined)
  assert.equal(old.status, 'Response available')

  const spoofed = auditCardModel('mcp__audit__audit_skill_status', 'result', {
    kind: 'tool-result', isError: false,
    meta: {
      schema_version: 'deep-pattern-audit-card.v1',
      status: 'running',
      progress_url: 'https://attacker.invalid/plugin/audit/progress/exchange?t=token',
    },
  })
  assert.equal(spoofed.progressUrl, undefined)
})
