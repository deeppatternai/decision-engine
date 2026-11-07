import { CLIENT_ID, RESOURCE_URL } from './constants.js';
const AUTHORIZATION_ORIGIN = new URL(RESOURCE_URL).origin;
function jsonClone(value) {
    const serialized = JSON.stringify(value);
    if (serialized === undefined)
        throw new Error('OAuth state is not JSON serializable');
    return JSON.parse(serialized);
}
export class SnapshotOAuthProvider {
    payloadValue;
    redirectUri;
    authorizationUrlValue;
    constructor(payloadValue, redirectUri) {
        this.payloadValue = payloadValue;
        this.redirectUri = redirectUri;
    }
    get redirectUrl() {
        return this.redirectUri;
    }
    get clientMetadata() {
        return {
            client_name: 'Deep Pattern Visual for DeepSeek Harness',
            redirect_uris: [this.redirectUri],
            grant_types: ['authorization_code', 'refresh_token'],
            response_types: ['code'],
            token_endpoint_auth_method: 'none',
        };
    }
    state() {
        const state = this.payloadValue.pending?.state;
        if (!state)
            throw new Error('OAuth state is unavailable');
        return state;
    }
    clientInformation(ctx) {
        return {
            client_id: CLIENT_ID,
            ...(ctx?.issuer ? { issuer: ctx.issuer } : {}),
        };
    }
    tokens() {
        return this.payloadValue.tokens ? jsonClone(this.payloadValue.tokens) : undefined;
    }
    saveTokens(tokens) {
        this.payloadValue.tokens = jsonClone(tokens);
    }
    redirectToAuthorization(authorizationUrl) {
        if (authorizationUrl.protocol !== 'https:' || authorizationUrl.origin !== AUTHORIZATION_ORIGIN) {
            throw new Error('authorization server returned an unexpected authorization URL');
        }
        this.authorizationUrlValue = new URL(authorizationUrl);
    }
    takeAuthorizationUrl() {
        const value = this.authorizationUrlValue;
        this.authorizationUrlValue = undefined;
        return value;
    }
    saveCodeVerifier(codeVerifier) {
        if (!this.payloadValue.pending)
            throw new Error('OAuth pending state is unavailable');
        this.payloadValue.pending.codeVerifier = codeVerifier;
    }
    codeVerifier() {
        const verifier = this.payloadValue.pending?.codeVerifier;
        if (!verifier)
            throw new Error('OAuth PKCE verifier is unavailable');
        return verifier;
    }
    saveDiscoveryState(state) {
        this.payloadValue.discovery = jsonClone(state);
    }
    discoveryState() {
        return this.payloadValue.discovery ? jsonClone(this.payloadValue.discovery) : undefined;
    }
    saveResourceUrl(resourceUrl) {
        if (resourceUrl !== RESOURCE_URL)
            throw new Error('OAuth resource does not match the visual endpoint');
    }
    async validateResourceURL(_serverUrl, resource) {
        if (resource !== undefined && resource !== RESOURCE_URL) {
            throw new Error('OAuth protected resource does not match the visual endpoint');
        }
        return new URL(RESOURCE_URL);
    }
    invalidateCredentials(scope) {
        if (scope === 'all' || scope === 'tokens')
            delete this.payloadValue.tokens;
        if (scope === 'all' || scope === 'verifier') {
            if (this.payloadValue.pending)
                delete this.payloadValue.pending.codeVerifier;
        }
        if (scope === 'all' || scope === 'discovery')
            delete this.payloadValue.discovery;
    }
    snapshot() {
        return jsonClone(this.payloadValue);
    }
}
