import { RESOURCE_URL } from './constants.js';
const AUDIT_ORIGIN = new URL(RESOURCE_URL).origin;
const PROGRESS_PATH_PREFIX = '/plugin/audit/';
const LINK_PREFIX = 'Open audit progress:';
const LINK_LABEL = 'Open live audit progress';
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
export function isTrustedProgressUri(uri) {
    try {
        const parsed = new URL(uri);
        return (parsed.protocol === 'https:' &&
            parsed.origin === AUDIT_ORIGIN &&
            parsed.pathname.startsWith(PROGRESS_PATH_PREFIX));
    }
    catch {
        return false;
    }
}
function progressLinkLine(uri) {
    return `${LINK_PREFIX} [${LINK_LABEL}](${uri})`;
}
function trustedProgressLink(content) {
    if (!Array.isArray(content))
        return undefined;
    for (const value of content) {
        if (!isRecord(value) || value.type !== 'resource_link')
            continue;
        if (typeof value.name !== 'string' || !value.name || typeof value.uri !== 'string')
            continue;
        if (isTrustedProgressUri(value.uri))
            return { name: value.name, uri: value.uri };
    }
    return undefined;
}
export function exposeProgressLink(result) {
    if (!isRecord(result) || !Array.isArray(result.content))
        return result;
    const link = trustedProgressLink(result.content);
    if (!link)
        return result;
    const line = progressLinkLine(link.uri);
    if (result.content.some((value) => isRecord(value) && value.type === 'text' && value.text === line)) {
        return result;
    }
    return {
        ...result,
        content: [{ type: 'text', text: line }, ...result.content],
    };
}
