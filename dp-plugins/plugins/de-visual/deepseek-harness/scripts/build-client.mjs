import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const buildRoot = resolve(root, '.client-build')
const source = await readFile(resolve(buildRoot, 'index.cjs'), 'utf8')
const declaration = await readFile(resolve(buildRoot, 'index.d.cts'), 'utf8')
const outputRoot = resolve(root, 'dist')

const bundle = [
  'window.__ModuleLoader__.load({',
  '  id: "@deeppattern/de-visual-harness",',
  '  factory: (require) => {',
  '    const module = { exports: {} };',
  '    const exports = module.exports;',
  source.split('\n').map((line) => line ? `    ${line}` : '').join('\n'),
  '    return module.exports;',
  '  },',
  '});',
  '',
].join('\n')

await mkdir(outputRoot, { recursive: true })
await writeFile(resolve(outputRoot, 'client.js'), bundle, 'utf8')
await writeFile(resolve(outputRoot, 'client.d.ts'), declaration, 'utf8')
await rm(buildRoot, { recursive: true, force: true })
