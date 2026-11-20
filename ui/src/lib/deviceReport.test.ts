import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn(async (_input: string, _init: RequestInit) => new Response('{"ok":true}', { status: 200 }));

import {
  nativeDeviceLabel,
  parseNativeDeviceDescription,
  reportNativeDevice,
  resetNativeDeviceReportForTests,
} from './deviceReport';

const described = {
  kind: 'iphone_app',
  display_name: 'iPhone 16 Pro',
  installation_id: '5eeb24aca55730b9f93cc16472af8b16',
  aliases: [{ source: 'health', id: 'health-installation' }, { source: 'schedule', id: 'schedule-id' }],
  app_version: '1.0 (42)',
  system_version: 'iOS 26.1',
};

function installShell(answer: unknown, capabilities: Record<string, number> = { device: 1 }) {
  const call = vi.fn(async (method: string) => {
    if (method !== 'device.describe') throw new Error(`unexpected ${method}`);
    if (answer instanceof Error) throw answer;
    return answer;
  });
  vi.stubGlobal('window', {
    avibeNative: {
      contract: 1, platform: 'ios', shellVersion: 'test', capabilities, call, on: vi.fn(() => () => undefined),
    },
  });
  return call;
}

describe('the device report', () => {
  beforeEach(() => {
    apiFetch.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetNativeDeviceReportForTests();
  });

  it('sends what the App describes to the instance once per page', async () => {
    const call = installShell(described);

    await reportNativeDevice(apiFetch);
    await reportNativeDevice(apiFetch);

    expect(call).toHaveBeenCalledTimes(1);
    expect(apiFetch).toHaveBeenCalledTimes(1);
    expect(apiFetch).toHaveBeenCalledWith('/api/devices/self', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(described),
    });
    await expect(nativeDeviceLabel()).resolves.toBe('iPhone 16 Pro');
  });

  it('sends nothing outside the App, from an older App, or when the App refuses', async () => {
    for (const [answer, capabilities] of [
      [described, { push: 1 }],
      [new Error('forbidden'), { device: 1 }],
      [{ ...described, kind: 'browser' }, { device: 1 }],
    ] as const) {
      installShell(answer, capabilities);
      await reportNativeDevice(apiFetch);
      await expect(nativeDeviceLabel()).resolves.toBe('iPhone App');
      vi.unstubAllGlobals();
      resetNativeDeviceReportForTests();
    }
    await reportNativeDevice(apiFetch);
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it('asks again after the App refused, and reports once it answers', async () => {
    let answer: unknown = new Error('forbidden');
    const call = installShell(undefined);
    call.mockImplementation(async () => {
      if (answer instanceof Error) throw answer;
      return answer;
    });

    await reportNativeDevice(apiFetch);
    await expect(nativeDeviceLabel()).resolves.toBe('iPhone App');
    expect(apiFetch).not.toHaveBeenCalled();

    answer = described;
    await reportNativeDevice(apiFetch);
    await reportNativeDevice(apiFetch);
    expect(apiFetch).toHaveBeenCalledTimes(1);
    await expect(nativeDeviceLabel()).resolves.toBe('iPhone 16 Pro');
  });

  it('never lets a failed report surface', async () => {
    installShell(described);
    apiFetch.mockRejectedValueOnce(new Error('offline'));

    await expect(reportNativeDevice(apiFetch)).resolves.toBeUndefined();
  });

  it('keeps only the fields of the instance contract', () => {
    expect(parseNativeDeviceDescription({
      ...described,
      extra: 'x',
      installation_id: '',
      aliases: [{ source: 'health', id: 'h' }, { source: 'other', id: 'o' }, { source: 'schedule', id: '' }, 'x'],
    })).toEqual({
      kind: 'iphone_app',
      display_name: 'iPhone 16 Pro',
      aliases: [{ source: 'health', id: 'h' }],
      app_version: '1.0 (42)',
      system_version: 'iOS 26.1',
    });
    for (const broken of [
      null,
      'x',
      { ...described, display_name: '' },
      { ...described, display_name: 'x'.repeat(121) },
      { ...described, app_version: 1 },
      { ...described, aliases: undefined },
    ]) {
      expect(parseNativeDeviceDescription(broken)).toBeNull();
    }
  });
});
