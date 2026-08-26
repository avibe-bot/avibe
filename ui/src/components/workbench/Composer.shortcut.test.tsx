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
import {
  defaultActionShortcuts,
  writeActionShortcuts,
} from '../../lib/actionShortcuts';
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
  state: { disabled?: boolean; initialDraft?: string; mentions?: boolean } = {},
) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer
        sessionId={sessionId}
        onSend={() => undefined}
        disabled={state.disabled}
        initialDraft={state.initialDraft}
        onSearchAgents={state.mentions ? async () => [] : undefined}
        onSearchSessions={state.mentions ? async () => [] : undefined}
      />
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
    const textbox = screen.getByRole('textbox');

    await act(async () => undefined);
    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    const finish = await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    expect(finish.getAttribute('title')).toBe(mic.getAttribute('title'));

    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
  });

  it('does not handle the shortcut outside the composer', async () => {
    renderComposer('scoped-shortcut-session');
    const textbox = screen.getByRole('textbox');
    await act(async () => undefined);

    fireEvent.keyDown(window, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();

    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
  });

  it('owns a configured editor chord before Lexical handles it', async () => {
    const shortcuts = defaultActionShortcuts();
    shortcuts.voiceInput = {
      code: 'KeyZ',
      altKey: false,
      ctrlKey: true,
      metaKey: false,
      shiftKey: false,
    };
    writeActionShortcuts(shortcuts);
    renderComposer('editor-shortcut-session', { mentions: true, initialDraft: 'Keep this draft' });
    await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    await waitFor(() => expect(textbox.textContent).toBe('Keep this draft'));
    await act(async () => undefined);

    fireEvent.keyDown(textbox, { code: 'KeyZ', key: 'z', ctrlKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    expect(textbox.textContent).toBe('Keep this draft');
  });

  it('yields the voice shortcut while the mention picker is open', async () => {
    renderComposer('mention-picker-shortcut-session', { mentions: true });
    await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    const picker = document.createElement('ul');
    picker.dataset.mentionPicker = '';
    textbox.parentElement?.append(picker);
    await act(async () => undefined);

    fireEvent.keyDown(textbox, { code: 'KeyZ', key: 'z', altKey: true });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();
  });

  it('keeps shortcut ownership after the microphone control changes to finish', async () => {
    renderComposer('clicked-shortcut-session');
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);
    mic.focus();

    fireEvent.click(mic);
    const finish = await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    await waitFor(() => expect(document.activeElement).toBe(finish));

    fireEvent.keyDown(finish, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
  });

  it('does not advertise or handle the shortcut when voice input is disabled', async () => {
    renderComposer('disabled-shortcut-session', { disabled: true });
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });

    expect(mic.hasAttribute('disabled')).toBe(true);
    expect(mic.getAttribute('title')).toBe(en.chat.compose.voice);
    fireEvent.keyDown(screen.getByRole('textbox'), { code: 'KeyZ', altKey: true });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();
  });

  it('finishes a modified Escape shortcut while plain Escape still cancels', async () => {
    const shortcuts = defaultActionShortcuts();
    shortcuts.voiceInput = {
      code: 'Escape',
      altKey: true,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    };
    writeActionShortcuts(shortcuts);
    renderComposer('escape-shortcut-session');
    await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    await act(async () => undefined);

    fireEvent.keyDown(textbox, { code: 'Escape', key: 'Escape', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    fireEvent.keyDown(textbox, { code: 'Escape', key: 'Escape', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
    expect(voiceMocks.abort).not.toHaveBeenCalled();

    voiceMocks.finish.mockReset();
    fireEvent.keyDown(window, { code: 'Escape', key: 'Escape' });
    expect(voiceMocks.abort).toHaveBeenCalledOnce();
  });
});
