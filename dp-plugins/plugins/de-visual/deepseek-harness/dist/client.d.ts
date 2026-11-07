declare const TOOL_NAMES: readonly ["mcp__visual__visual_submit", "mcp__visual__visual_status", "mcp__visual__visual_result", "mcp__visual__visual_cancel", "mcp__visual__visual_open_view"];
type VisualWireName = (typeof TOOL_NAMES)[number];
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
export interface VisualCardModel {
    title: string;
    status: string;
    state: CardState;
    taskId?: string;
    mode?: string;
    progressUrl?: string;
    summary?: string;
    artifactAvailable?: boolean;
}
export declare function visualCardModel(toolName: string, phase: 'preparing' | 'start' | 'result', block: unknown): VisualCardModel;
export declare const inject: readonly ["slots"];
export declare function apply(ctx: ClientContext): void;
export declare const visualToolViewNames: readonly VisualWireName[];
export {};
