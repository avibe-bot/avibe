import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.fn(async (_input: string, _init: RequestInit) => new Response('{"ok":true}', { status: 200 }));

import { nativeDeviceLabel, reportNativeDevice } from './deviceReport';

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

  it('reports nothing until the App has an installation id, but names the device', async () => {
    const { installation_id: _id, ...withoutID } = described;
    const call = installShell(withoutID);

    await reportNativeDevice(apiFetch);
    expect(apiFetch).not.toHaveBeenCalled();
    await expect(nativeDeviceLabel()).resolves.toBe('iPhone 16 Pro');

    // Push sets up; the next activation of the same page reports.
    call.mockImplementation(async () => described);
    await reportNativeDevice(apiFetch);
    await reportNativeDevice(apiFetch);
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it('never lets a failed report surface', async () => {
    installShell(described);
    apiFetch.mockRejectedValueOnce(new Error('offline'));

    await expect(reportNativeDevice(apiFetch)).resolves.toBeUndefined();
  });

  it('sends only the fields of the instance contract', async () => {
    installShell({
      ...described,
      extra: 'x',
      aliases: [{ source: 'health', id: 'h' }, { source: 'other', id: 'o' }, { source: 'schedule', id: '' }, 'x'],
    });

    await reportNativeDevice(apiFetch);

    expect(JSON.parse(String(apiFetch.mock.calls[0]?.[1]?.body))).toEqual({
      kind: 'iphone_app',
      display_name: 'iPhone 16 Pro',
      installation_id: '5eeb24aca55730b9f93cc16472af8b16',
      aliases: [{ source: 'health', id: 'h' }],
      app_version: '1.0 (42)',
      system_version: 'iOS 26.1',
    });
  });

  it.each([
    ['no name', { ...described, display_name: '' }],
    ['a name over 120 characters', { ...described, display_name: 'x'.repeat(121) }],
    ['a version that is not text', { ...described, app_version: 1 }],
    ['no aliases list', { ...described, aliases: undefined }],
    ['not an object', 'x'],
  ])('sends nothing and keeps the old name for an answer with %s', async (_name, answer) => {
    installShell(answer);

    await reportNativeDevice(apiFetch);

    expect(apiFetch).not.toHaveBeenCalled();
    await expect(nativeDeviceLabel()).resolves.toBe('iPhone App');
  });
});
