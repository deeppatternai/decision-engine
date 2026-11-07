window.__ModuleLoader__.load({
  id: "@deeppattern/de-audit-harness",
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    "use strict";
    Object.defineProperty(exports, "__esModule", { value: true });
    exports.auditToolViewNames = exports.inject = void 0;
    exports.auditCardModel = auditCardModel;
    exports.apply = apply;
    const react_1 = require("react");
    const CARD_SCHEMA = 'deep-pattern-audit-card.v1';
    const TASK_ID = /^[A-Za-z0-9._:-]{1,128}$/;
    const TOOL_NAMES = [
        'mcp__audit__audit_skill_submit',
        'mcp__audit__audit_skill_status',
        'mcp__audit__audit_skill_result',
        'mcp__audit__audit_skill_events',
    ];
    function isRecord(value) {
        return typeof value === 'object' && value !== null && !Array.isArray(value);
    }
    function text(value, maximum = 120) {
        if (typeof value !== 'string')
            return undefined;
        const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
        return normalized ? normalized.slice(0, maximum) : undefined;
    }
    function number(value, maximum) {
        return Number.isSafeInteger(value) && value >= 0 && value <= maximum
            ? value
            : undefined;
    }
    function progressUrl(value) {
        if (typeof value !== 'string')
            return undefined;
        try {
            const url = new URL(value);
            return url.protocol === 'https:' &&
                url.origin === 'https://endpoint.deeppattern.ai' &&
                url.pathname.startsWith('/plugin/audit/')
                ? value
                : undefined;
        }
        catch {
            return undefined;
        }
    }
    function titleFor(toolName) {
        if (toolName.endsWith('audit_skill_submit'))
            return 'Deep Pattern Audit';
        if (toolName.endsWith('audit_skill_status'))
            return 'Audit progress';
        if (toolName.endsWith('audit_skill_result'))
            return 'Audit result';
        return 'Audit events';
    }
    function stateFor(status, terminal) {
        if (status === 'failed' || status === 'connection_timeout' || status === 'timeout')
            return 'error';
        if (status === 'cancelled' || status === 'partial')
            return 'warning';
        if (status === 'completed')
            return 'done';
        if (terminal === true)
            return 'done';
        if (status === 'queued' || status === 'running' || terminal === false)
            return 'running';
        return 'idle';
    }
    function auditors(value) {
        if (!Array.isArray(value))
            return [];
        const result = [];
        for (const [index, raw] of value.slice(0, 8).entries()) {
            if (!isRecord(raw))
                continue;
            const label = text(raw.label) ?? `Reviewer ${index + 1}`;
            const model = text(raw.model);
            const status = text(raw.status, 32);
            const phase = text(raw.phase, 32);
            result.push({
                label,
                ...(model ? { model } : {}),
                ...(status ? { status } : {}),
                ...(phase ? { phase } : {}),
            });
        }
        return result;
    }
    function auditCardModel(toolName, phase, block) {
        const title = titleFor(toolName);
        if (phase === 'preparing') {
            return { title, status: 'Preparing', state: 'preparing', auditors: [] };
        }
        if (phase === 'start') {
            return { title, status: 'Running', state: 'running', auditors: [] };
        }
        if (!isRecord(block))
            return { title, status: 'Response available', state: 'idle', auditors: [] };
        if (block.isError === true) {
            const interrupted = isRecord(block.error) && block.error.code === 'interrupted';
            return {
                title,
                status: interrupted ? 'Stopped' : 'Failed',
                state: interrupted ? 'warning' : 'error',
                auditors: [],
            };
        }
        const meta = isRecord(block.meta) && block.meta.schema_version === CARD_SCHEMA ? block.meta : undefined;
        if (!meta) {
            return {
                title,
                status: toolName.endsWith('audit_skill_result') ? 'Completed' : 'Response available',
                state: toolName.endsWith('audit_skill_result') ? 'done' : 'idle',
                auditors: [],
            };
        }
        const progress = isRecord(meta.progress) ? meta.progress : undefined;
        const status = text(meta.status, 32);
        const terminal = typeof progress?.terminal === 'boolean' ? progress.terminal : undefined;
        const taskId = typeof meta.task_id === 'string' && TASK_ID.test(meta.task_id) ? meta.task_id : undefined;
        const trustedProgressUrl = progressUrl(meta.progress_url);
        const progressPhase = text(progress?.phase, 32);
        const elapsedMs = number(progress?.elapsed_ms, 7 * 24 * 60 * 60 * 1000);
        const tier = text(meta.tier, 32);
        const summary = text(meta.summary, 500);
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
        };
    }
    const styles = {
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
    };
    function dotColor(state) {
        if (state === 'preparing' || state === 'running')
            return 'var(--dsw-alias-brand-primary)';
        if (state === 'done')
            return 'var(--dsw-alias-state-success-primary)';
        if (state === 'warning')
            return 'var(--dsw-alias-state-warn-label)';
        if (state === 'error')
            return 'var(--dsw-alias-state-error-primary)';
        return 'var(--dsw-alias-label-caption)';
    }
    function StateMarker({ state, size = 8 }) {
        return (0, react_1.createElement)('span', {
            'aria-hidden': true,
            style: { ...styles.dot, background: dotColor(state), height: size, width: size },
        });
    }
    function elapsedText(milliseconds) {
        const seconds = Math.floor(milliseconds / 1000);
        if (seconds < 60)
            return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        return `${minutes}m ${seconds % 60}s`;
    }
    function auditorState(auditor) {
        return stateFor(auditor.status, auditor.status === 'completed' ? true : undefined);
    }
    function AuditCard(props) {
        const model = auditCardModel(props.toolName, props.phase, props.block);
        const [expanded, setExpanded] = (0, react_1.useState)(true);
        const running = model.state === 'preparing' || model.state === 'running';
        const hasBody = Boolean(model.taskId || model.phase || model.tier || model.elapsedMs !== undefined || model.progressUrl || model.summary || model.auditors.length);
        const toggle = () => hasBody && setExpanded((value) => !value);
        const onKeyDown = (event) => {
            if (!hasBody || (event.key !== 'Enter' && event.key !== ' '))
                return;
            event.preventDefault();
            toggle();
        };
        return (0, react_1.createElement)('section', { style: styles.card, 'data-tool': props.toolName, 'data-state': model.state }, (0, react_1.createElement)('div', {
            style: { ...styles.header, cursor: hasBody ? 'pointer' : 'default' },
            role: hasBody ? 'button' : undefined,
            tabIndex: hasBody ? 0 : undefined,
            'aria-expanded': hasBody ? expanded : undefined,
            onClick: toggle,
            onKeyDown,
        }, (0, react_1.createElement)('span', { style: styles.title, 'aria-live': running ? 'polite' : undefined }, model.title), (0, react_1.createElement)(StateMarker, { state: model.state, size: 8 }), (0, react_1.createElement)('span', { style: styles.status }, model.status)), hasBody && expanded ? (0, react_1.createElement)('div', { style: styles.body }, (0, react_1.createElement)('div', { style: styles.meta }, model.taskId ? (0, react_1.createElement)('span', { style: styles.task }, `Task ${model.taskId}`) : null, model.tier ? (0, react_1.createElement)('span', null, `Tier ${model.tier}`) : null, model.phase ? (0, react_1.createElement)('span', null, model.phase) : null, model.elapsedMs !== undefined ? (0, react_1.createElement)('span', null, elapsedText(model.elapsedMs)) : null), model.auditors.length ? (0, react_1.createElement)('div', { style: styles.auditors }, ...model.auditors.map((auditor, index) => (0, react_1.createElement)('div', { style: styles.auditor, key: `${auditor.label}-${index}` }, (0, react_1.createElement)(StateMarker, { state: auditorState(auditor), size: 7 }), (0, react_1.createElement)('span', { style: styles.auditorLabel }, auditor.model ? `${auditor.label} · ${auditor.model}` : auditor.label), (0, react_1.createElement)('span', { style: styles.auditorState }, auditor.phase ?? auditor.status ?? 'pending')))) : null, model.summary ? (0, react_1.createElement)('p', { style: styles.summary }, model.summary) : null, model.progressUrl ? (0, react_1.createElement)('a', {
            style: styles.link,
            href: model.progressUrl,
            target: '_blank',
            rel: 'noopener noreferrer',
            onClick: (event) => event.stopPropagation(),
        }, 'Open live audit progress') : null) : null);
    }
    exports.inject = ['slots'];
    function apply(ctx) {
        for (const toolName of TOOL_NAMES) {
            ctx.slots.inject('tool.call.toolview', () => ctx.slots.register({
                name: 'tool.call.toolview',
                key: toolName,
            }, AuditCard));
        }
    }
    exports.auditToolViewNames = TOOL_NAMES;

    return module.exports;
  },
});
