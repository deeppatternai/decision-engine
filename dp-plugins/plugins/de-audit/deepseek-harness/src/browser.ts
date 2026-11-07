import { spawn } from 'node:child_process'

export interface BrowserCommand {
  command: string
  args: string[]
}

export function browserCommand(url: URL, platform = process.platform): BrowserCommand {
  if (url.protocol !== 'https:') {
    throw new Error('authorization URL must use HTTPS')
  }
  if (platform === 'darwin') {
    return { command: '/usr/bin/open', args: [url.href] }
  }
  if (platform === 'win32') {
    return { command: 'explorer.exe', args: [url.href] }
  }
  if (platform === 'linux') {
    return { command: 'xdg-open', args: [url.href] }
  }
  throw new Error(`unsupported browser platform: ${platform}`)
}

export async function openBrowser(url: URL): Promise<void> {
  const { command, args } = browserCommand(url)
  await new Promise<void>((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
      shell: false,
    })
    child.once('error', reject)
    child.once('spawn', () => {
      child.unref()
      resolve()
    })
  })
}
