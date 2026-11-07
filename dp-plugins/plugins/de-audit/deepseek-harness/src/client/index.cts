import { createElement as h, useState, type CSSProperties, type KeyboardEvent } from 'react'

const CARD_SCHEMA = 'deep-pattern-audit-card.v1'
const TASK_ID = /^[A-Za-z0-9._:-]{1,128}$/
const TOOL_NAMES = [
  'mcp__audit__audit_skill_submit',
  'mcp__audit__audit_skill_status',
  'mcp__audit__audit_skill_result',
  'mcp__audit__audit_skill_events',
] as const

type AuditWireName = (typeof TOOL_NAMES)[number]
type CardState = 'preparing' | 'running' | 'done' | 'warning' | 'error' | 'idle'

interface ToolCallViewProps {
  toolName: string
  phase: 'preparing' | 'start' | 'result'
  block: unknown
}

interface ClientContext {
  slots: {
    inject(name: 'tool.call.toolview', callback: () => unknown): unknown
    register(
      options: { name: 'tool.call.toolview'; key: string },
      component: (props: ToolCallViewProps) => unknown,
    ): unknown
  }
}

export interface AuditCardAuditor {
  label: string
  model?: string
  status?: string
  phase?: string
}

export interface AuditCardModel {
  title: string
  status: string
  state: CardState
  taskId?: string
  phase?: string
  elapsedMs?: number
  tier?: string
  progressUrl?: string
  summary?: string
  auditors: AuditCardAuditor[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function text(value: unknown, maximum = 120): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  return normalized ? normalized.slice(0, maximum) : undefined
}

function number(value: unknown, maximum: number): number | undefined {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= maximum
    ? value as number
    : undefined
}

function progressUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' &&
      url.origin === 'https://endpoint.deeppattern.ai' &&
      url.pathname.startsWith('/plugin/audit/')
      ? value
      : undefined
  } catch {
    return undefined
  }
}

function titleFor(toolName: string): string {
  if (toolName.endsWith('audit_skill_submit')) return 'Deep Pattern Audit'
  if (toolName.endsWith('audit_skill_status')) return 'Audit progress'
  if (toolName.endsWith('audit_skill_result')) return 'Audit result'
  return 'Audit events'
}

function stateFor(status: string | undefined, terminal: boolean | undefined): CardState {
  if (status === 'failed' || status === 'connection_timeout' || status === 'timeout') return 'error'
  if (status === 'cancelled' || status === 'partial') return 'warning'
  if (status === 'completed') return 'done'
  if (terminal === true) return 'done'
  if (status === 'queued' || status === 'running' || terminal === false) return 'running'
  return 'idle'
}

function auditors(value: unknown): AuditCardAuditor[] {
  if (!Array.isArray(value)) return []
  const result: AuditCardAuditor[] = []
  for (const [index, raw] of value.slice(0, 8).entries()) {
    if (!isRecord(raw)) continue
    const label = text(raw.label) ?? `Reviewer ${index + 1}`
    const model = text(raw.model)
    const status = text(raw.status, 32)
    const phase = text(raw.phase, 32)
    result.push({
      label,
      ...(model ? { model } : {}),
      ...(status ? { status } : {}),
      ...(phase ? { phase } : {}),
    })
  }
  return result
}

export function auditCardModel(
  toolName: string,
  phase: 'preparing' | 'start' | 'result',
  block: unknown,
): AuditCardModel {
  const title = titleFor(toolName)
  if (phase === 'preparing') {
    return { title, status: 'Preparing', state: 'preparing', auditors: [] }
  }
  if (phase === 'start') {
    return { title, status: 'Running', state: 'running', auditors: [] }
  }
  if (!isRecord(block)) return { title, status: 'Response available', state: 'idle', auditors: [] }
  if (block.isError === true) {
    const interrupted = isRecord(block.error) && block.error.code === 'interrupted'
    return {
      title,
      status: interrupted ? 'Stopped' : 'Failed',
      state: interrupted ? 'warning' : 'error',
      auditors: [],
    }
  }
  const meta = isRecord(block.meta) && block.meta.schema_version === CARD_SCHEMA ? block.meta : undefined
  if (!meta) {
    return {
      title,
      status: toolName.endsWith('audit_skill_result') ? 'Completed' : 'Response available',
      state: toolName.endsWith('audit_skill_result') ? 'done' : 'idle',
      auditors: [],
    }
  }
  const progress = isRecord(meta.progress) ? meta.progress : undefined
  const status = text(meta.status, 32)
  const terminal = typeof progress?.terminal === 'boolean' ? progress.terminal : undefined
  const taskId = typeof meta.task_id === 'string' && TASK_ID.test(meta.task_id) ? meta.task_id : undefined
  const trustedProgressUrl = progressUrl(meta.progress_url)
  const progressPhase = text(progress?.phase, 32)
  const elapsedMs = number(progress?.elapsed_ms, 7 * 24 * 60 * 60 * 1000)
  const tier = text(meta.tier, 32)
  const summary = text(meta.summary, 500)
  return {
    title,
    status: status ?? (terminal === true ? 'Completed' : 'Response available'),
    state: stateFor(status, terminal),
    auditors: auditors(progress?.auditors),
    ...(taskId ? { taskId } : {}),
    ...(progressPhase ? { phase: progressPhase } : {}),
    ...(elapsedMs !== undefined ? { elapsedMs } : {}),
    ...(tier ? { tier } : {}),
    ...(trustedProgressUrl ? { progressUrl: trustedProgressUrl } : {}),
    ...(summary ? { summary } : {}),
  }
}

const styles: Record<string, CSSProperties> = {
  card: {
    border: '1px solid var(--dsw-alias-border-l2, rgba(127,127,127,.25))',
    borderRadius: 8,
    background: 'var(--dsw-alias-bg-base, transparent)',
    color: 'var(--dsw-alias-label-primary, inherit)',
    margin: '4px 0',
    maxWidth: 680,
    overflow: 'hidden',
  },
  header: { alignItems: 'center', display: 'flex', gap: 8, minHeight: 38, padding: '0 12px' },
  icon: { color: 'var(--dsw-alias-label-secondary, currentColor)', display: 'inline-flex', flex: 'none' },
  title: { fontSize: 13, fontWeight: 600, lineHeight: '20px' },
  status: {
    color: 'var(--dsw-alias-label-secondary, inherit)',
    fontSize: 12,
    lineHeight: '18px',
    marginLeft: 'auto',
    textTransform: 'capitalize',
  },
  body: {
    borderTop: '1px solid var(--dsw-alias-border-l2, rgba(127,127,127,.18))',
    display: 'grid',
    gap: 8,
    padding: '10px 12px 12px',
  },
  meta: { color: 'var(--dsw-alias-label-caption, inherit)', display: 'flex', flexWrap: 'wrap', fontSize: 11, gap: 10 },
  task: { fontFamily: 'var(--dsw-font-markdown-code-block-small, monospace)', overflowWrap: 'anywhere' },
  auditors: { display: 'grid', gap: 5 },
  auditor: { alignItems: 'center', display: 'grid', fontSize: 12, gap: 8, gridTemplateColumns: '14px minmax(80px, 1fr) minmax(60px, auto)' },
  auditorLabel: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  auditorState: { color: 'var(--dsw-alias-label-caption, inherit)', textAlign: 'right' },
  summary: { color: 'var(--dsw-alias-label-secondary, inherit)', fontSize: 12, lineHeight: '18px', margin: 0, whiteSpace: 'pre-wrap' },
  link: {
    alignItems: 'center',
    color: 'var(--dsw-alias-brand-text)',
    display: 'inline-flex',
    fontSize: 12,
    textDecoration: 'none',
    width: 'fit-content',
  },
  dot: {
    borderRadius: '50%',
    display: 'inline-block',
    flex: 'none',
    height: 8,
    width: 8,
  },
}

function dotColor(state: CardState): string {
  if (state === 'preparing' || state === 'running') return 'var(--dsw-alias-brand-primary)'
  if (state === 'done') return 'var(--dsw-alias-state-success-primary)'
  if (state === 'warning') return 'var(--dsw-alias-state-warn-label)'
  if (state === 'error') return 'var(--dsw-alias-state-error-primary)'
  return 'var(--dsw-alias-label-caption)'
}

function StateMarker({ state, size = 8 }: { state: CardState; size?: number }) {
  return h('span', {
    'aria-hidden': true,
    style: { ...styles.dot, background: dotColor(state), height: size, width: size },
  })
}

function elapsedText(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${seconds % 60}s`
}

function auditorState(auditor: AuditCardAuditor): CardState {
  return stateFor(auditor.status, auditor.status === 'completed' ? true : undefined)
}

function AuditCard(props: ToolCallViewProps) {
  const model = auditCardModel(props.toolName, props.phase, props.block)
  const [expanded, setExpanded] = useState(true)
  const running = model.state === 'preparing' || model.state === 'running'
  const hasBody = Boolean(model.taskId || model.phase || model.tier || model.elapsedMs !== undefined || model.progressUrl || model.summary || model.auditors.length)
  const toggle = () => hasBody && setExpanded((value) => !value)
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!hasBody || (event.key !== 'Enter' && event.key !== ' ')) return
    event.preventDefault()
    toggle()
  }

  return h('section', { style: styles.card, 'data-tool': props.toolName, 'data-state': model.state },
    h('div', {
      style: { ...styles.header, cursor: hasBody ? 'pointer' : 'default' },
      role: hasBody ? 'button' : undefined,
      tabIndex: hasBody ? 0 : undefined,
      'aria-expanded': hasBody ? expanded : undefined,
      onClick: toggle,
      onKeyDown,
    },
    h('span', { style: styles.title, 'aria-live': running ? 'polite' : undefined }, model.title),
    h(StateMarker, { state: model.state, size: 8 }),
    h('span', { style: styles.status }, model.status)),
    hasBody && expanded ? h('div', { style: styles.body },
      h('div', { style: styles.meta },
        model.taskId ? h('span', { style: styles.task }, `Task ${model.taskId}`) : null,
        model.tier ? h('span', null, `Tier ${model.tier}`) : null,
        model.phase ? h('span', null, model.phase) : null,
        model.elapsedMs !== undefined ? h('span', null, elapsedText(model.elapsedMs)) : null,
      ),
      model.auditors.length ? h('div', { style: styles.auditors },
        ...model.auditors.map((auditor, index) => h('div', { style: styles.auditor, key: `${auditor.label}-${index}` },
          h(StateMarker, { state: auditorState(auditor), size: 7 }),
          h('span', { style: styles.auditorLabel }, auditor.model ? `${auditor.label} · ${auditor.model}` : auditor.label),
          h('span', { style: styles.auditorState }, auditor.phase ?? auditor.status ?? 'pending'),
        )),
      ) : null,
      model.summary ? h('p', { style: styles.summary }, model.summary) : null,
      model.progressUrl ? h('a', {
        style: styles.link,
        href: model.progressUrl,
        target: '_blank',
        rel: 'noopener noreferrer',
        onClick: (event: { stopPropagation(): void }) => event.stopPropagation(),
      }, 'Open live audit progress') : null,
    ) : null,
  )
}

export const inject = ['slots'] as const

export function apply(ctx: ClientContext): void {
  for (const toolName of TOOL_NAMES) {
    ctx.slots.inject('tool.call.toolview', () => ctx.slots.register({
      name: 'tool.call.toolview',
      key: toolName,
    }, AuditCard))
  }
}

export const auditToolViewNames: readonly AuditWireName[] = TOOL_NAMES
