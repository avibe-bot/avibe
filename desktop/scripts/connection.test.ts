import assert from 'node:assert/strict'
import { test } from 'node:test'

import { connect, retryDelay, STUCK_AFTER_MS, type ConnectOptions } from '../src/connection.ts'

/** A fake clock: `sleep` advances time instead of waiting. */
function harness(overrides: Partial<ConnectOptions> & { failSubscribe?: number; failSnapshot?: number }) {
  let time = 0
  let subscribeFailures = overrides.failSubscribe ?? 0
  let snapshotFailures = overrides.failSnapshot ?? 0
  const calls = { subscribe: 0, snapshot: 0, stuck: 0, recovered: 0, sleeps: [] as number[] }
  const options: ConnectOptions = {
    subscribe: async () => {
      calls.subscribe += 1
      if (subscribeFailures > 0) {
        subscribeFailures -= 1
        throw new Error('listen rejected')
      }
    },
    snapshot: async () => {
      calls.snapshot += 1
      if (snapshotFailures > 0) {
        snapshotFailures -= 1
        throw new Error('bootstrap_status rejected')
      }
    },
    heardFromApp: () => false,
    onStuck: () => {
      calls.stuck += 1
    },
    onRecovered: () => {
      calls.recovered += 1
    },
    now: () => time,
    sleep: async (ms) => {
      calls.sleeps.push(ms)
      time += ms
    },
    ...overrides,
  }
  return { options, calls, elapsed: () => time }
}

const quiet = () => {
  const original = console.error
  console.error = () => {}
  return () => {
    console.error = original
  }
}

test('a start that answers at once never reports a stuck app', async () => {
  const { options, calls } = harness({})
  await connect(options)
  assert.deepEqual([calls.subscribe, calls.snapshot, calls.stuck, calls.recovered], [1, 1, 0, 0])
})

test('early failures on a slow start are retried without a failure message', async () => {
  const restore = quiet()
  try {
    // Ten failed subscriptions and three failed snapshots: about 30 seconds.
    const { options, calls, elapsed } = harness({ failSubscribe: 10, failSnapshot: 3 })
    await connect(options)
    restore()
    assert.equal(calls.stuck, 0)
    assert.equal(calls.subscribe, 11, 'subscribes until it succeeds once, never again after')
    assert.equal(calls.snapshot, 4)
    assert.ok(elapsed() < STUCK_AFTER_MS, `took ${elapsed()} ms`)
  } finally {
    restore()
  }
})

test('the stuck message appears only after a minute without any answer, and clears on the next answer', async () => {
  const restore = quiet()
  try {
    const { options, calls, elapsed } = harness({ failSubscribe: 1_000 })
    let stuckAt = -1
    options.onStuck = () => {
      calls.stuck += 1
      stuckAt = elapsed()
      // The app answers again a little later.
      options.subscribe = async () => {}
    }
    await connect(options)
    restore()
    assert.equal(calls.stuck, 1)
    assert.ok(stuckAt >= STUCK_AFTER_MS && stuckAt < STUCK_AFTER_MS + 5_000, `stuck at ${stuckAt} ms`)
    assert.equal(calls.recovered, 1)
  } finally {
    restore()
  }
})

test('a status event is an answer: a failed snapshot after it is not retried', async () => {
  const restore = quiet()
  try {
    let heard = false
    const { options, calls } = harness({
      failSnapshot: 1_000,
      heardFromApp: () => heard,
    })
    options.subscribe = async () => {
      calls.subscribe += 1
      heard = true
    }
    await connect(options)
    restore()
    // The snapshot is skipped because an event already arrived.
    assert.deepEqual([calls.snapshot, calls.stuck], [0, 0])

    heard = false
    const second = harness({ failSnapshot: 1_000, heardFromApp: () => heard })
    second.options.snapshot = async () => {
      second.calls.snapshot += 1
      heard = true
      throw new Error('bootstrap_status rejected')
    }
    quiet()
    await connect(second.options)
    restore()
    assert.deepEqual([second.calls.snapshot, second.calls.stuck], [1, 0])
  } finally {
    restore()
  }
})

test('retries back off to a steady interval', () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 50].map(retryDelay),
    [250, 500, 1_000, 2_000, 4_000, 5_000, 5_000],
  )
})
