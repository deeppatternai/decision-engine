import type { VisualToolName } from './constants.js';
type JsonPrimitive = boolean | number | string | null;
type JsonValue = JsonPrimitive | JsonValue[] | {
    [key: string]: JsonValue;
};
export declare function projectVisualPresentation(rawName: VisualToolName, value: unknown): JsonValue;
export {};
