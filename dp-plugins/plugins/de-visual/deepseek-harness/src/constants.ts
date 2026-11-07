export const PLUGIN_NAME = 'deep-pattern-visual'
export const PLUGIN_VERSION = '0.1.0-remote.2'
export const SERVER_NAME = 'visual'
export const CLIENT_ID = 'deep-pattern-visual-harness'
export const RESOURCE_URL = 'https://endpoint.deeppattern.ai/plugin/mcp/v1/visual'
export const CALLBACK_HOST = '127.0.0.1'
export const CALLBACK_PATH = '/deeppattern/oauth/callback'
export const AUTH_SCOPES = 'visual:submit visual:read visual:cancel'
export const CREDENTIAL_RECORD_ID = 'oauth'
export const TRANSPORT_AUTH_HEADER = 'X-DeepPattern-MCP-Auth-Mode'
export const TRANSPORT_AUTH_VALUE = 'transport'
export const CALLBACK_TIMEOUT_MS = 5 * 60 * 1000
export const STATUS_WAIT_MAX_MS = 20 * 1000
export const TOOL_CALL_TIMEOUT_MS = 60 * 1000

export const VISUAL_TOOL_NAMES = [
  'visual_submit',
  'visual_status',
  'visual_result',
  'visual_cancel',
  'visual_open_view',
] as const

export type VisualToolName = (typeof VISUAL_TOOL_NAMES)[number]
