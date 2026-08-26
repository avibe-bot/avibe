/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const voiceMocks = vi.hoisted(() => ({
  abort: vi.fn(),
  finish: vi.fn(),
  getUserMedia: vi.fn(),
  pipelineStart: vi.fn(),
  realtimeAbort: vi.fn(),
  realtimeStart: vi.fn(),
}));
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});
const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/avibeFetch')>(),
  primeCloudToken: vi.fn(),
}));
vi.mock('../../lib/voiceRecording', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/voiceRecording')>(),
  VoiceRecordingPipeline: class {
    start = voiceMocks.pipelineStart;
    finish = voiceMocks.finish;
    abort = voiceMocks.abort;
  },
}));
vi.mock('../../lib/voiceRealtime', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/voiceRealtime')>(),
  VoiceRealtimeSession: class {
    start = voiceMocks.realtimeStart;
    abort = voiceMocks.realtimeAbort;
    sendPcm = vi.fn();
  },
}));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { Composer } from './Composer';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderComposer = (
  sessionId = 'shortcut-session',
  state: { disabled?: boolean; voiceShortcutActive?: boolean } = {},
) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer sessionId={sessionId} onSend={() => undefined} {...state} />
    </ToastProvider>
  </I18nextProvider>,
);

beforeEach(() => {
  window.localStorage.clear();
  voiceMocks.abort.mockReset();
  voiceMocks.finish.mockReset();
  voiceMocks.getUserMedia.mockReset();
  voiceMocks.pipelineStart.mockReset().mockResolvedValue(true);
  voiceMocks.realtimeAbort.mockReset();
  voiceMocks.realtimeStart.mockReset().mockReturnValue(new Promise(() => undefined));
  voiceMocks.getUserMedia.mockResolvedValue({
    getTracks: () => [{ stop: vi.fn() }],
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: voiceMocks.getUserMedia },
  });
  Object.defineProperty(navigator, 'platform', {
    configurable: true,
    value: 'Linux x86_64',
  });
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
  apiFetch.mockReset().mockResolvedValue({
    ok: true,
    json: async () => ({ available: true, max_file_bytes: 10_000_000 }),
  });
});

afterEach(cleanup);

describe('Composer voice shortcut', () => {
  it('starts and finishes voice input with the configured chord and advertises it on hover', async () => {
    renderComposer();
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    expect(mic.getAttribute('title')).toBe(
      'Press Alt+Z for voice input, press it again to finish, or press Esc to cancel',
    );

    await act(async () => undefined);
    fireEvent.keyDown(window, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    const finish = await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    expect(finish.getAttribute('title')).toBe(mic.getAttribute('title'));

    fireEvent.keyDown(window, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
  });

  it('leaves the shortcut with an open menu', async () => {
    renderComposer('menu-shortcut-session');
    await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);

    const menuTrigger = document.createElement('button');
    menuTrigger.setAttribute('aria-haspopup', 'menu');
    menuTrigger.setAttribute('aria-expanded', 'true');
    document.body.append(menuTrigger);
    fireEvent.keyDown(window, { code: 'KeyZ', altKey: true });

    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();
  });

  it('does not advertise or handle the shortcut when voice input is disabled', async () => {
    renderComposer('disabled-shortcut-session', { disabled: true });
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });

    expect(mic.hasAttribute('disabled')).toBe(true);
    expect(mic.getAttribute('title')).toBe(en.chat.compose.voice);
    fireEvent.keyDown(window, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();
  });
});
