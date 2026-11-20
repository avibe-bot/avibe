// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { APP_LOCK_FIXTURES } from '../../lib/appLockContract';
import { AppLockSetting } from './AppLockSetting';

type Call = (method: string, params?: Record<string, unknown>) => Promise<unknown>;

function iphone(call: Call, capabilities: Record<string, number> = { appLock: 1 }) {
  Object.defineProperty(window, 'avibeNative', {
    value: { contract: 1, platform: 'ios', shellVersion: '1', capabilities, call, on: () => () => {} },
    configurable: true,
  });
}
function mac(invoke: Call, marker = true) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
  if (marker) Object.defineProperty(window, '__AVIBE_DESKTOP_APP_LOCK__', { value: true, configurable: true });
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
}
const show = () => render(<I18nextProvider i18n={i18n}><AppLockSetting /></I18nextProvider>);
const select = () => screen.findByRole('combobox', { name: i18n.t('settings.general.appLock.title') }) as Promise<HTMLSelectElement>;

afterEach(() => {
  cleanup();
  for (const key of ['avibeNative', '__AVIBE_DESKTOP_SHELL__', '__AVIBE_DESKTOP_APP_LOCK__', '__TAURI_INTERNALS__']) {
    delete (window as unknown as Record<string, unknown>)[key];
  }
});

describe('App Lock setting', () => {
  it('is absent without a shell that owns App Lock', async () => {
    const call = vi.fn();
    iphone(call, { push: 1 });
    await act(async () => { show(); });
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(call).not.toHaveBeenCalled();
  });

  it('is absent on a desktop shell without the App Lock marker', async () => {
    const invoke = vi.fn();
    mac(invoke, false);
    await act(async () => { show(); });
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(invoke).not.toHaveBeenCalled();
  });

  it('hides when the shell answers outside the contract', async () => {
    iphone(vi.fn(async () => ({ policy: 'sometimes' })));
    await act(async () => { show(); });
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('shows the iPhone choice and changes it only through the shell', async () => {
    const call = vi.fn(async (method: string) => (method === 'appLock.status' ? APP_LOCK_FIXTURES.iphoneOff : APP_LOCK_FIXTURES.setChanged));
    iphone(call);
    show();
    const control = await select();
    expect(control.value).toBe('off');
    expect(screen.getByText(i18n.t('settings.general.appLock.descriptionFaceId'))).toBeTruthy();
    fireEvent.change(control, { target: { value: 'fiveMinutes' } });
    await vi.waitFor(() => expect(control.value).toBe('fiveMinutes'));
    expect(call).toHaveBeenLastCalledWith('appLock.setPolicy', { policy: 'fiveMinutes' });
    expect(screen.getByText(i18n.t('settings.general.appLock.coversPhone'))).toBeTruthy();
  });

  it('keeps the shell value and says so when verification is cancelled', async () => {
    const invoke = vi.fn(async (command: string) => (command === 'app_lock_status'
      ? { ...APP_LOCK_FIXTURES.setCancelled.status }
      : APP_LOCK_FIXTURES.setCancelled));
    mac(invoke);
    show();
    const control = await select();
    fireEvent.change(control, { target: { value: 'immediately' } });
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText(i18n.t('settings.general.appLock.failed'))).toBeTruthy();
    expect(control.value).toBe('off');
    expect(invoke).toHaveBeenCalledWith('app_lock_set_policy', { policy: 'immediately' });
  });

  it('reads as Off and explains when the device has no passcode', async () => {
    iphone(vi.fn(async () => APP_LOCK_FIXTURES.iphoneNoPasscode));
    show();
    const control = await select();
    expect(control.value).toBe('off');
    expect(control.disabled).toBe(true);
    expect(screen.getByText(i18n.t('settings.general.appLock.noPasscodePhone'))).toBeTruthy();
  });

  it('says the passcode unlocks when Face ID is not allowed, and the Mac password without Touch ID', async () => {
    iphone(vi.fn(async () => APP_LOCK_FIXTURES.iphoneFaceIdDenied));
    show();
    await select();
    expect(screen.getByText(i18n.t('settings.general.appLock.faceIdDenied'))).toBeTruthy();
    cleanup();
    delete (window as unknown as Record<string, unknown>).avibeNative;
    mac(vi.fn(async () => APP_LOCK_FIXTURES.macLidClosed));
    show();
    await select();
    expect(screen.getByText(i18n.t('settings.general.appLock.passwordOnlyMac'))).toBeTruthy();
    expect(screen.getByText(i18n.t('settings.general.appLock.descriptionMac'))).toBeTruthy();
  });
});
