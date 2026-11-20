// @vitest-environment jsdom
// ui/design: update/instance-and-app. The chip beside another computer's name is that instance's version,
// from the instance's own page. An update of the instance must show as running there until it ends, with the
// panel closed and whoever started it (the shell's panel is another window; another device may have).
import { act, cleanup, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import type { VersionInfo } from '../context/ApiContext';
import { InstanceVersionChip } from './InstanceVersionChip';

const api = vi.hoisted(() => ({ getVersion: vi.fn<(options?: unknown) => Promise<Partial<VersionInfo>>>() }));
vi.mock('../context/ApiContext', () => ({ useApi: () => api }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
const reload = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  api.getVersion.mockReset();
  reload.mockReset();
  vi.stubGlobal('location', { ...window.location, reload });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const idle = { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true };
const installing = { ...idle, install: { phase: 'installing' as const, target: '3.2.8rc4', started_at: '2026-11-20T07:03:17Z' } };
const mount = (appUpdate = false) => render(<I18nextProvider i18n={i18n}>
  <InstanceVersionChip name="alex" appUpdate={appUpdate} onPresent={() => {}} />
</I18nextProvider>);
const chip = () => screen.getByRole('button', { name: 'alex is on v3.2.8rc2' });
const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

it("turns gold when the instance or this computer's app can be updated, and stays plain when neither can", async () => {
  api.getVersion.mockResolvedValue({ current: '3.2.8rc2', latest: '3.2.8rc2', has_update: false });
  const quiet = mount();
  await tick();
  expect(chip().className).not.toContain('gold');
  quiet.unmount();
  mount(true);
  await tick();
  expect(chip().className).toContain('gold');
});

it('reads the version again when the window comes back, and follows an install started elsewhere to its end', async () => {
  api.getVersion.mockResolvedValueOnce(idle);
  mount();
  await tick();
  expect(chip().querySelector('.animate-spin')).toBeNull();

  // The update was clicked in the shell's panel, another window: coming back to this one reads the version.
  api.getVersion.mockResolvedValue(installing);
  vi.setSystemTime(Date.now() + 10_000);
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  await tick();
  expect(chip().querySelector('.animate-spin')).toBeTruthy();

  // The Runtime stops answering while it restarts; the chip keeps saying it is running.
  api.getVersion.mockRejectedValue(new Error('offline'));
  await tick(3000);
  expect(chip().querySelector('.animate-spin')).toBeTruthy();
  expect(reload).not.toHaveBeenCalled();

  // It answers with the new version: the page loads again, since its interface changed with it.
  api.getVersion.mockResolvedValue({ current: '3.2.8rc4', latest: '3.2.8rc4', has_update: false });
  await tick(3000);
  expect(reload).toHaveBeenCalledOnce();
});

it('keeps reading while the panel it asked for is open, so an install started there shows here at once', async () => {
  api.getVersion.mockResolvedValueOnce(idle);
  mount();
  await tick();
  // The panel is another window and holds the focus: no focus event reaches this page while it is open.
  await act(async () => { chip().click(); });
  api.getVersion.mockResolvedValue(installing);
  await tick(5000);
  expect(chip().querySelector('.animate-spin')).toBeTruthy();
});

it('loads the page again when a read on return finds the new version before the next poll does', async () => {
  api.getVersion.mockResolvedValueOnce(installing);
  mount();
  await tick();
  // The instance restarted between two polls, and the window comes back to the front first.
  api.getVersion.mockResolvedValue({ current: '3.2.8rc4', latest: '3.2.8rc4', has_update: false });
  vi.setSystemTime(Date.now() + 10_000);
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  await tick();
  expect(reload).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'alex is on v3.2.8rc4' })).toBeNull();
});

it('goes back to offering the update when the install ends without a new version', async () => {
  api.getVersion.mockResolvedValueOnce(installing);
  mount();
  await tick();
  expect(chip().querySelector('.animate-spin')).toBeTruthy();
  api.getVersion.mockResolvedValue(idle);
  await tick(3000);
  expect(chip().querySelector('.animate-spin')).toBeNull();
  expect(chip().className).toContain('gold');
  expect(reload).not.toHaveBeenCalled();
});
