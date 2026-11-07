import { randomBytes, timingSafeEqual } from 'node:crypto'

import type { Context } from '@deepseek-ai/cordis'
import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client'
import type { ToolDefinition, ToolExecution } from '@deepseek-ai/dsh-tools'
import {
  auth,
  Client,
  StreamableHTTPClientTransport,
  type AuthProvider,
  type Tool,
} from '@modelcontextprotocol/client'

import { openBrowser } from './browser.js'
import { deferAuditContinuation } from './audit-continuation.js'
import { projectAuditPresentation } from './audit-presentation.js'
import { type CallbackResult, OAuthCallbackServer } from './callback-server.js'
import {
  AUDIT_TOOL_NAMES,
  AUTH_SCOPES,
  CALLBACK_PATH,
  CLIENT_ID,
  PLUGIN_VERSION,
  RESOURCE_URL,
  SERVER_NAME,
  STATUS_WAIT_MAX_MS,
  TOOL_CALL_TIMEOUT_MS,
  TRANSPORT_AUTH_HEADER,
  TRANSPORT_AUTH_VALUE,
  type AuditToolName,
} from './constants.js'
import { emptyGrantPayload, OAuthGrantStore, type OAuthGrantPayload } from './oauth-store.js'
import { SnapshotOAuthProvider } from './oauth-provider.js'
import { exposeProgressLink } from './progress-link.js'

export type ConnectionState = 'disconnected' | 'authorizing' | 'connecting' | 'connected' | 'failed'

interface ControlResult {
  state: ConnectionState
  message: string
  tools: string[]
}

class AuthenticationRequiredError extends Error {}

function publicToolName(rawName: string): string {
  return `mcp__${SERVER_NAME}__${rawName}`
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

function safeFailureMessage(error: unknown): string {
  if (error instanceof AuthenticationRequiredError) return 'Authorization is required.'
  return 'Deep Pattern Audit could not connect. Retry with the connect action.'
}

function controlTool(manager: DeepPatternAuditManager): ToolDefinition {
  return {
    name: publicToolName('connect'),
    description:
      'Connect, inspect, or disconnect the Deep Pattern Audit OAuth session. Use connect when audit tools are unavailable.',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['connect', 'status', 'logout'] },
        wait_ms: { type: 'integer', minimum: 0, maximum: STATUS_WAIT_MAX_MS },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          state: { type: 'string' },
          message: { type: 'string' },
          tools: { type: 'array', items: { type: 'string' } },
        },
        required: ['state', 'message', 'tools'],
        additionalProperties: false,
      },
      render(_args, value) {
        const result = value as unknown as ControlResult
        return [{ type: 'text', text: `${result.message}\nState: ${result.state}` }]
      },
    },
    async execute(args) {
      if (typeof args !== 'object' || args === null || Array.isArray(args)) {
        throw new Error('connect action arguments must be an object')
      }
      const input = args as Record<string, unknown>
      const action = input.action
      const waitMs = input.wait_ms === undefined ? 0 : input.wait_ms
      if (action !== 'connect' && action !== 'status' && action !== 'logout') {
        throw new Error('action must be connect, status, or logout')
      }
      if (!Number.isInteger(waitMs) || (waitMs as number) < 0 || (waitMs as number) > STATUS_WAIT_MAX_MS) {
        throw new Error(`wait_ms must be an integer between 0 and ${STATUS_WAIT_MAX_MS}`)
      }
      if (action === 'connect') return manager.connect()
      if (action === 'logout') return manager.logout()
      return manager.status(waitMs as number)
    },
  }
}

export class DeepPatternAuditManager {
  private readonly store: OAuthGrantStore
  private readonly logger
  private stateValue: ConnectionState = 'disconnected'
  private stateMessage = 'Deep Pattern Audit is not connected.'
  private queue: Promise<void> = Promise.resolve()
  private client: Client | undefined
  private remoteDisposers = new Map<string, () => void>()
  private callbackServer: OAuthCallbackServer | undefined
  private activeAttempt: string | undefined
  private waiters = new Set<() => void>()
  private disposed = false

  constructor(private readonly ctx: Context) {
    this.store = new OAuthGrantStore(ctx.credentials)
    this.logger = ctx.logger('deep-pattern-audit')
  }

  registerControlTool(): () => void {
    return this.ctx.tools.register(controlTool(this))
  }

  restore(): void {
    void this.enqueue(async () => {
      const payload = await this.store.read()
      if (!payload?.tokens) return this.result()
      try {
        await this.connectRemote(false)
      } catch {
        this.setState('disconnected', 'Saved authorization requires reconnection.')
      }
      return this.result()
    }).catch(() => {
      this.logger.warn('saved Deep Pattern authorization could not be read')
      this.setState('failed', 'Saved authorization is invalid. Run logout, then connect again.')
    })
  }

  async connect(): Promise<ControlResult> {
    return this.enqueue(async () => {
      if (this.stateValue === 'connected') return this.result()
      if (this.stateValue === 'authorizing') return this.result()
      const payload = await this.store.read()
      if (payload?.tokens) {
        try {
          await this.connectRemote(true)
          return this.result()
        } catch (error) {
          if (!(error instanceof AuthenticationRequiredError)) {
            this.logger.warn('saved Deep Pattern authorization could not be restored')
          }
        }
      }
      await this.startInteractiveAuthorization()
      return this.result()
    })
  }

  async status(waitMs = 0): Promise<ControlResult> {
    const initial = this.stateValue
    if (waitMs > 0 && (initial === 'authorizing' || initial === 'connecting')) {
      await new Promise<void>((resolve) => {
        let timer: NodeJS.Timeout
        const done = (): void => {
          clearTimeout(timer)
          this.waiters.delete(done)
          resolve()
        }
        timer = setTimeout(done, waitMs)
        timer.unref()
        this.waiters.add(done)
      })
    }
    return this.result()
  }

  async logout(): Promise<ControlResult> {
    return this.enqueue(async () => {
      await this.stopCallbackServer()
      this.activeAttempt = undefined
      await this.disconnectRemote()
      await this.store.delete()
      this.setState('disconnected', 'Deep Pattern Audit authorization was removed from this Harness profile.')
      return this.result()
    })
  }

  async dispose(): Promise<void> {
    this.disposed = true
    this.activeAttempt = undefined
    await this.stopCallbackServer()
    await this.disconnectRemote()
    for (const resolve of this.waiters) resolve()
    this.waiters.clear()
  }

  private enqueue<T>(action: () => Promise<T>): Promise<T> {
    const running = this.queue.then(action, action)
    this.queue = running.then(
      () => undefined,
      () => undefined,
    )
    return running
  }

  private result(): ControlResult {
    return {
      state: this.stateValue,
      message: this.stateMessage,
      tools: [...this.remoteDisposers.keys()].sort(),
    }
  }

  private setState(state: ConnectionState, message: string): void {
    this.stateValue = state
    this.stateMessage = message
    for (const resolve of this.waiters) resolve()
    this.waiters.clear()
  }

  private async startInteractiveAuthorization(): Promise<void> {
    await this.stopCallbackServer()
    const attempt = randomBytes(24).toString('base64url')
    this.activeAttempt = attempt
    const callback = new OAuthCallbackServer(
      (result) => this.completeAuthorization(result, attempt),
      () => {
        if (this.activeAttempt === attempt) {
          this.activeAttempt = undefined
          this.setState('disconnected', 'Authorization timed out. Run connect to try again.')
        }
      },
    )
    this.callbackServer = callback
    const callbackUrl = await callback.start()
    let authorizationUrl: URL | undefined
    try {
      await this.store.modify(async (current) => {
        const payload: OAuthGrantPayload = current ?? emptyGrantPayload()
        delete payload.tokens
        payload.pending = {
          state: randomBytes(32).toString('base64url'),
          redirectUri: callbackUrl.href,
          createdAt: new Date().toISOString(),
        }
        const provider = new SnapshotOAuthProvider(payload, callbackUrl.href)
        const outcome = await auth(provider, { serverUrl: RESOURCE_URL, scope: AUTH_SCOPES })
        if (outcome !== 'REDIRECT') throw new Error('OAuth did not start an authorization redirect')
        authorizationUrl = provider.takeAuthorizationUrl()
        if (!authorizationUrl) throw new Error('OAuth did not provide an authorization URL')
        return provider.snapshot()
      })
      this.setState('authorizing', 'A browser authorization page was opened. Complete it, then check status.')
      await openBrowser(authorizationUrl as URL)
    } catch (error) {
      if (this.activeAttempt === attempt) this.activeAttempt = undefined
      await this.stopCallbackServer()
      this.setState('failed', safeFailureMessage(error))
      throw error
    }
  }

  private async completeAuthorization(callback: CallbackResult, attempt: string): Promise<void> {
    try {
      if (this.disposed || this.activeAttempt !== attempt) {
        throw new Error('OAuth authorization attempt is no longer active')
      }
      if (
        callback.params.getAll('state').length !== 1 ||
        callback.params.getAll('code').length !== 1 ||
        callback.params.getAll('iss').length > 1
      ) {
        throw new Error('OAuth callback parameters are ambiguous')
      }
      const state = callback.params.get('state')
      const code = callback.params.get('code')
      if (!state || !code) throw new Error('OAuth callback is missing required parameters')
      this.setState('connecting', 'Authorization completed. Connecting audit tools.')
      await this.store.modify(async (current) => {
        if (!current?.pending || !constantTimeEqual(state, current.pending.state)) {
          throw new Error('OAuth callback state does not match')
        }
        if (current.pending.redirectUri !== this.callbackServer?.callbackUrl.href) {
          throw new Error('OAuth callback redirect does not match')
        }
        const provider = new SnapshotOAuthProvider(current, current.pending.redirectUri)
        const outcome = await auth(provider, {
          serverUrl: RESOURCE_URL,
          scope: AUTH_SCOPES,
          authorizationCode: code,
          ...(callback.params.get('iss') ? { iss: callback.params.get('iss') as string } : {}),
        })
        if (outcome !== 'AUTHORIZED') throw new Error('OAuth code exchange did not authorize the client')
        const snapshot = provider.snapshot()
        delete snapshot.pending
        return snapshot
      })
      if (this.disposed || this.activeAttempt !== attempt) {
        throw new Error('OAuth authorization attempt is no longer active')
      }
      await this.connectRemote(false)
      this.activeAttempt = undefined
      callback.respondSuccess()
    } catch (error) {
      if (this.activeAttempt === attempt) this.activeAttempt = undefined
      this.setState('failed', safeFailureMessage(error))
      callback.respondFailure()
      throw error
    }
  }

  private async connectRemote(interactiveFallback: boolean): Promise<void> {
    this.setState('connecting', 'Connecting Deep Pattern Audit tools.')
    let lastToken: string | undefined
    const authProvider: AuthProvider = {
      token: async () => {
        lastToken = (await this.store.read())?.tokens?.access_token
        return lastToken
      },
      onUnauthorized: async () => this.refreshAfterUnauthorized(lastToken),
    }
    const transport = new StreamableHTTPClientTransport(new URL(RESOURCE_URL), {
      authProvider,
      requestInit: {
        headers: { [TRANSPORT_AUTH_HEADER]: TRANSPORT_AUTH_VALUE },
      },
      onInsufficientScope: 'throw',
    })
    const nextClient = new Client({ name: CLIENT_ID, version: PLUGIN_VERSION })
    try {
      await nextClient.connect(transport)
      const listed = await nextClient.listTools(undefined, { cacheMode: 'refresh' })
      if (this.disposed) throw new Error('Deep Pattern Audit plugin is no longer active')
      const definitions = this.buildDefinitions(nextClient, listed.tools)
      const nextDisposers = new Map<string, () => void>()
      try {
        for (const [name, definition] of definitions) {
          nextDisposers.set(name, this.ctx.tools.register(definition))
        }
      } catch (error) {
        for (const dispose of nextDisposers.values()) dispose()
        throw error
      }
      const previousClient = this.client
      const previousDisposers = this.remoteDisposers
      this.client = nextClient
      this.remoteDisposers = nextDisposers
      for (const dispose of previousDisposers.values()) dispose()
      if (previousClient) await previousClient.close()
      this.setState('connected', 'Deep Pattern Audit is connected and ready.')
    } catch (error) {
      await nextClient.close().catch(() => undefined)
      if (error instanceof AuthenticationRequiredError && interactiveFallback) throw error
      this.setState('disconnected', safeFailureMessage(error))
      throw error
    }
  }

  private buildDefinitions(client: Client, tools: Tool[]): Map<string, ToolDefinition> {
    const available = new Map(tools.map((tool) => [tool.name, tool]))
    const missing = AUDIT_TOOL_NAMES.filter((name) => !available.has(name))
    if (missing.length > 0) throw new Error('audit server did not expose the required tool contract')
    const definitions = new Map<string, ToolDefinition>()
    for (const rawName of AUDIT_TOOL_NAMES) {
      const tool = available.get(rawName) as Tool
      const name = publicToolName(rawName)
      const base = {
        name,
        rawName,
        description: tool.description ?? '',
        inputSchema: tool.inputSchema,
        taskRequired: tool.execution?.taskSupport === 'required',
        call: async (args: Record<string, unknown>, execution: ToolExecution) => {
          const result = await client.callTool(
            { name: rawName as AuditToolName, arguments: args },
            { signal: execution.signal, timeout: TOOL_CALL_TIMEOUT_MS, toolDefinition: tool },
          )
          const projected = rawName === 'audit_skill_submit' || rawName === 'audit_skill_status'
            ? exposeProgressLink(result)
            : result
          deferAuditContinuation(rawName, projected, execution)
          return projected
        },
      }
      const mcpDefinition = createMcpToolDefinition(
        this.ctx,
        tool.outputSchema === undefined ? base : { ...base, outputSchema: tool.outputSchema },
      )
      const definition: ToolDefinition = {
        ...mcpDefinition,
        output: {
          ...mcpDefinition.output,
          presentationMeta(_args, value) {
            return projectAuditPresentation(rawName, value)
          },
        },
      }
      definitions.set(name, definition)
    }
    return definitions
  }

  private async refreshAfterUnauthorized(staleToken: string | undefined): Promise<void> {
    let requiresAuthorization = false
    await this.store.modify(async (current) => {
      if (!current?.tokens) {
        requiresAuthorization = true
        return current ?? emptyGrantPayload()
      }
      if (staleToken && current.tokens.access_token !== staleToken) return current
      const placeholder = `http://127.0.0.1${CALLBACK_PATH}`
      current.pending = {
        state: randomBytes(32).toString('base64url'),
        redirectUri: placeholder,
        createdAt: new Date().toISOString(),
      }
      const provider = new SnapshotOAuthProvider(current, placeholder)
      const outcome = await auth(provider, { serverUrl: RESOURCE_URL, scope: AUTH_SCOPES })
      const snapshot = provider.snapshot()
      delete snapshot.pending
      requiresAuthorization = outcome !== 'AUTHORIZED'
      return snapshot
    })
    if (requiresAuthorization) throw new AuthenticationRequiredError()
  }

  private async disconnectRemote(): Promise<void> {
    for (const dispose of this.remoteDisposers.values()) dispose()
    this.remoteDisposers.clear()
    const current = this.client
    this.client = undefined
    if (current) await current.close().catch(() => undefined)
  }

  private async stopCallbackServer(): Promise<void> {
    const current = this.callbackServer
    this.callbackServer = undefined
    if (current) await current.close()
  }
}
