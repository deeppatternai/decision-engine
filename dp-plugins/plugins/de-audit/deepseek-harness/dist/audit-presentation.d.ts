import type { AuditToolName } from './constants.js';
type JsonPrimitive = boolean | number | string | null;
type JsonValue = JsonPrimitive | JsonValue[] | {
    [key: string]: JsonValue;
};
export declare function projectAuditPresentation(rawName: AuditToolName, value: unknown): JsonValue;
export {};
