import type { VisualToolName } from './constants.js'
import { isTrustedProgressUri } from './progress-link.js'

type JsonPrimitive = boolean | number | string | null
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }

const CARD_SCHEMA = 'deep-pattern-visual-card.v1'
const TASK_ID = /^vis_[0-9]+$/
const STATUS = new Set(['queued', 'running', 'completed', 'failed', 'cancelled'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function text(value: unknown, maximum = 160): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  return normalized ? normalized.slice(0, maximum) : undefined
}

function compact<T extends Record<string, JsonValue | undefined>>(value: T): Record<string, JsonValue> {
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, JsonValue] => entry[1] !== undefined),
  )
}

function progressUrl(content: unknown): string | undefined {
  if (!Array.isArray(content)) return undefined
  for (const block of content) {
    if (
      isRecord(block) &&
      block.type === 'resource_link' &&
      typeof block.uri === 'string' &&
      isTrustedProgressUri(block.uri)
    ) return block.uri
  }
  return undefined
}

function summary(content: unknown): string | undefined {
  if (!Array.isArray(content)) return undefined
  const value = content
    .filter((block) => isRecord(block) && block.type === 'text' && typeof block.text === 'string')
    .map((block) => (block as { text: string }).text)
    .join('\n')
  return text(value, 500)
}

export function projectVisualPresentation(rawName: VisualToolName, value: unknown): JsonValue {
  const root = isRecord(value) ? value : undefined
  const payload = root && isRecord(root.structuredContent) ? root.structuredContent : undefined
  const taskId = payload?.task_id
  const status = typeof payload?.status === 'string' && STATUS.has(payload.status)
    ? payload.status
    : undefined
  return compact({
    schema_version: CARD_SCHEMA,
    tool: rawName,
    task_id: typeof taskId === 'string' && TASK_ID.test(taskId) ? taskId : undefined,
    title: text(payload?.title),
    mode: ['diagram', 'infographic', 'comic'].includes(String(payload?.mode))
      ? String(payload?.mode)
      : undefined,
    status,
    terminal: typeof payload?.terminal === 'boolean' ? payload.terminal : undefined,
    artifact_available: typeof payload?.artifact_available === 'boolean'
      ? payload.artifact_available
      : undefined,
    progress_url: progressUrl(root?.content),
    summary: rawName === 'visual_result' ? summary(root?.content) : undefined,
  })
}
