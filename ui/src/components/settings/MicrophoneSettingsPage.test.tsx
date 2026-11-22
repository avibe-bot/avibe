/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import type { VoiceInputTest } from '@/lib/voiceInputTest';
import { MicrophoneSettingsView, SettingsMicrophonePage } from './MicrophoneSettingsPage';

const startVoiceInputTest = vi.hoisted(() => vi.fn());
vi.mock('@/lib/voiceInputTest', () => ({ startVoiceInputTest }));
vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderWithI18n = (element: React.ReactElement) => render(
  <I18nextProvider i18n={i18n}>{element}</I18nextProvider>,
);

const testCapture = (): VoiceInputTest => ({
  level: { read: () => 0, set: () => {}, reset: () => {} },
  stream: { getTracks: () => [] } as unknown as MediaStream,
  stop: vi.fn(),
});

beforeEach(() => {
  window.localStorage.clear();
  startVoiceInputTest.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('MicrophoneSettingsView', () => {
  it('keeps the test explicit and exposes the selected input control', () => {
    const onStart = vi.fn();
    renderWithI18n(
      <MicrophoneSettingsView
        platform="desktop"
        state="selected"
        selection="usb"
        devices={[{ deviceId: 'usb', label: 'USB microphone' }]}
        missing={false}
        onStart={onStart}
      />,
    );

    expect(screen.getByText('USB microphone')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start test' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Start test' }));
    expect(onStart).toHaveBeenCalledOnce();
  });
});

describe('SettingsMicrophonePage', () => {
  it('does not open the microphone when the page mounts', () => {
    renderWithI18n(<SettingsMicrophonePage />);

    expect(startVoiceInputTest).not.toHaveBeenCalled();
  });

  // The page can retire while the browser permission/capture promise is pending.
  // The existing immediate-resolution test covers only an already acquired stream.
  it('stops a capture that resolves after the settings page unmounts', async () => {
    const capture = testCapture();
    let resolve!: (capture: VoiceInputTest) => void;
    startVoiceInputTest.mockReturnValue(new Promise<VoiceInputTest>((done) => { resolve = done; }));
    const view = renderWithI18n(<SettingsMicrophonePage />);
    fireEvent.click(screen.getByRole('button', { name: 'Start test' }));
    expect(startVoiceInputTest).toHaveBeenCalledOnce();
    view.unmount();
    await act(async () => { resolve(capture); });
    expect(capture.stop).toHaveBeenCalledOnce();
  });

  it.each(['resolve', 'reject'] as const)('retires hidden pending acquisition and fences its late %s from a successor', async (outcome) => {
    const retired = testCapture();
    const successor = testCapture();
    let resolve!: (capture: VoiceInputTest) => void;
    let reject!: (error: Error) => void;
    startVoiceInputTest.mockReturnValueOnce(new Promise<VoiceInputTest>((done, fail) => {
      resolve = done;
      reject = fail;
    })).mockResolvedValueOnce(successor);
    renderWithI18n(<SettingsMicrophonePage />);
    const start = screen.getByRole('button', { name: 'Start test' });
    fireEvent.click(start);
    fireEvent.click(start);
    expect(startVoiceInputTest).toHaveBeenCalledOnce();
    const callbacks = startVoiceInputTest.mock.calls[0][0];
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    fireEvent(document, new Event('visibilitychange'));
    hidden.mockReturnValue(false);
    fireEvent.click(screen.getByRole('button', { name: 'Start test' }));
    await screen.findByRole('button', { name: 'Stop test' });
    await act(async () => {
      callbacks.onDeviceFallback();
      callbacks.onStreamEnded();
      if (outcome === 'resolve') resolve(retired);
      else reject(new DOMException('late refusal', 'NotAllowedError'));
    });
    expect(retired.stop).toHaveBeenCalledTimes(outcome === 'resolve' ? 1 : 0);
    expect(successor.stop).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Stop test' })).toBeTruthy();
    expect(screen.queryByText(en.settings.microphone.missingTitle)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop test' }));
    expect(successor.stop).toHaveBeenCalledOnce();
  });

  it('starts and stops the local test through the shared capture helper', async () => {
    const capture = testCapture();
    startVoiceInputTest.mockResolvedValue(capture);
    renderWithI18n(<SettingsMicrophonePage />);

    fireEvent.click(screen.getByRole('button', { name: 'Start test' }));
    await waitFor(() => expect(startVoiceInputTest).toHaveBeenCalledOnce());
    expect(await screen.findByRole('button', { name: 'Stop test' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Stop test' }));
    expect(capture.stop).toHaveBeenCalledOnce();
  });
});
