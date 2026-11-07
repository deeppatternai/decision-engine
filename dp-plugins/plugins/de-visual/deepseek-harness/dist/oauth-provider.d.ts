import type { OAuthClientInformationContext, OAuthClientMetadata, OAuthClientProvider, OAuthDiscoveryState, StoredOAuthClientInformation, StoredOAuthTokens } from '@modelcontextprotocol/client';
import type { OAuthGrantPayload } from './oauth-store.js';
export declare class SnapshotOAuthProvider implements OAuthClientProvider {
    private readonly payloadValue;
    private readonly redirectUri;
    private authorizationUrlValue;
    constructor(payloadValue: OAuthGrantPayload, redirectUri: string);
    get redirectUrl(): string;
    get clientMetadata(): OAuthClientMetadata;
    state(): string;
    clientInformation(ctx?: OAuthClientInformationContext): StoredOAuthClientInformation;
    tokens(): StoredOAuthTokens | undefined;
    saveTokens(tokens: StoredOAuthTokens): void;
    redirectToAuthorization(authorizationUrl: URL): void;
    takeAuthorizationUrl(): URL | undefined;
    saveCodeVerifier(codeVerifier: string): void;
    codeVerifier(): string;
    saveDiscoveryState(state: OAuthDiscoveryState): void;
    discoveryState(): OAuthDiscoveryState | undefined;
    saveResourceUrl(resourceUrl: string): void;
    validateResourceURL(_serverUrl: string | URL, resource?: string): Promise<URL>;
    invalidateCredentials(scope: 'all' | 'client' | 'tokens' | 'verifier' | 'discovery'): void;
    snapshot(): OAuthGrantPayload;
}
