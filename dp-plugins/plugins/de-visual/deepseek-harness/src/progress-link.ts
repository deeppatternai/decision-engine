import { RESOURCE_URL } from './constants.js'

const VISUAL_ORIGIN = new URL(RESOURCE_URL).origin
const PROGRESS_PATH_PREFIX = '/plugin/visual/view/exchange'
const LINK_PREFIX = 'Open visual progress:'
const LINK_LABEL = 'Open live visual progress'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function isTrustedProgressUri(uri: string): boolean {
  try {
    const parsed = new URL(uri)
    return (
      parsed.protocol === 'https:' &&
      parsed.origin === VISUAL_ORIGIN &&
      parsed.pathname === PROGRESS_PATH_PREFIX
    )
  } catch {
    return false
  }
}

function progressLinkLine(uri: string): string {
  return `${LINK_PREFIX} [${LINK_LABEL}](${uri})`
}

function trustedProgressLink(content: unknown): { name: string; uri: string } | undefined {
  if (!Array.isArray(content)) return undefined
  for (const value of content) {
    if (!isRecord(value) || value.type !== 'resource_link') continue
    if (typeof value.name !== 'string' || !value.name || typeof value.uri !== 'string') continue
    if (isTrustedProgressUri(value.uri)) return { name: value.name, uri: value.uri }
  }
  return undefined
}

export function exposeProgressLink<T>(result: T): T {
  if (!isRecord(result) || !Array.isArray(result.content)) return result
  const link = trustedProgressLink(result.content)
  if (!link) return result
  const line = progressLinkLine(link.uri)
  if (
    result.content.some(
      (value) => isRecord(value) && value.type === 'text' && value.text === line,
    )
  ) {
    return result
  }
  return {
    ...result,
    content: [{ type: 'text', text: line }, ...result.content],
  } as T
}
