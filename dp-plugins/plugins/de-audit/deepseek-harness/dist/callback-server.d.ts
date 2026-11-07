export interface CallbackResult {
    params: URLSearchParams;
    respondSuccess(): void;
    respondFailure(): void;
}
export declare class OAuthCallbackServer {
    private readonly onCallback;
    private readonly onTimeout;
    private server;
    private timer;
    private callbackUrlValue;
    private consumed;
    private consumedUrl;
    private completion;
    private readonly pendingResponses;
    constructor(onCallback: (result: CallbackResult) => Promise<void>, onTimeout: () => void);
    get callbackUrl(): URL;
    start(timeoutMs?: number): Promise<URL>;
    private handle;
    private finish;
    close(): Promise<void>;
}
