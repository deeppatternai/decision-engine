import { createServer } from 'node:http';
import { CALLBACK_HOST, CALLBACK_PATH, CALLBACK_TIMEOUT_MS } from './constants.js';
function writePage(response, ok) {
    const title = ok ? 'Deep Pattern Visual connected' : 'Deep Pattern Visual connection failed';
    const detail = ok
        ? 'Authorization completed. You can close this window and return to DeepSeek Harness.'
        : 'Authorization did not complete. Close this window and retry from DeepSeek Harness.';
    const body = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>body{margin:0;background:#101416;color:#edf1f2;font:16px/1.6 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}main{max-width:520px;padding:32px;border:1px solid #354044;border-radius:6px;background:#171c1f}h1{font-size:23px;margin:0 0 12px;color:#e7bd67}p{margin:0;color:#bec8cb}</style></head><body><main><h1>${title}</h1><p>${detail}</p></main></body></html>`;
    response.writeHead(ok ? 200 : 400, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        'x-content-type-options': 'nosniff',
    });
    response.end(body);
}
export class OAuthCallbackServer {
    onCallback;
    onTimeout;
    server;
    timer;
    callbackUrlValue;
    consumed = false;
    consumedUrl;
    completion;
    pendingResponses = new Set();
    constructor(onCallback, onTimeout) {
        this.onCallback = onCallback;
        this.onTimeout = onTimeout;
    }
    get callbackUrl() {
        if (!this.callbackUrlValue)
            throw new Error('callback server has not started');
        return this.callbackUrlValue;
    }
    async start(timeoutMs = CALLBACK_TIMEOUT_MS) {
        if (this.server)
            return this.callbackUrl;
        this.server = createServer((request, response) => {
            void this.handle(request.method, request.url, response);
        });
        await new Promise((resolve, reject) => {
            this.server?.once('error', reject);
            this.server?.listen(0, CALLBACK_HOST, () => resolve());
        });
        const address = this.server.address();
        if (!address || typeof address === 'string') {
            await this.close();
            throw new Error('callback server did not bind a TCP port');
        }
        this.callbackUrlValue = new URL(`http://${CALLBACK_HOST}:${address.port}${CALLBACK_PATH}`);
        this.timer = setTimeout(() => {
            this.onTimeout();
            void this.close();
        }, timeoutMs);
        this.timer.unref();
        return this.callbackUrl;
    }
    async handle(method, rawUrl, response) {
        const requestUrl = new URL(rawUrl ?? '/', this.callbackUrl);
        if (method !== 'GET' || requestUrl.pathname !== CALLBACK_PATH) {
            response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
            response.end('Not found');
            return;
        }
        if (this.consumed) {
            if (requestUrl.href !== this.consumedUrl) {
                response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
                response.end('Not found');
                return;
            }
            if (this.completion !== undefined) {
                writePage(response, this.completion);
            }
            else {
                this.pendingResponses.add(response);
            }
            return;
        }
        this.consumed = true;
        this.consumedUrl = requestUrl.href;
        this.pendingResponses.add(response);
        const result = {
            params: requestUrl.searchParams,
            respondSuccess: () => this.finish(true),
            respondFailure: () => this.finish(false),
        };
        try {
            await this.onCallback(result);
        }
        catch {
            result.respondFailure();
        }
        finally {
            if (this.completion === undefined)
                result.respondFailure();
            await this.close();
        }
    }
    finish(ok) {
        if (this.completion !== undefined)
            return;
        this.completion = ok;
        for (const response of this.pendingResponses) {
            if (!response.writableEnded)
                writePage(response, ok);
        }
        this.pendingResponses.clear();
    }
    async close() {
        if (this.timer)
            clearTimeout(this.timer);
        this.timer = undefined;
        if (this.pendingResponses.size > 0)
            this.finish(false);
        const current = this.server;
        this.server = undefined;
        if (!current)
            return;
        await new Promise((resolve) => current.close(() => resolve()));
    }
}
