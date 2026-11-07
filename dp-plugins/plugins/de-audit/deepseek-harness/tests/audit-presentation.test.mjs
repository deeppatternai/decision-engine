import assert from 'node:assert/strict'
import test from 'node:test'

import { DeepPatternAuditManager } from '../dist/audit-client.js'
import { projectAuditPresentation } from '../dist/audit-presentation.js'

const PROGRESS_URL = 'https://endpoint.deeppattern.ai/plugin/audit/progress/exchange?t=opaque'

test('projects bounded replay-safe audit progress metadata', () => {
  const projected = projectAuditPresentation('audit_skill_status', {
    content: [{ type: 'resource_link', name: 'progress', uri: PROGRESS_URL }],
    structuredContent: {
      task_id: 'aud_card_1',
      status: 'running',
      progress: {
        schema_version: 'audit-progress.v1',
        tier: 'standard',
        phase: 'reviewing',
        terminal: false,
        elapsed_ms: 12500,
        poll_after_ms: 20000,
        auditors: [
          { label: 'Reviewer 1', model: 'Model A', status: 'running', phase: 'analysis', provider: 'secret' },
          { label: 'Reviewer 2', status: 'queued' },
        ],
        ignored: 'not projected',
      },
      ignored: 'not projected',
    },
  })

  assert.deepEqual(projected, {
    schema_version: 'deep-pattern-audit-card.v1',
    tool: 'audit_skill_status',
    task_id: 'aud_card_1',
    status: 'running',
    tier: 'standard',
    progress: {
      schema_version: 'audit-progress.v1',
      phase: 'reviewing',
      terminal: false,
      elapsed_ms: 12500,
      poll_after_ms: 20000,
      auditors: [
        { label: 'Reviewer 1', model: 'Model A', status: 'running', phase: 'analysis' },
        { label: 'Reviewer 2', status: 'queued' },
      ],
    },
    progress_url: PROGRESS_URL,
  })
})

test('projects only bounded terminal result text', () => {
  const projected = projectAuditPresentation('audit_skill_result', {
    content: [{ type: 'text', text: `Final result\n${'x'.repeat(800)}` }],
    structuredContent: { task_id: 'aud_done', status: 'completed' },
  })
  assert.equal(projected.summary.length, 500)
  assert.match(projected.summary, /^Final result/)
})

test('rejects malformed identifiers, untrusted links, secrets, and oversized values', () => {
  const projected = projectAuditPresentation('audit_skill_submit', {
    content: [{
      type: 'resource_link',
      name: 'progress',
      uri: 'https://attacker.invalid/plugin/audit/progress/exchange?t=token',
    }],
    structuredContent: {
      task_id: 'bad task id',
      status: 'RUNNING NOW',
      tier: 'x'.repeat(100),
      access_token: 'must-not-survive',
      progress: {
        elapsed_ms: -1,
        poll_after_ms: Number.MAX_SAFE_INTEGER,
        auditors: [{ label: 'Reviewer', status: 'running', credential: 'must-not-survive' }],
      },
    },
  })
  const serialized = JSON.stringify(projected)
  assert.equal(projected.task_id, undefined)
  assert.equal(projected.status, undefined)
  assert.equal(projected.progress_url, undefined)
  assert.equal(projected.tier.length, 32)
  assert.doesNotMatch(serialized, /access_token|credential|must-not-survive|attacker/)
  assert.deepEqual(projected.progress, { auditors: [{ label: 'Reviewer', status: 'running' }] })
})

test('unknown or malformed canonical values retain only the card and tool identity', () => {
  assert.deepEqual(projectAuditPresentation('audit_skill_events', null), {
    schema_version: 'deep-pattern-audit-card.v1',
    tool: 'audit_skill_events',
  })
})

test('all four registered MCP definitions persist the bounded card projection', () => {
  const manager = new DeepPatternAuditManager({
    credentials: {},
    logger() { return { warn() {} } },
  })
  const rawNames = [
    'audit_skill_submit',
    'audit_skill_status',
    'audit_skill_result',
    'audit_skill_events',
  ]
  const definitions = manager.buildDefinitions(
    { callTool() { throw new Error('not called') } },
    rawNames.map((name) => ({ name, description: name, inputSchema: { type: 'object' } })),
  )
  for (const rawName of rawNames) {
    const definition = definitions.get(`mcp__audit__${rawName}`)
    assert.equal(typeof definition.output.presentationMeta, 'function')
    assert.equal(definition.output.presentationMeta({}, { content: [] }).tool, rawName)
  }
})
