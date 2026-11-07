import { type CredentialProvider } from '@deepseek-ai/dsh-credentials';
import type { OAuthDiscoveryState, StoredOAuthTokens } from '@modelcontextprotocol/client';
import { RESOURCE_URL } from './constants.js';
export interface PendingAuthorization {
    state: string;
    redirectUri: string;
    createdAt: string;
    codeVerifier?: string;
}
export interface OAuthGrantPayload {
    version: 1;
    resourceUrl: typeof RESOURCE_URL;
    tokens?: StoredOAuthTokens;
    discovery?: OAuthDiscoveryState;
    pending?: PendingAuthorization;
}
export declare function parseGrantPayload(value: unknown): OAuthGrantPayload;
export declare function emptyGrantPayload(): OAuthGrantPayload;
export declare class OAuthGrantStore {
    private readonly credentials;
    constructor(credentials: CredentialProvider);
    read(): Promise<OAuthGrantPayload | undefined>;
    modify(mutate: (current: OAuthGrantPayload | undefined) => Promise<OAuthGrantPayload>): Promise<OAuthGrantPayload>;
    delete(): Promise<void>;
}
