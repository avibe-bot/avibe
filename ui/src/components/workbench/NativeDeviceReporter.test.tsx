/** @vitest-environment jsdom */
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn(async (_input: string, _init: RequestInit) => new Response('{"ok":true}')));
vi.mock('@/lib/apiFetch', () => ({ apiFetch }));

import { NativeDeviceReporter } from './NativeDeviceReporter';
import { DESKTOP_DEVICE_EVENT } from '@/lib/native';

const computer = {
  kind: 'desktop_app',
  display_name: 'Studio Mac',
  installation_id: '3f'.repeat(32),
  aliases: [],
  app_version: '0.4.2',
  system_version: 'macOS 26.1',
};

describe('NativeDeviceReporter in the Desktop app', () => {
  afterEach(() => {
    cleanup();
    apiFetch.mockClear();
    delete (window as unknown as Record<string, unknown>).__AVIBE_DESKTOP_DEVICE__;
  });

  it('reports nothing until the app describes the computer, then reports it once', async () => {
    render(<NativeDeviceReporter />);
    // A page of a view that is not yet the committed one, or not the app's at all.
    window.dispatchEvent(new Event(DESKTOP_DEVICE_EVENT));
    await Promise.resolve();
    expect(apiFetch).not.toHaveBeenCalled();

    // The view is published: the app gives this document the description, then announces it.
    Object.defineProperty(window, '__AVIBE_DESKTOP_DEVICE__', { value: Object.freeze(computer), configurable: true });
    window.dispatchEvent(new Event(DESKTOP_DEVICE_EVENT));
    window.dispatchEvent(new Event(DESKTOP_DEVICE_EVENT));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    expect(apiFetch).toHaveBeenCalledWith('/api/devices/self', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(computer),
    });
  });
});
