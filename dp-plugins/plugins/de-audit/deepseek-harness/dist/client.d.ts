declare const TOOL_NAMES: readonly ["mcp__audit__audit_skill_submit", "mcp__audit__audit_skill_status", "mcp__audit__audit_skill_result", "mcp__audit__audit_skill_events"];
type AuditWireName = (typeof TOOL_NAMES)[number];
type CardState = 'preparing' | 'running' | 'done' | 'warning' | 'error' | 'idle';
interface ToolCallViewProps {
    toolName: string;
    phase: 'preparing' | 'start' | 'result';
    block: unknown;
}
interface ClientContext {
    slots: {
        inject(name: 'tool.call.toolview', callback: () => unknown): unknown;
        register(options: {
            name: 'tool.call.toolview';
            key: string;
        }, component: (props: ToolCallViewProps) => unknown): unknown;
    };
}
export interface AuditCardAuditor {
    label: string;
    model?: string;
    status?: string;
    phase?: string;
}
export interface AuditCardModel {
    title: string;
    status: string;
    state: CardState;
    taskId?: string;
    phase?: string;
    elapsedMs?: number;
    tier?: string;
    progressUrl?: string;
    summary?: string;
    auditors: AuditCardAuditor[];
}
export declare function auditCardModel(toolName: string, phase: 'preparing' | 'start' | 'result', block: unknown): AuditCardModel;
export declare const inject: readonly ["slots"];
export declare function apply(ctx: ClientContext): void;
export declare const auditToolViewNames: readonly AuditWireName[];
export {};
