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

  it('sends what the Desktop app gives its view of the instance, once per page and without asking', async () => {
    const computer = {
      kind: 'desktop_app',
      display_name: 'Studio Mac',
      installation_id: '3f'.repeat(32),
      aliases: [],
      app_version: '0.4.2',
      system_version: 'macOS 26.1',
    };
    vi.stubGlobal('window', { __AVIBE_DESKTOP_DEVICE__: Object.freeze(computer) });

    await reportNativeDevice(apiFetch);
    await reportNativeDevice(apiFetch);

    expect(apiFetch).toHaveBeenCalledTimes(1);
    expect(apiFetch).toHaveBeenCalledWith('/api/devices/self', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(computer),
    });
  });

  it('never lets a failed report surface', async () => {
    installShell(described);
    apiFetch.mockRejectedValueOnce(new Error('offline'));

    await expect(reportNativeDevice(apiFetch)).resolves.toBeUndefined();
  });

  describe('with a Health binding ticket', () => {
    // Synthetic: 43 base64url characters, the shape of 32 random bytes.
    const ticket = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-';
    const withTicket = { ...described, health_binding_ticket: ticket };
    const posted = () => apiFetch.mock.calls.map(([, init]) => JSON.parse(String(init.body)));

    it('asks for it only for the report, sends it once and caches the description without it', async () => {
      const call = installShell(withTicket);

      await reportNativeDevice(apiFetch);
      await reportNativeDevice(apiFetch);

      expect(call).toHaveBeenCalledTimes(1);
      expect(call).toHaveBeenCalledWith('device.describe', { health_binding: true });
      expect(posted()).toEqual([{ ...described, health_binding_ticket: ticket }]);
      await expect(nativeDeviceLabel()).resolves.toBe('iPhone 16 Pro');
      expect(call).toHaveBeenCalledTimes(1);
    });

    it('sends the description once more without it when the ticket is refused, and never the ticket again', async () => {
      for (const refusal of [
        new Response('{"error":"health_binding_not_accepted"}', { status: 403 }),
        // An older instance that doesn't know the field.
        new Response('{"error":"invalid_device_description"}', { status: 400 }),
      ]) {
        apiFetch.mockClear();
        installShell(withTicket);
        apiFetch.mockResolvedValueOnce(refusal);

        await reportNativeDevice(apiFetch);
        await reportNativeDevice(apiFetch);

        expect(posted()).toEqual([{ ...described, health_binding_ticket: ticket }, described]);
        vi.unstubAllGlobals();
      }
    });

    it('sends nothing more on any other answer', async () => {
      for (const [status, body] of [
        [403, '{"error":"remote_session_required"}'],
        [404, '{"error":"device_not_found"}'],
        [409, '{"error":"installation_mismatch"}'],
        [429, '{"error":"health_binding_rate_limited"}'],
        [503, '{"error":"health_binding_unavailable"}'],
      ] as const) {
        apiFetch.mockClear();
        installShell(withTicket);
        apiFetch.mockResolvedValueOnce(new Response(body, { status }));

        await reportNativeDevice(apiFetch);

        expect(posted()).toEqual([{ ...described, health_binding_ticket: ticket }]);
        vi.unstubAllGlobals();
      }
    });

    it('reports without one when the App attaches none, or a malformed one', async () => {
      for (const answer of [described, { ...described, health_binding_ticket: 'short' }, { ...described, health_binding_ticket: 7 }]) {
        apiFetch.mockClear();
        installShell(answer);

        await reportNativeDevice(apiFetch);

        expect(posted()).toEqual([described]);
        vi.unstubAllGlobals();
      }
    });

    it('describes as before when an older App refuses the question', async () => {
      const call = installShell(described);
      call.mockImplementation(async (_method: string, params?: Record<string, unknown>) => {
        if (params?.health_binding) throw new Error('device.describe does not accept parameters.');
        return described;
      });

      await reportNativeDevice(apiFetch);

      expect(call).toHaveBeenCalledTimes(2);
      expect(posted()).toEqual([described]);
    });
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
