export interface BrowserCommand {
    command: string;
    args: string[];
}
export declare function browserCommand(url: URL, platform?: NodeJS.Platform): BrowserCommand;
export declare function openBrowser(url: URL): Promise<void>;
