import { describe, expect, it } from 'vitest';
import { SessionDraftPersistence } from './sessionDraftPersistence';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
};

describe('SessionDraftPersistence', () => {
  it('serializes writes for one session and keeps the latest text', async () => {
    const persistence = new SessionDraftPersistence();
    const first = deferred<{ ok: boolean }>();
    const calls: string[] = [];

    persistence.save('session-a', 'first', async () => {
      calls.push('first');
      return first.promise;
    });
    await Promise.resolve();
    const second = persistence.save('session-a', 'second', async () => {
      calls.push('second');
      return { ok: true };
    });

    expect(calls).toEqual(['first']);
    first.resolve({ ok: true });
    await second;
    expect(calls).toEqual(['first', 'second']);
    expect(persistence.revision('session-a')).toBe(0);
  });

  it('does not block writes for different sessions', async () => {
    const persistence = new SessionDraftPersistence();
    const first = deferred<{ ok: boolean }>();
    const calls: string[] = [];

    persistence.save('session-a', 'first', async () => {
      calls.push('a');
      return first.promise;
    });
    await persistence.save('session-b', 'other', async () => {
      calls.push('b');
      return { ok: true };
    });

    expect(calls).toEqual(['a', 'b']);
    first.resolve({ ok: true });
  });

  it('waits for a write before a bootstrap read', async () => {
    const persistence = new SessionDraftPersistence();
    const first = deferred<{ ok: boolean }>();
    let readStarted = false;
    persistence.save('session-a', 'draft', async () => first.promise);

    const read = (async () => {
      await persistence.waitForWrites('session-a');
      readStarted = true;
    })();
    await Promise.resolve();
    expect(readStarted).toBe(false);
    first.resolve({ ok: true });
    await read;
    expect(readStarted).toBe(true);
  });

  it('returns the local text when a read races a newer or failed write', async () => {
    const persistence = new SessionDraftPersistence();
    const failed = persistence.save('session-a', 'local', async () => ({ ok: false }));
    await failed;
    expect(persistence.reconcileRead('session-a', 1, '')).toBe('local');

    const pending = deferred<{ ok: boolean }>();
    persistence.save('session-a', 'newer', async () => pending.promise);
    expect(persistence.reconcileRead('session-a', 1, 'old')).toBe('newer');
    pending.resolve({ ok: true });
  });
});
