import assert from 'node:assert/strict'
import test from 'node:test'

import { DeepPatternAuditManager } from '../dist/audit-client.js'

test('registers the local control tool before any OAuth grant exists', async () => {
  let registered
  const context = {
    credentials: {
      readRecord: async () => undefined,
      modifyRecord: async () => assert.fail('unexpected credential write'),
      deleteRecord: async () => undefined,
    },
    logger: Object.assign(() => ({ warn() {}, info() {}, error() {}, debug() {} }), {
      warn() {},
      info() {},
      error() {},
      debug() {},
    }),
    tools: {
      register(definition) {
        registered = definition
        return () => undefined
      },
    },
  }
  const manager = new DeepPatternAuditManager(context)
  const dispose = manager.registerControlTool()
  assert.equal(registered.name, 'mcp__audit__connect')
  const result = await registered.execute({ action: 'status', wait_ms: 0 })
  assert.deepEqual(result, {
    state: 'disconnected',
    message: 'Deep Pattern Audit is not connected.',
    tools: [],
  })
  dispose()
  await manager.dispose()
})
