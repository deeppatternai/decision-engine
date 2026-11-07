import type { AuditToolName } from './constants.js'
import { isTrustedProgressUri } from './progress-link.js'

type JsonPrimitive = boolean | number | string | null
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }

const CARD_SCHEMA = 'deep-pattern-audit-card.v1'
const PROGRESS_SCHEMA = 'audit-progress.v1'
const TASK_ID = /^[A-Za-z0-9._:-]{1,128}$/
const STATUS = /^[a-z][a-z0-9_-]{0,31}$/
const SAFE_TEXT = 120
const SUMMARY_TEXT = 500
const MAX_AUDITORS = 8

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function boundedText(value: unknown, maximum = SAFE_TEXT): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  if (!normalized) return undefined
  return normalized.slice(0, maximum)
}

function safeStatus(value: unknown): string | undefined {
  return typeof value === 'string' && STATUS.test(value) ? value : undefined
}

function safeInteger(value: unknown, maximum: number): number | undefined {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= maximum
    ? value as number
    : undefined
}

function compact<T extends Record<string, JsonValue | undefined>>(value: T): Record<string, JsonValue> {
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, JsonValue] => entry[1] !== undefined),
  )
}

function projectedAuditors(value: unknown): JsonValue[] | undefined {
  if (!Array.isArray(value)) return undefined
  const auditors: JsonValue[] = []
  for (const raw of value.slice(0, MAX_AUDITORS)) {
    if (!isRecord(raw)) continue
    const auditor = compact({
      label: boundedText(raw.label),
      model: boundedText(raw.model),
      status: safeStatus(raw.status),
      phase: safeStatus(raw.phase),
    })
    if (Object.keys(auditor).length > 0) auditors.push(auditor)
  }
  return auditors.length > 0 ? auditors : undefined
}

function trustedProgressUrl(content: unknown): string | undefined {
  if (!Array.isArray(content)) return undefined
  for (const block of content) {
    if (
      isRecord(block) &&
      block.type === 'resource_link' &&
      typeof block.uri === 'string' &&
      isTrustedProgressUri(block.uri)
    ) {
      return block.uri
    }
  }
  return undefined
}

function terminalSummary(content: unknown): string | undefined {
  if (!Array.isArray(content)) return undefined
  const text = content
    .filter((block) => isRecord(block) && block.type === 'text' && typeof block.text === 'string')
    .map((block) => (block as { text: string }).text)
    .join('\n')
  return boundedText(text, SUMMARY_TEXT)
}

export function projectAuditPresentation(rawName: AuditToolName, value: unknown): JsonValue {
  const root = isRecord(value) ? value : undefined
  const payload = root && isRecord(root.structuredContent) ? root.structuredContent : undefined
  const progress = payload && isRecord(payload.progress) ? payload.progress : undefined
  const taskId = payload?.task_id
  const status = safeStatus(payload?.status)
  const terminal = typeof progress?.terminal === 'boolean' ? progress.terminal : undefined
  const resultLike = rawName === 'audit_skill_result'
  const projectedProgress = progress
    ? compact({
        schema_version: progress.schema_version === PROGRESS_SCHEMA ? PROGRESS_SCHEMA : undefined,
        phase: safeStatus(progress.phase),
        terminal,
        elapsed_ms: safeInteger(progress.elapsed_ms, 7 * 24 * 60 * 60 * 1000),
        poll_after_ms: safeInteger(progress.poll_after_ms, 60 * 1000),
        auditors: projectedAuditors(progress.auditors),
      })
    : undefined

  return compact({
    schema_version: CARD_SCHEMA,
    tool: rawName,
    task_id: typeof taskId === 'string' && TASK_ID.test(taskId) ? taskId : undefined,
    status,
    tier: boundedText(payload?.tier ?? progress?.tier, 32),
    progress: projectedProgress && Object.keys(projectedProgress).length > 0
      ? projectedProgress
      : undefined,
    progress_url: trustedProgressUrl(root?.content),
    summary: resultLike ? terminalSummary(root?.content) : undefined,
  })
}
