import { describe, expect, it, vi } from 'vitest';

import { FencedSource } from './fencedSource';

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void };
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const harness = () => {
  const pending: Array<{ key: string; reply: Deferred<string> }> = [];
  const applied: Array<[string, string]> = [];
  const failed: string[] = [];
  const source = new FencedSource<string, string>({
    read: (key) => {
      const reply = deferred<string>();
      pending.push({ key, reply });
      return reply.promise;
    },
    apply: (key, value) => applied.push([key, value]),
    fail: (key) => failed.push(key),
  });
  return { source, pending, applied, failed };
};

describe('FencedSource', () => {
  it('applies a read for the current key', async () => {
    const { source, pending, applied } = harness();
    source.setKey('A');
    source.refresh();
    pending[0].reply.resolve('a1');
    await flush();
    expect(applied).toEqual([['A', 'a1']]);
  });

  it('drops a late result and a late failure for a previous key', async () => {
    const { source, pending, applied, failed } = harness();
    source.setKey('A');
    source.refresh();
    source.refresh();
    source.setKey('B');
    pending[0].reply.reject(new Error('404 for A'));
    await flush();
    // A's queued trailing read was dropped with the key change.
    expect(pending).toHaveLength(1);
    source.refresh();
    pending[1].reply.resolve('b1');
    await flush();
    expect(failed).toEqual([]);
    expect(applied).toEqual([['B', 'b1']]);
  });

  it('drops a read that started before a live merge, then re-reads once', async () => {
    const { source, pending, applied } = harness();
    source.setKey('S');
    source.refresh();
    source.noteLiveMerge();
    pending[0].reply.resolve('stale');
    await flush();
    expect(applied).toEqual([]);
    expect(pending).toHaveLength(2);
    pending[1].reply.resolve('fresh');
    await flush();
    expect(applied).toEqual([['S', 'fresh']]);
  });

  it('coalesces a burst to one read in flight and one trailing read', async () => {
    const { source, pending, applied } = harness();
    source.setKey('S');
    for (let index = 0; index < 5; index += 1) source.refresh();
    expect(pending).toHaveLength(1);
    pending[0].reply.resolve('first');
    await flush();
    expect(pending).toHaveLength(2);
    pending[1].reply.resolve('second');
    await flush();
    expect(pending).toHaveLength(2);
    // The read in flight when a refresh was asked for is superseded by it.
    expect(applied.map(([, value]) => value)).toEqual(['second']);
  });

  it('drops a read superseded by a refresh, even if the trailing read fails', async () => {
    const { source, pending, applied } = harness();
    source.setKey('S');
    source.refresh();
    source.refresh();
    pending[0].reply.resolve('running');
    await flush();
    expect(applied).toEqual([]);
    pending[1].reply.reject(new Error('offline'));
    await flush();
    expect(applied).toEqual([]);
  });

  it('reads nothing without a key', () => {
    const read = vi.fn();
    const source = new FencedSource<string, string>({ read, apply: vi.fn() });
    source.refresh();
    expect(read).not.toHaveBeenCalled();
  });
});
