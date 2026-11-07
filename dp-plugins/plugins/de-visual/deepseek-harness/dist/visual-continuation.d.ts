import type { ToolExecution, ToolRunContext } from '@deepseek-ai/dsh-tools';
import type { VisualToolName } from './constants.js';
declare module '@deepseek-ai/dsh-llm' {
    interface MessageSourceMap {
        'deep-pattern-visual': {
            kind: 'deep-pattern-visual';
            taskId: string;
            action: 'show-progress-and-poll' | 'poll' | 'fetch-result';
        };
    }
}
type ContinuationExecution = ToolExecution & Partial<Pick<ToolRunContext, 'deferContext'>>;
export declare function deferVisualContinuation(rawName: VisualToolName, result: unknown, execution: ContinuationExecution): void;
export {};
