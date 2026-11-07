import type {
  OAuthClientInformationContext,
  OAuthClientMetadata,
  OAuthClientProvider,
  OAuthDiscoveryState,
  StoredOAuthClientInformation,
  StoredOAuthTokens,
} from '@modelcontextprotocol/client'

import { CLIENT_ID, RESOURCE_URL } from './constants.js'
import type { OAuthGrantPayload } from './oauth-store.js'

const AUTHORIZATION_ORIGIN = new URL(RESOURCE_URL).origin

function jsonClone<T>(value: T): T {
  const serialized = JSON.stringify(value)
  if (serialized === undefined) throw new Error('OAuth state is not JSON serializable')
  return JSON.parse(serialized) as T
}

export class SnapshotOAuthProvider implements OAuthClientProvider {
  private authorizationUrlValue: URL | undefined

  constructor(
    private readonly payloadValue: OAuthGrantPayload,
    private readonly redirectUri: string,
  ) {}

  get redirectUrl(): string {
    return this.redirectUri
  }

  get clientMetadata(): OAuthClientMetadata {
    return {
      client_name: 'Deep Pattern Audit for DeepSeek Harness',
      redirect_uris: [this.redirectUri],
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
    }
  }

  state(): string {
    const state = this.payloadValue.pending?.state
    if (!state) throw new Error('OAuth state is unavailable')
    return state
  }

  clientInformation(ctx?: OAuthClientInformationContext): StoredOAuthClientInformation {
    return {
      client_id: CLIENT_ID,
      ...(ctx?.issuer ? { issuer: ctx.issuer } : {}),
    }
  }

  tokens(): StoredOAuthTokens | undefined {
    return this.payloadValue.tokens ? jsonClone(this.payloadValue.tokens) : undefined
  }

  saveTokens(tokens: StoredOAuthTokens): void {
    this.payloadValue.tokens = jsonClone(tokens)
  }

  redirectToAuthorization(authorizationUrl: URL): void {
    if (authorizationUrl.protocol !== 'https:' || authorizationUrl.origin !== AUTHORIZATION_ORIGIN) {
      throw new Error('authorization server returned an unexpected authorization URL')
    }
    this.authorizationUrlValue = new URL(authorizationUrl)
  }

  takeAuthorizationUrl(): URL | undefined {
    const value = this.authorizationUrlValue
    this.authorizationUrlValue = undefined
    return value
  }

  saveCodeVerifier(codeVerifier: string): void {
    if (!this.payloadValue.pending) throw new Error('OAuth pending state is unavailable')
    this.payloadValue.pending.codeVerifier = codeVerifier
  }

  codeVerifier(): string {
    const verifier = this.payloadValue.pending?.codeVerifier
    if (!verifier) throw new Error('OAuth PKCE verifier is unavailable')
    return verifier
  }

  saveDiscoveryState(state: OAuthDiscoveryState): void {
    this.payloadValue.discovery = jsonClone(state)
  }

  discoveryState(): OAuthDiscoveryState | undefined {
    return this.payloadValue.discovery ? jsonClone(this.payloadValue.discovery) : undefined
  }

  saveResourceUrl(resourceUrl: string): void {
    if (resourceUrl !== RESOURCE_URL) throw new Error('OAuth resource does not match the audit endpoint')
  }

  async validateResourceURL(_serverUrl: string | URL, resource?: string): Promise<URL> {
    if (resource !== undefined && resource !== RESOURCE_URL) {
      throw new Error('OAuth protected resource does not match the audit endpoint')
    }
    return new URL(RESOURCE_URL)
  }

  invalidateCredentials(scope: 'all' | 'client' | 'tokens' | 'verifier' | 'discovery'): void {
    if (scope === 'all' || scope === 'tokens') delete this.payloadValue.tokens
    if (scope === 'all' || scope === 'verifier') {
      if (this.payloadValue.pending) delete this.payloadValue.pending.codeVerifier
    }
    if (scope === 'all' || scope === 'discovery') delete this.payloadValue.discovery
  }

  snapshot(): OAuthGrantPayload {
    return jsonClone(this.payloadValue)
  }
}
