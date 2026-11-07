import type { Context } from '@deepseek-ai/cordis';
export declare const name = "deep-pattern-audit";
export declare const inject: string[];
export declare function apply(ctx: Context): Promise<() => Promise<void>>;
