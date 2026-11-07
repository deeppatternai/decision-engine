import type { Context } from '@deepseek-ai/cordis'

import { DeepPatternAuditManager } from './audit-client.js'
import { PLUGIN_NAME } from './constants.js'

export const name = PLUGIN_NAME
export const inject = ['tools', 'credentials']

export async function apply(ctx: Context): Promise<() => Promise<void>> {
  const manager = new DeepPatternAuditManager(ctx)
  const disposeControl = manager.registerControlTool()
  manager.restore()
  return async () => {
    disposeControl()
    await manager.dispose()
  }
}
