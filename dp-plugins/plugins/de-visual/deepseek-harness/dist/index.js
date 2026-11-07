import { DeepPatternVisualManager } from './visual-client.js';
import { PLUGIN_NAME } from './constants.js';
export const name = PLUGIN_NAME;
export const inject = ['tools', 'credentials'];
export async function apply(ctx) {
    const manager = new DeepPatternVisualManager(ctx);
    const disposeControl = manager.registerControlTool();
    manager.restore();
    return async () => {
        disposeControl();
        await manager.dispose();
    };
}
