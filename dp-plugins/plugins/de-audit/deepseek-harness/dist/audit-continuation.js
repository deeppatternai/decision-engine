import { createUserMessage } from '@deepseek-ai/dsh-llm';
const TERMINAL_STATUSES = new Set([
    'cancelled',
    'completed',
    'connection_timeout',
    'failed',
    'partial',
    'timeout',
]);
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function auditState(result) {
    if (!isRecord(result) ||
        result.isError === true ||
        !Array.isArray(result.content) ||
        !isRecord(result.structuredContent)) {
        return undefined;
    }
    const payload = result.structuredContent;
    if (typeof payload.task_id !== 'string' ||
        !/^[A-Za-z0-9._:-]{1,128}$/.test(payload.task_id)) {
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
            `Continue the Deep Pattern Audit task ${JSON.stringify(taskId)} in this same turn.`,
            'Before calling another tool, send a user-visible assistant update containing the exact `Open audit progress:` Markdown line from the immediately preceding tool result and',
            `\`Task ID: ${taskId}\`.`,
            `Then call \`mcp__audit__audit_skill_status\` exactly once with \`{"task_id":${JSON.stringify(taskId)},"include_view_resource":false,"wait_seconds":20}\`.`,
            'Do not submit another audit, create another idempotency key, replace the task ID, or end the turn while the task is non-terminal.',
        ].join(' ');
    }
    if (action === 'poll') {
        return [
            `Continue the existing Deep Pattern Audit task ${JSON.stringify(taskId)} in this same turn.`,
            `Call \`mcp__audit__audit_skill_status\` exactly once with \`{"task_id":${JSON.stringify(taskId)},"include_view_resource":false,"wait_seconds":20}\`.`,
            'Do not submit again, change the task ID, call events as a polling substitute, or end the turn while the task is non-terminal.',
        ].join(' ');
    }
    return [
        `Deep Pattern Audit task ${JSON.stringify(taskId)} is terminal.`,
        `Call \`mcp__audit__audit_skill_result\` exactly once with \`{"task_id":${JSON.stringify(taskId)}}\`, then summarize the complete result for the user in this conversation.`,
        'Do not submit again, poll again, or end the turn before returning that result.',
    ].join(' ');
}
export function deferAuditContinuation(rawName, result, execution) {
    if (rawName !== 'audit_skill_submit' && rawName !== 'audit_skill_status')
        return;
    if (typeof execution.deferContext !== 'function')
        return;
    const state = auditState(result);
    if (!state)
        return;
    const action = rawName === 'audit_skill_submit'
        ? 'show-progress-and-poll'
        : state.terminal
            ? 'fetch-result'
            : 'poll';
    execution.deferContext(createUserMessage({
        content: [{ type: 'text', text: continuationText(action, state.taskId) }],
        source: { kind: 'deep-pattern-audit', taskId: state.taskId, action },
    }));
}
