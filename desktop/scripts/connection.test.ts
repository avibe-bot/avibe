import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'

import { connect } from '../src/connection.ts'

type ConnectOptions = Parameters<typeof connect>[0]

/** The page's fixed "no answer" limit (src/connection.ts). */
const STUCK_AFTER_MS = 60_000

/** The test's clock: Node's mocked timers, advanced in small steps so that the
 *  promises a timer releases settle before the next one fires. */
function clock() {
  let now = 0
  return {
    now: () => now,
    async advance(ms: number, step = 50) {
      const end = now + ms
      while (now < end) {
        await flush()
        const by = Math.min(step, end - now)
        now += by
        mock.timers.tick(by)
      }
      await flush()
    },
  }
}

async function flush() {
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve()
  }
}

beforeEach(() => {
  mock.timers.enable({ apis: ['setTimeout'] })
})
afterEach(() => {
  mock.timers.reset()
})

const never = () => new Promise<never>(() => {})
const reject = (what: string) => async () => {
  throw new Error(what)
}

function harness(overrides: Partial<ConnectOptions>) {
  const time = clock()
  const calls = { stuck: [] as number[], recovered: [] as number[] }
  let heard = false
  const options: ConnectOptions = {
    subscribe: async () => {},
    snapshot: async () => {},
    heardFromApp: () => heard,
    onStuck: () => calls.stuck.push(time.now()),
    onRecovered: () => calls.recovered.push(time.now()),
    ...overrides,
  }
  let finished = false
  const run = connect(options).then(() => {
    finished = true
  })
  return {
    time,
    calls,
    options,
    run,
    finished: () => finished,
    hear: () => {
      heard = true
    },
  }
}

const quietly = async (body: () => Promise<void>) => {
  const original = console.error
  console.error = () => {}
  try {
    await body()
  } finally {
    console.error = original
  }
}

test('a start that answers at once never reports a stuck app', async () => {
  const h = harness({})
  await h.time.advance(STUCK_AFTER_MS * 2)
  assert.ok(h.finished())
  assert.deepEqual(h.calls.stuck, [])
})

test('failures on a slow start are retried for as long as the app keeps answering', () =>
  quietly(async () => {
    // The subscription fails for 50 s, then the status read fails for another 50 s.
    const at = { now: () => 0 }
    const h = harness({
      subscribe: async () => {
        if (at.now() < 50_000) throw new Error('listen rejected')
      },
      snapshot: async () => {
        if (at.now() < 100_000) throw new Error('bootstrap_status rejected')
      },
    })
    at.now = h.time.now
    await h.time.advance(110_000)
    assert.ok(h.finished())
    // The successful subscription at 50 s restarted the clock, so the failing
    // status read until 100 s never reached a full minute without an answer.
    assert.deepEqual(h.calls.stuck, [])
  }))

test('a minute without any answer reports a stuck app, and the next answer takes it back', () =>
  quietly(async () => {
    const h = harness({ subscribe: reject('listen rejected') })
    await h.time.advance(STUCK_AFTER_MS - 1_000)
    assert.deepEqual(h.calls.stuck, [])
    await h.time.advance(2_000)
    assert.equal(h.calls.stuck.length, 1)
    assert.equal(h.calls.stuck[0], STUCK_AFTER_MS)

    h.options.subscribe = async () => {}
    await h.time.advance(10_000)
    assert.ok(h.finished())
    assert.equal(h.calls.recovered.length, 1)
  }))

test('a call that never settles is reported too', async () => {
  const h = harness({ subscribe: never })
  await h.time.advance(STUCK_AFTER_MS + 1_000)
  assert.deepEqual(h.calls.stuck, [STUCK_AFTER_MS])
  assert.ok(!h.finished())
})

test('a status event is an answer, even while a call hangs', async () => {
  const h = harness({ snapshot: never })
  await h.time.advance(5_000)
  h.hear()
  await h.time.advance(STUCK_AFTER_MS * 2)
  assert.deepEqual(h.calls.stuck, [])
  assert.ok(!h.finished(), 'the hung call itself never returns')
})

test('a failed status read after an event is not retried', () =>
  quietly(async () => {
    let reads = 0
    const h = harness({})
    h.options.snapshot = async () => {
      reads += 1
      h.hear()
      throw new Error('bootstrap_status rejected')
    }
    await h.time.advance(10_000)
    assert.ok(h.finished())
    assert.equal(reads, 1)
    assert.deepEqual(h.calls.stuck, [])
  }))
