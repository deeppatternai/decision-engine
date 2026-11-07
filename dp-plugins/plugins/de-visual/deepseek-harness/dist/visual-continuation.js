import { createUserMessage } from '@deepseek-ai/dsh-llm';
const TERMINAL_STATUSES = new Set([
    'cancelled',
    'completed',
    'failed',
]);
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function visualState(result) {
    if (!isRecord(result) ||
        result.isError === true ||
        !Array.isArray(result.content) ||
        !isRecord(result.structuredContent)) {
        return undefined;
    }
    const payload = result.structuredContent;
    if (typeof payload.task_id !== 'string' ||
        !/^vis_[0-9]+$/.test(payload.task_id)) {
        return undefined;
    }
    const progress = isRecord(payload.progress) ? payload.progress : undefined;
    const terminal = typeof progress?.terminal === 'boolean'
        ? progress.terminal
        : typeof payload.status === 'string' && TERMINAL_STATUSES.has(payload.status);
    return { taskId: payload.task_id, terminal };
}
function continuationText(action, taskId) {
    if (action === 'show-progress-and-poll') {
        return [
            `Continue the Deep Pattern Visual task ${JSON.stringify(taskId)} in this same turn.`,
            'Before calling another tool, send a user-visible assistant update containing the exact `Open visual progress:` Markdown line from the immediately preceding tool result and',
            `\`Task ID: ${taskId}\`.`,
            `Then call \`mcp__visual__visual_status\` exactly once with \`{"task_id":${JSON.stringify(taskId)},"include_view_resource":false,"wait_seconds":20}\`.`,
            'Do not submit another visual, create another idempotency key, replace the task ID, or end the turn while the task is non-terminal.',
        ].join(' ');
    }
    if (action === 'poll') {
        return [
            `Continue the existing Deep Pattern Visual task ${JSON.stringify(taskId)} in this same turn.`,
            `Call \`mcp__visual__visual_status\` exactly once with \`{"task_id":${JSON.stringify(taskId)},"include_view_resource":false,"wait_seconds":20}\`.`,
            'Do not submit again, change the task ID, call another tool as a polling substitute, or end the turn while the task is non-terminal.',
        ].join(' ');
    }
    return [
        `Deep Pattern Visual task ${JSON.stringify(taskId)} is terminal.`,
        `Call \`mcp__visual__visual_result\` exactly once with \`{"task_id":${JSON.stringify(taskId)}}\`, then summarize the complete result for the user in this conversation.`,
        'Do not submit again, poll again, or end the turn before returning that result.',
    ].join(' ');
}
export function deferVisualContinuation(rawName, result, execution) {
    if (rawName !== 'visual_submit' && rawName !== 'visual_status')
        return;
    if (typeof execution.deferContext !== 'function')
        return;
    const state = visualState(result);
    if (!state)
        return;
    const action = rawName === 'visual_submit'
        ? 'show-progress-and-poll'
        : state.terminal
            ? 'fetch-result'
            : 'poll';
    execution.deferContext(createUserMessage({
        content: [{ type: 'text', text: continuationText(action, state.taskId) }],
        source: { kind: 'deep-pattern-visual', taskId: state.taskId, action },
    }));
}
