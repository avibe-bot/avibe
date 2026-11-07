import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The engine check (lib/voiceEngine): a running context whose clock stands still renders nothing.
// Each test reads the page's verdict on a fresh module, as a page that loaded it.

const engine = async () => {
  vi.resetModules();
  return import('./voiceEngine');
};

/** A context whose clock moves while it runs, or stands still (`stalled`). */
const contextThat = (state: string, stalled = false) => {
  const startedAt = Date.now();
  return {
    state,
    get currentTime() {
      return this.state === 'running' && !stalled ? (Date.now() - startedAt) / 1000 : 0;
    },
  } as unknown as AudioContext & { state: string };
};

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('checkVoiceEngine', () => {
  it('finds a running context healthy as soon as its clock moves', async () => {
    const { checkVoiceEngine } = await engine();
    const verdict = checkVoiceEngine(contextThat('running'));
    await vi.advanceTimersByTimeAsync(100);
    await expect(verdict).resolves.toBe('healthy');
  });

  it('finds one whose clock stands still while it runs stalled, and not before VOICE_ENGINE_STALL_MS', async () => {
    const { checkVoiceEngine, VOICE_ENGINE_STALL_MS } = await engine();
    let verdict: string | null = null;
    void checkVoiceEngine(contextThat('running', true)).then((value) => {
      verdict = value;
    });
    await vi.advanceTimersByTimeAsync(VOICE_ENGINE_STALL_MS - 100);
    expect(verdict).toBeNull();
    await vi.advanceTimersByTimeAsync(200);
    expect(verdict).toBe('stalled');
  });

  it('judges no context that is not running: the autoplay policy is not a stall', async () => {
    const { checkVoiceEngine, VOICE_ENGINE_STALL_MS } = await engine();
    const suspended = checkVoiceEngine(contextThat('suspended'));
    await vi.advanceTimersByTimeAsync(VOICE_ENGINE_STALL_MS + 100);
    await expect(suspended).resolves.toBe('unknown');

    const closing = contextThat('running', true);
    const closed = checkVoiceEngine(closing);
    closing.state = 'closed';
    await vi.advanceTimersByTimeAsync(100);
    await expect(closed).resolves.toBe('unknown');
  });

  it('judges a context that starts running from when it runs', async () => {
    const { checkVoiceEngine, VOICE_ENGINE_STALL_MS } = await engine();
    const context = contextThat('suspended', true);
    let verdict: string | null = null;
    void checkVoiceEngine(context).then((value) => {
      verdict = value;
    });
    await vi.advanceTimersByTimeAsync(300);
    context.state = 'running';
    await vi.advanceTimersByTimeAsync(VOICE_ENGINE_STALL_MS - 100);
    expect(verdict).toBeNull();
    await vi.advanceTimersByTimeAsync(200);
    expect(verdict).toBe('stalled');
  });

  it('checks a context once at a time', async () => {
    const { checkVoiceEngine } = await engine();
    const context = contextThat('running');
    expect(checkVoiceEngine(context)).toBe(checkVoiceEngine(context));
  });
});

describe("the page's verdict", () => {
  it('records what a search found: a rate that renders, or none', async () => {
    const { preferredVoiceSampleRate, recordVoiceEngine, voiceEngineStalled } = await engine();
    expect([voiceEngineStalled(), preferredVoiceSampleRate()]).toEqual([false, undefined]);
    recordVoiceEngine({ rate: 16_000 });
    expect([voiceEngineStalled(), preferredVoiceSampleRate()]).toEqual([false, 16_000]);
    recordVoiceEngine(null);
    expect(voiceEngineStalled()).toBe(true);
    // The default renders again: no rate is preferred.
    recordVoiceEngine({ rate: undefined });
    expect([voiceEngineStalled(), preferredVoiceSampleRate()]).toEqual([false, undefined]);
  });

  it.each([
    ['an iPhone, any browser', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0 Mobile/15E148 Safari/604.1', true],
    ['Safari on a Mac', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15', true],
    ['Chrome on a Mac', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36', false],
    ['Firefox', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 15.6; rv:146.0) Gecko/20100101 Firefox/146.0', false],
  ])('tries other rates only on WebKit: %s', async (_name, userAgent, expected) => {
    vi.stubGlobal('navigator', { userAgent });
    const { voiceEngineCanChangeRate } = await engine();
    expect(voiceEngineCanChangeRate()).toBe(expected);
  });
});
