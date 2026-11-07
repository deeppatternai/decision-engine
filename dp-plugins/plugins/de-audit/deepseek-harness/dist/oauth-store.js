import { credentialKey, } from '@deepseek-ai/dsh-credentials';
import { CALLBACK_PATH, CREDENTIAL_RECORD_ID, PLUGIN_NAME, RESOURCE_URL } from './constants.js';
const KEY = credentialKey(PLUGIN_NAME, CREDENTIAL_RECORD_ID);
const TOP_LEVEL_KEYS = new Set(['version', 'resourceUrl', 'tokens', 'discovery', 'pending']);
const PENDING_KEYS = new Set(['state', 'redirectUri', 'createdAt', 'codeVerifier']);
function isRecord(value) {
    if (typeof value !== 'object' || value === null || Array.isArray(value))
        return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
}
function isJsonValue(value) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean')
        return true;
    if (typeof value === 'number')
        return Number.isFinite(value);
    if (Array.isArray(value))
        return value.every(isJsonValue);
    if (!isRecord(value))
        return false;
    return Object.values(value).every((item) => item !== undefined && isJsonValue(item));
}
function hasOnlyKeys(value, keys) {
    return Object.keys(value).every((key) => keys.has(key));
}
function validTokens(value) {
    if (!isRecord(value) || !isJsonValue(value))
        return false;
    return (typeof value.access_token === 'string' &&
        value.access_token.length > 0 &&
        typeof value.token_type === 'string' &&
        value.token_type.length > 0 &&
        (value.refresh_token === undefined || typeof value.refresh_token === 'string') &&
        (value.expires_in === undefined || (typeof value.expires_in === 'number' && value.expires_in >= 0)) &&
        (value.scope === undefined || typeof value.scope === 'string') &&
        (value.issuer === undefined || typeof value.issuer === 'string'));
}
function validDiscovery(value) {
    if (!isRecord(value) ||
        !isJsonValue(value) ||
        typeof value.authorizationServerUrl !== 'string') {
        return false;
    }
    try {
        const authorizationServer = new URL(value.authorizationServerUrl);
        return (authorizationServer.protocol === 'https:' &&
            authorizationServer.origin === new URL(RESOURCE_URL).origin &&
            authorizationServer.username === '' &&
            authorizationServer.password === '');
    }
    catch {
        return false;
    }
}
function validPending(value) {
    if (!isRecord(value) || !hasOnlyKeys(value, PENDING_KEYS))
        return false;
    let redirectUri;
    try {
        redirectUri = new URL(typeof value.redirectUri === 'string' ? value.redirectUri : '');
    }
    catch {
        return false;
    }
    return (typeof value.state === 'string' &&
        value.state.length >= 32 &&
        redirectUri.protocol === 'http:' &&
        redirectUri.hostname === '127.0.0.1' &&
        redirectUri.port !== '' &&
        redirectUri.pathname === CALLBACK_PATH &&
        redirectUri.username === '' &&
        redirectUri.password === '' &&
        redirectUri.search === '' &&
        redirectUri.hash === '' &&
        typeof value.createdAt === 'string' &&
        Number.isFinite(Date.parse(value.createdAt)) &&
        (value.codeVerifier === undefined ||
            (typeof value.codeVerifier === 'string' && value.codeVerifier.length >= 43)));
}
export function parseGrantPayload(value) {
    if (!isRecord(value) || !hasOnlyKeys(value, TOP_LEVEL_KEYS)) {
        throw new Error('stored Deep Pattern OAuth grant has an invalid shape');
    }
    if (value.version !== 1 || value.resourceUrl !== RESOURCE_URL) {
        throw new Error('stored Deep Pattern OAuth grant has an unsupported version or resource');
    }
    if (value.tokens !== undefined && !validTokens(value.tokens)) {
        throw new Error('stored Deep Pattern OAuth tokens are invalid');
    }
    if (value.discovery !== undefined && !validDiscovery(value.discovery)) {
        throw new Error('stored Deep Pattern OAuth discovery state is invalid');
    }
    if (value.pending !== undefined && !validPending(value.pending)) {
        throw new Error('stored Deep Pattern OAuth pending state is invalid');
    }
    return structuredClone(value);
}
export function emptyGrantPayload() {
    return { version: 1, resourceUrl: RESOURCE_URL };
}
function payloadFromRecord(record) {
    if (record === undefined)
        return undefined;
    if (record.kind !== 'grant') {
        throw new Error('Deep Pattern credential record is not an OAuth grant');
    }
    return parseGrantPayload(record.payload);
}
export class OAuthGrantStore {
    credentials;
    constructor(credentials) {
        this.credentials = credentials;
    }
    async read() {
        return payloadFromRecord(await this.credentials.readRecord(KEY));
    }
    async modify(mutate) {
        const result = await this.credentials.modifyRecord(KEY, async (record) => {
            const next = parseGrantPayload(await mutate(payloadFromRecord(record)));
            return { kind: 'grant', payload: next };
        });
        const payload = payloadFromRecord(result);
        if (!payload)
            throw new Error('credential provider did not persist the OAuth grant');
        return payload;
    }
    async delete() {
        await this.credentials.deleteRecord(KEY);
    }
}
