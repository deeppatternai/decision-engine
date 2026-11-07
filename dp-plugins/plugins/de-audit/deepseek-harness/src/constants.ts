export const PLUGIN_NAME = 'deep-pattern-audit'
export const PLUGIN_VERSION = '0.2.13-remote.11'
export const SERVER_NAME = 'audit'
export const CLIENT_ID = 'deep-pattern-audit-harness'
export const RESOURCE_URL = 'https://endpoint.deeppattern.ai/plugin/mcp/v1/audit'
export const CALLBACK_HOST = '127.0.0.1'
export const CALLBACK_PATH = '/deeppattern/oauth/callback'
export const AUTH_SCOPES = 'audit:submit audit:read audit:events'
export const CREDENTIAL_RECORD_ID = 'oauth'
export const TRANSPORT_AUTH_HEADER = 'X-DeepPattern-MCP-Auth-Mode'
export const TRANSPORT_AUTH_VALUE = 'transport'
export const CALLBACK_TIMEOUT_MS = 5 * 60 * 1000
export const STATUS_WAIT_MAX_MS = 20 * 1000
export const TOOL_CALL_TIMEOUT_MS = 60 * 1000

export const AUDIT_TOOL_NAMES = [
  'audit_skill_submit',
  'audit_skill_status',
  'audit_skill_result',
  'audit_skill_events',
] as const

export type AuditToolName = (typeof AUDIT_TOOL_NAMES)[number]
