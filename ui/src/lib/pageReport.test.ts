import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { notePageHold, setPageWhyReader, takePageReport, type PageWhy } from './pageReport';

describe('the page report', () => {
  let stop: () => void = () => {};
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    stop();
    vi.useRealTimers();
  });

  it('has nothing to send for a page that gives no reason', () => {
    expect(takePageReport()).toBeNull();
    stop = setPageWhyReader(() => null);
    expect(takePageReport()).toBeNull();
  });

  it('sends a word when it changes, and the same word again only after ten seconds', () => {
    let word: PageWhy = 'no_path';
    stop = setPageWhyReader(() => word);
    expect(takePageReport()).toBe('no_path');
    expect(takePageReport()).toBeNull();
    vi.advanceTimersByTime(9_999);
    expect(takePageReport()).toBeNull();
    // A change is sent at once, and so is the change back.
    word = 'quiet_request';
    expect(takePageReport()).toBe('quiet_request');
    word = 'no_path';
    expect(takePageReport()).toBe('no_path');
    vi.advanceTimersByTime(10_000);
    expect(takePageReport()).toBe('no_path');
  });

  it('says a hold once, with the word, whether or not the word is due', () => {
    stop = setPageWhyReader(() => 'on_path');
    expect(takePageReport()).toBe('on_path');
    notePageHold(37.4);
    expect(takePageReport()).toBe('on_path;hold=37');
    expect(takePageReport()).toBeNull();
    // Within the instance's bounds, and of two holds the later.
    notePageHold(5_000);
    notePageHold(-3);
    expect(takePageReport()).toBe('on_path;hold=0');
    notePageHold(5_000);
    expect(takePageReport()).toBe('on_path;hold=1000');
  });

  it('keeps no hold for a page that has no word', () => {
    notePageHold(40);
    stop = setPageWhyReader(() => 'idle');
    expect(takePageReport()).toBe('idle');
  });

  it('starts again with a new reader', () => {
    stop = setPageWhyReader(() => 'idle');
    expect(takePageReport()).toBe('idle');
    stop();
    expect(takePageReport()).toBeNull();
    stop = setPageWhyReader(() => 'idle');
    expect(takePageReport()).toBe('idle');
  });
});
