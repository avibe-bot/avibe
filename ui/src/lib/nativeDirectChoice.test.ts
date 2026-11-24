/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  nativeDirectChoiceAvailable, nativeDirectChoiceShell, nativeDirectOnlyAvailable, onNativeDirectChoiceChanged,
  readNativeDirectChoice, setNativeDirectChoice,
} from './nativeDirectChoice';

// The path choice through the shell that answers `direct` (docs/plans/ios-app.md, `directPreference`).
function shell(platform: 'ios' | 'desktop', directPreference: number, answer: unknown = { mode: 'automatic', reverted: null }) {
  const call = vi.fn(async () => answer);
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: { contract: 1, platform, shellVersion: '3.2.8', capabilities: { direct: 3, directPreference }, call, on: () => () => undefined },
  });
  return call;
}

afterEach(() => { Reflect.deleteProperty(window, 'avibeNative'); });

describe('nativeDirectChoice', () => {
  it('is the App\'s and a Desktop window\'s, and 「仅直连和中继」 only from version 2', () => {
    shell('ios', 1);
    expect([nativeDirectChoiceAvailable(), nativeDirectOnlyAvailable(), nativeDirectChoiceShell()]).toEqual([true, false, 'app']);
    shell('desktop', 2);
    expect([nativeDirectChoiceAvailable(), nativeDirectOnlyAvailable(), nativeDirectChoiceShell()]).toEqual([true, true, 'desktop']);
    shell('desktop', 0);
    expect([nativeDirectChoiceAvailable(), nativeDirectChoiceShell()]).toEqual([false, null]);
  });

  it('never asks a shell before version 2 for direct', async () => {
    const call = shell('ios', 1);
    expect(await setNativeDirectChoice('direct')).toBeNull();
    expect(call).not.toHaveBeenCalled();
    shell('ios', 2, { mode: 'direct', reverted: null });
    expect(await setNativeDirectChoice('direct')).toEqual({ mode: 'direct', reverted: null });
  });

  it('reads which choice went back, and a shell before `from` only ever puts the tunnel back', async () => {
    shell('ios', 2, { mode: 'automatic', reverted: { at: 1_700_000_000_000, reason: 'unreachable', from: 'direct' } });
    expect(await readNativeDirectChoice()).toEqual({ mode: 'automatic', reverted: { at: 1_700_000_000_000, from: 'direct' } });
    shell('ios', 1, { mode: 'automatic', reverted: { at: 1_700_000_000_000, reason: 'unreachable' } });
    expect(await readNativeDirectChoice()).toEqual({ mode: 'automatic', reverted: { at: 1_700_000_000_000, from: 'tunnel' } });
    shell('ios', 2, { mode: 'later', reverted: null });
    expect(await readNativeDirectChoice()).toBeNull();
  });

  it('hears the shell put a choice back', () => {
    const heard = vi.fn();
    const stop = onNativeDirectChoiceChanged(heard);
    window.dispatchEvent(new CustomEvent('avibe:native:direct.preferenceChanged', { detail: { mode: 'automatic', reverted: { at: 5, reason: 'unreachable', from: 'direct' } } }));
    stop();
    expect(heard).toHaveBeenCalledWith({ mode: 'automatic', reverted: { at: 5, from: 'direct' } });
  });
});
