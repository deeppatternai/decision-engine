import { createElement as h, type CSSProperties } from 'react'

const CARD_SCHEMA = 'deep-pattern-visual-card.v1'
const TOOL_NAMES = [
  'mcp__visual__visual_submit',
  'mcp__visual__visual_status',
  'mcp__visual__visual_result',
  'mcp__visual__visual_cancel',
  'mcp__visual__visual_open_view',
] as const

type VisualWireName = (typeof TOOL_NAMES)[number]
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

export interface VisualCardModel {
  title: string
  status: string
  state: CardState
  taskId?: string
  mode?: string
  progressUrl?: string
  summary?: string
  artifactAvailable?: boolean
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function safeText(value: unknown, maximum = 160): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  return normalized ? normalized.slice(0, maximum) : undefined
}

function trustedUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' &&
      url.origin === 'https://endpoint.deeppattern.ai' &&
      url.pathname === '/plugin/visual/view/exchange'
      ? value
      : undefined
  } catch {
    return undefined
  }
}

function titleFor(toolName: string): string {
  if (toolName.endsWith('visual_submit')) return 'Deep Pattern Visual'
  if (toolName.endsWith('visual_status')) return 'Visual progress'
  if (toolName.endsWith('visual_result')) return 'Visual result'
  if (toolName.endsWith('visual_cancel')) return 'Cancel visual'
  return 'Visual viewer'
}

function stateFor(status: string | undefined, terminal: boolean | undefined): CardState {
  if (status === 'failed') return 'error'
  if (status === 'cancelled') return 'warning'
  if (status === 'completed' || terminal === true) return 'done'
  if (status === 'queued' || status === 'running' || terminal === false) return 'running'
  return 'idle'
}

export function visualCardModel(
  toolName: string,
  phase: 'preparing' | 'start' | 'result',
  block: unknown,
): VisualCardModel {
  const title = titleFor(toolName)
  if (phase === 'preparing') return { title, status: 'Preparing', state: 'preparing' }
  if (phase === 'start') return { title, status: 'Running', state: 'running' }
  if (!isRecord(block)) return { title, status: 'Response available', state: 'idle' }
  if (block.isError === true) return { title, status: 'Failed', state: 'error' }
  const meta = isRecord(block.meta) && block.meta.schema_version === CARD_SCHEMA
    ? block.meta
    : undefined
  if (!meta) return { title, status: 'Response available', state: 'idle' }
  const status = safeText(meta.status, 32)
  const terminal = typeof meta.terminal === 'boolean' ? meta.terminal : undefined
  const taskId = typeof meta.task_id === 'string' && /^vis_[0-9]+$/.test(meta.task_id)
    ? meta.task_id
    : undefined
  const mode = ['diagram', 'infographic', 'comic'].includes(String(meta.mode))
    ? String(meta.mode)
    : undefined
  const progressUrl = trustedUrl(meta.progress_url)
  const summary = safeText(meta.summary, 500)
  return {
    title: safeText(meta.title) ?? title,
    status: status ?? (terminal ? 'Completed' : 'Response available'),
    state: stateFor(status, terminal),
    ...(taskId ? { taskId } : {}),
    ...(mode ? { mode } : {}),
    ...(progressUrl ? { progressUrl } : {}),
    ...(summary ? { summary } : {}),
    ...(typeof meta.artifact_available === 'boolean'
      ? { artifactAvailable: meta.artifact_available }
      : {}),
  }
}

const styles: Record<string, CSSProperties> = {
  card: { border: '1px solid var(--dsw-alias-border-l2, rgba(127,127,127,.25))', borderRadius: 8, background: 'var(--dsw-alias-bg-base, transparent)', color: 'var(--dsw-alias-label-primary, inherit)', margin: '4px 0', maxWidth: 680, overflow: 'hidden' },
  header: { alignItems: 'center', display: 'flex', gap: 8, minHeight: 42, padding: '0 12px' },
  dot: { borderRadius: '50%', display: 'inline-block', height: 8, width: 8 },
  title: { fontSize: 13, fontWeight: 600 },
  status: { color: 'var(--dsw-alias-label-secondary, inherit)', fontSize: 12, marginLeft: 'auto', textTransform: 'capitalize' },
  body: { borderTop: '1px solid var(--dsw-alias-border-l2, rgba(127,127,127,.18))', display: 'grid', gap: 8, padding: '10px 12px 12px' },
  meta: { color: 'var(--dsw-alias-label-caption, inherit)', display: 'flex', flexWrap: 'wrap', fontSize: 11, gap: 10 },
  summary: { color: 'var(--dsw-alias-label-secondary, inherit)', fontSize: 12, lineHeight: '18px', margin: 0, whiteSpace: 'pre-wrap' },
  link: { color: 'var(--dsw-alias-brand-text)', fontSize: 12, textDecoration: 'none', width: 'fit-content' },
}

function dotColor(state: CardState): string {
  if (state === 'preparing' || state === 'running') return 'var(--dsw-alias-brand-primary)'
  if (state === 'done') return 'var(--dsw-alias-state-success-primary)'
  if (state === 'warning') return 'var(--dsw-alias-state-warn-label)'
  if (state === 'error') return 'var(--dsw-alias-state-error-primary)'
  return 'var(--dsw-alias-label-caption)'
}

function VisualCard(props: ToolCallViewProps) {
  const model = visualCardModel(props.toolName, props.phase, props.block)
  const hasBody = Boolean(model.taskId || model.mode || model.progressUrl || model.summary)
  return h('section', { style: styles.card, 'data-tool': props.toolName, 'data-state': model.state },
    h('div', { style: styles.header },
      h('span', { style: styles.title }, model.title),
      h('span', { style: { ...styles.dot, background: dotColor(model.state) }, 'aria-hidden': true }),
      h('span', { style: styles.status }, model.status)),
    hasBody ? h('div', { style: styles.body },
      h('div', { style: styles.meta },
        model.taskId ? h('span', null, `Task ${model.taskId}`) : null,
        model.mode ? h('span', null, model.mode) : null,
        model.artifactAvailable ? h('span', null, 'Artifact ready') : null),
      model.summary ? h('p', { style: styles.summary }, model.summary) : null,
      model.progressUrl ? h('a', { style: styles.link, href: model.progressUrl, target: '_blank', rel: 'noopener noreferrer' }, 'Open visual viewer') : null,
    ) : null,
  )
}

export const inject = ['slots'] as const

export function apply(ctx: ClientContext): void {
  for (const toolName of TOOL_NAMES) {
    ctx.slots.inject('tool.call.toolview', () => ctx.slots.register({
      name: 'tool.call.toolview',
      key: toolName,
    }, VisualCard))
  }
}

export const visualToolViewNames: readonly VisualWireName[] = TOOL_NAMES
