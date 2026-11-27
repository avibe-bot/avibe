import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.resetModules();
});

/** A fresh page: neither the stream nor any request has said anything. */
const freshPage = async () => await import('./liveStreamVia');

describe('how the page reaches the instance', () => {
  it('is direct only when the stream and the latest request both say so', async () => {
    const page = await freshPage();
    const request = (word: string | null) => page.noteRequestVia(page.beginRequestVia(), word);
    // Unknown is never "not direct", and never direct either.
    expect(page.pageVia()).toBeNull();
    page.setLiveStreamVia('direct');
    expect(page.pageVia()).toBeNull();
    request('direct');
    expect(page.pageVia()).toBe('direct');

    // The stream is a connection of its own: either one alone says "other".
    request('other');
    expect(page.pageVia()).toBe('other');
    request('direct');
    page.setLiveStreamVia('other');
    expect(page.pageVia()).toBe('other');

    // A stream that closed says nothing; what the requests said stands.
    page.setLiveStreamVia(null);
    expect(page.pageVia()).toBeNull();
    request('other');
    expect(page.pageVia()).toBe('other');
  });

  it('keeps the latest word through answers that say nothing, or something else', async () => {
    const page = await freshPage();
    const request = (word: string | null) => page.noteRequestVia(page.beginRequestVia(), word);
    page.setLiveStreamVia('direct');
    request('direct');
    // An asset, a public route, an instance that does not say: no flicker.
    for (const word of [null, '', 'tunnel', 'DIRECT']) request(word);
    expect(page.latestRequestVia()).toBe('direct');
    expect(page.pageVia()).toBe('direct');
  });

  it('takes answers in the order their requests began', async () => {
    const page = await freshPage();
    // A slow request on the old connection, then one on the new.
    const slow = page.beginRequestVia();
    const later = page.beginRequestVia();
    page.noteRequestVia(later, 'direct');
    page.noteRequestVia(slow, 'other');
    expect(page.latestRequestVia()).toBe('direct');
    // The next request speaks again.
    page.noteRequestVia(page.beginRequestVia(), 'other');
    expect(page.latestRequestVia()).toBe('other');
  });

  it('tells its listeners of a change of either word, and of nothing else', async () => {
    const page = await freshPage();
    const heard = vi.fn();
    const stop = page.subscribePageVia(heard);
    page.setLiveStreamVia('direct');
    page.noteRequestVia(page.beginRequestVia(), 'direct');
    expect(heard).toHaveBeenCalledTimes(2);
    page.setLiveStreamVia('direct');
    page.noteRequestVia(page.beginRequestVia(), 'direct');
    page.noteRequestVia(page.beginRequestVia(), null);
    expect(heard).toHaveBeenCalledTimes(2);
    stop();
    page.setLiveStreamVia(null);
    expect(heard).toHaveBeenCalledTimes(2);
  });
});
