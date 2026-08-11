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

  it('keeps the local text when a read starts during a pending write', async () => {
    const persistence = new SessionDraftPersistence();
    const first = deferred<{ ok: boolean }>();
    persistence.save('session-a', 'draft', async () => first.promise);

    const read = persistence.beginRead('session-a');
    expect(persistence.reconcileRead('session-a', read, '')).toBe('draft');
    first.resolve({ ok: true });
    await Promise.resolve();
  });

  it('retains a successful revision until an older read reconciles', async () => {
    const persistence = new SessionDraftPersistence();
    const read = persistence.beginRead('session-a');
    await persistence.save('session-a', 'newer', async () => ({ ok: true }));

    expect(persistence.reconcileRead('session-a', read, 'old')).toBe('newer');
    expect(persistence.revision('session-a')).toBe(0);
  });

  it('returns the local text when a read races a failed write', async () => {
    const persistence = new SessionDraftPersistence();
    const failed = persistence.save('session-a', 'local', async () => ({ ok: false }));
    await failed;
    const failedRead = persistence.beginRead('session-a');
    expect(persistence.reconcileRead('session-a', failedRead, '')).toBe('local');

    const pending = deferred<{ ok: boolean }>();
    const newerRead = persistence.beginRead('session-a');
    persistence.save('session-a', 'newer', async () => pending.promise);
    expect(persistence.reconcileRead('session-a', newerRead, 'old')).toBe('newer');
    pending.resolve({ ok: true });
    await Promise.resolve();
  });

  it('clears pending and failed state when a session is archived', async () => {
    const persistence = new SessionDraftPersistence();
    persistence.save('session-a', 'draft', async () => ({ ok: false }));
    await Promise.resolve();
    persistence.clearSession('session-a');

    expect(persistence.revision('session-a')).toBe(0);
    expect(persistence.beginRead('session-a').pending).toBe(false);
  });
});
