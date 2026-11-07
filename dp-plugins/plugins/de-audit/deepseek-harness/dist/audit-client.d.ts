import type { Context } from '@deepseek-ai/cordis';
export type ConnectionState = 'disconnected' | 'authorizing' | 'connecting' | 'connected' | 'failed';
interface ControlResult {
    state: ConnectionState;
    message: string;
    tools: string[];
}
export declare class DeepPatternAuditManager {
    private readonly ctx;
    private readonly store;
    private readonly logger;
    private stateValue;
    private stateMessage;
    private queue;
    private client;
    private remoteDisposers;
    private callbackServer;
    private activeAttempt;
    private waiters;
    private disposed;
    constructor(ctx: Context);
    registerControlTool(): () => void;
    restore(): void;
    connect(): Promise<ControlResult>;
    status(waitMs?: number): Promise<ControlResult>;
    logout(): Promise<ControlResult>;
    dispose(): Promise<void>;
    private enqueue;
    private result;
    private setState;
    private startInteractiveAuthorization;
    private completeAuthorization;
    private connectRemote;
    private buildDefinitions;
    private refreshAfterUnauthorized;
    private disconnectRemote;
    private stopCallbackServer;
}
export {};
