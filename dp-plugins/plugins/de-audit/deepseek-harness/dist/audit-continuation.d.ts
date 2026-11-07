import type { ToolExecution, ToolRunContext } from '@deepseek-ai/dsh-tools';
import type { AuditToolName } from './constants.js';
declare module '@deepseek-ai/dsh-llm' {
    interface MessageSourceMap {
        'deep-pattern-audit': {
            kind: 'deep-pattern-audit';
            taskId: string;
            action: 'show-progress-and-poll' | 'poll' | 'fetch-result';
        };
    }
}
type ContinuationExecution = ToolExecution & Partial<Pick<ToolRunContext, 'deferContext'>>;
export declare function deferAuditContinuation(rawName: AuditToolName, result: unknown, execution: ContinuationExecution): void;
export {};
