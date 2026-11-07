import assert from 'node:assert/strict'
import test from 'node:test'

import { browserCommand } from '../dist/browser.js'

const URL_VALUE = new URL('https://endpoint.deeppattern.ai/plugin/oauth/authorize?state=opaque')

test('maps browser launch without a shell on each desktop platform', () => {
  assert.deepEqual(browserCommand(URL_VALUE, 'darwin'), {
    command: '/usr/bin/open',
    args: [URL_VALUE.href],
  })
  assert.deepEqual(browserCommand(URL_VALUE, 'win32'), {
    command: 'explorer.exe',
    args: [URL_VALUE.href],
  })
  assert.deepEqual(browserCommand(URL_VALUE, 'linux'), {
    command: 'xdg-open',
    args: [URL_VALUE.href],
  })
})

test('rejects non-HTTPS browser targets and unknown platforms', () => {
  assert.throws(() => browserCommand(new URL('http://127.0.0.1/'), 'darwin'), /must use HTTPS/)
  assert.throws(() => browserCommand(URL_VALUE, 'aix'), /unsupported browser platform/)
})
