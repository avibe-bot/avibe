/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { measureNativeDirect, nativeDirectMeasureAvailable } from './nativeDirectMeasure';

// `direct.measure()` through the shell that answers `direct` (capability `directMeasure`).
function shell(platform: 'ios' | 'desktop', directMeasure: number, answer: () => Promise<unknown>) {
  const call = vi.fn(answer);
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform, shellVersion: '3.2.8', capabilities: { direct: 3, ...(directMeasure ? { directMeasure } : {}) }, call, on: () => () => undefined },
  });
  return call;
}

afterEach(() => { Reflect.deleteProperty(window, 'avibeNative'); });

describe('nativeDirectMeasure', () => {
  it('is offered only where the shell says it can measure, on the App and on Desktop', () => {
    shell('ios', 0, async () => null);
    expect(nativeDirectMeasureAvailable()).toBe(false);
    shell('ios', 1, async () => null);
    expect(nativeDirectMeasureAvailable()).toBe(true);
    shell('desktop', 1, async () => null);
    expect(nativeDirectMeasureAvailable()).toBe(true);
  });

  it('reads both legs, keeping only what names the route and the times', async () => {
    const call = shell('desktop', 1, async () => ({
      measuredAt: 1_700_000_000_000, basis: 'warm-https-health', samples: 5,
      current: { kind: 'ipv6', scope: 'global', relayRegion: 'ignored', ok: 5, medianMs: 21, maxMs: 30, error: null, address: '2001:db8::1' },
      tunnel: { kind: 'tunnel', scope: 'local', relayRegion: null, ok: 0, medianMs: 9, maxMs: 9, error: 'timeout' },
    }));
    expect(await measureNativeDirect()).toEqual({
      measuredAt: 1_700_000_000_000, samples: 5,
      current: { kind: 'ipv6', scope: 'global', relayRegion: null, ok: 5, medianMs: 21, maxMs: 30, error: null },
      tunnel: { kind: 'tunnel', scope: null, relayRegion: null, ok: 0, medianMs: null, maxMs: null, error: 'timeout' },
    });
    expect(call).toHaveBeenCalledWith('direct.measure');
  });

  it('says busy for a measurement already running, and failed for anything else', async () => {
    shell('ios', 1, async () => { throw { code: 'busy' }; });
    expect(await measureNativeDirect()).toBe('busy');
    shell('ios', 1, async () => { throw new Error('gone'); });
    expect(await measureNativeDirect()).toBe('failed');
    shell('ios', 1, async () => ({ samples: 5 }));
    expect(await measureNativeDirect()).toBe('failed');
    shell('ios', 0, async () => null);
    expect(await measureNativeDirect()).toBe('failed');
  });
});
