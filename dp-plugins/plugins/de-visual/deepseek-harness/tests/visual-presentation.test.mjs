import assert from 'node:assert/strict'
import test from 'node:test'

import { DeepPatternVisualManager } from '../dist/visual-client.js'
import { projectVisualPresentation } from '../dist/visual-presentation.js'

const VIEW_URL = 'https://endpoint.deeppattern.ai/plugin/visual/view/exchange#t=opaque'

test('projects bounded replay-safe visual progress metadata', () => {
  const projected = projectVisualPresentation('visual_status', {
    content: [{ type: 'resource_link', name: 'visual-view', uri: VIEW_URL }],
    structuredContent: {
      task_id: 'vis_201',
      title: 'Service topology',
      mode: 'diagram',
      status: 'running',
      terminal: false,
      artifact_available: false,
      ignored: 'not projected',
    },
  })

  assert.deepEqual(projected, {
    schema_version: 'deep-pattern-visual-card.v1',
    tool: 'visual_status',
    task_id: 'vis_201',
    title: 'Service topology',
    mode: 'diagram',
    status: 'running',
    terminal: false,
    artifact_available: false,
    progress_url: VIEW_URL,
  })
})

test('projects only bounded terminal result text', () => {
  const projected = projectVisualPresentation('visual_result', {
    content: [{ type: 'text', text: `Visual ready\n${'x'.repeat(800)}` }],
    structuredContent: {
      task_id: 'vis_202', status: 'completed', terminal: true, artifact_available: true,
    },
  })
  assert.equal(projected.summary.length, 500)
  assert.match(projected.summary, /^Visual ready/)
})

test('rejects malformed identifiers, untrusted links, secrets, and oversized values', () => {
  const projected = projectVisualPresentation('visual_submit', {
    content: [{
      type: 'resource_link',
      name: 'visual-view',
      uri: 'https://attacker.invalid/plugin/visual/view/exchange#t=token',
    }],
    structuredContent: {
      task_id: 'bad task id',
      title: 'x'.repeat(300),
      mode: 'unknown',
      status: 'RUNNING NOW',
      access_token: 'must-not-survive',
    },
  })
  const serialized = JSON.stringify(projected)
  assert.equal(projected.task_id, undefined)
  assert.equal(projected.title.length, 160)
  assert.equal(projected.mode, undefined)
  assert.equal(projected.status, undefined)
  assert.equal(projected.progress_url, undefined)
  assert.doesNotMatch(serialized, /access_token|must-not-survive|attacker/)
})

test('unknown or malformed canonical values retain only the card and tool identity', () => {
  assert.deepEqual(projectVisualPresentation('visual_open_view', null), {
    schema_version: 'deep-pattern-visual-card.v1',
    tool: 'visual_open_view',
  })
})

test('all five registered MCP definitions persist the bounded card projection', () => {
  const manager = new DeepPatternVisualManager({
    credentials: {},
    logger() { return { warn() {} } },
  })
  const rawNames = [
    'visual_submit',
    'visual_status',
    'visual_result',
    'visual_cancel',
    'visual_open_view',
  ]
  const definitions = manager.buildDefinitions(
    { callTool() { throw new Error('not called') } },
    rawNames.map((name) => ({ name, description: name, inputSchema: { type: 'object' } })),
  )
  for (const rawName of rawNames) {
    const definition = definitions.get(`mcp__visual__${rawName}`)
    assert.equal(typeof definition.output.presentationMeta, 'function')
    assert.equal(definition.output.presentationMeta({}, { content: [] }).tool, rawName)
  }
})
