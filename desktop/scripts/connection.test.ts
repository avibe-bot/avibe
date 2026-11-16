import assert from 'node:assert/strict'
import { test } from 'node:test'

import { connect, retryDelay, STUCK_AFTER_MS, type ConnectOptions } from '../src/connection.ts'

/** A manual clock: timers run only when the test advances time. */
function clock() {
  let now = 0
  let next = 0
  const timers = new Map<number, { at: number; run: () => void }>()
  return {
    now: () => now,
    schedule(run: () => void, ms: number) {
      const id = next++
      timers.set(id, { at: now + ms, run })
      return () => {
        timers.delete(id)
      }
    },
    /** Moves time forward in small steps, letting promises settle in between. */
    async advance(ms: number, step = 50) {
      const end = now + ms
      while (now < end) {
        await flush()
        now = Math.min(now + step, end)
        for (const [id, timer] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
          if (timer.at <= now) {
            timers.delete(id)
            timer.run()
          }
        }
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
    schedule: (run, ms) => time.schedule(run, ms),
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

test('retries back off to a steady interval', () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 50].map(retryDelay),
    [250, 500, 1_000, 2_000, 4_000, 5_000, 5_000],
  )
})
