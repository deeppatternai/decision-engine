import assert from 'node:assert/strict'
import test from 'node:test'

import { OAuthCallbackServer } from '../dist/callback-server.js'

test('accepts only the exact loopback callback path', async () => {
  let observed
  const server = new OAuthCallbackServer(async (result) => {
    observed = Object.fromEntries(result.params)
    result.respondSuccess()
  }, () => assert.fail('unexpected timeout'))
  const callback = await server.start(2_000)
  assert.equal(callback.hostname, '127.0.0.1')
  assert.equal(callback.pathname, '/deeppattern/oauth/callback')

  const wrong = new URL('/wrong', callback)
  assert.equal((await fetch(wrong)).status, 404)
  assert.equal((await fetch(callback, { method: 'POST' })).status, 404)
  const response = await fetch(`${callback.href}?state=opaque&code=authorization-code`)
  assert.equal(response.status, 200)
  assert.deepEqual(observed, { state: 'opaque', code: 'authorization-code' })
  assert.match(await response.text(), /Authorization completed/)
})

test('times out and closes without invoking a callback', async () => {
  let timedOut = false
  const server = new OAuthCallbackServer(
    async () => assert.fail('unexpected callback'),
    () => {
      timedOut = true
    },
  )
  await server.start(20)
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(timedOut, true)
  await server.close()
})

test('coalesces identical concurrent callbacks without accepting a changed replay', async () => {
  let release
  let callbackStarted
  const started = new Promise((resolve) => {
    callbackStarted = resolve
  })
  const blocked = new Promise((resolve) => {
    release = resolve
  })
  let callbackCount = 0
  const server = new OAuthCallbackServer(async (result) => {
    callbackCount += 1
    callbackStarted()
    await blocked
    result.respondSuccess()
  }, () => assert.fail('unexpected timeout'))
  const callback = await server.start(2_000)
  const callbackUrl = `${callback.href}?state=opaque&code=authorization-code`

  const first = fetch(callbackUrl)
  await started
  const duplicate = fetch(callbackUrl)
  const changed = await fetch(`${callback.href}?state=changed&code=authorization-code`)
  assert.equal(changed.status, 404)
  release()

  const [firstResponse, duplicateResponse] = await Promise.all([first, duplicate])
  assert.equal(firstResponse.status, 200)
  assert.equal(duplicateResponse.status, 200)
  assert.match(await firstResponse.text(), /Authorization completed/)
  assert.match(await duplicateResponse.text(), /Authorization completed/)
  assert.equal(callbackCount, 1)
})
