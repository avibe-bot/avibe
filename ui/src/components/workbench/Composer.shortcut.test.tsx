/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { useEffect, useRef } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type VoiceStopped = (
  reason: 'finish' | 'abort' | 'error',
  metadata: { pendingSegmentCount: number },
) => void;

const voiceMocks = vi.hoisted(() => ({
  abort: vi.fn(),
  finish: vi.fn(),
  getUserMedia: vi.fn(),
  onStopped: undefined as VoiceStopped | undefined,
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
    constructor(options: { onStopped: VoiceStopped }) {
      voiceMocks.onStopped = options.onStopped;
    }

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
import { Composer, type ComposerHandle } from './Composer';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

type ComposerTestState = {
  disabled?: boolean;
  initialDraft?: string;
  mentions?: boolean;
  shortcutStartEnabled?: boolean;
};

const ComposerShortcutHarness = ({
  sessionId,
  state,
}: {
  sessionId: string;
  state: ComposerTestState;
}) => {
  const composerRef = useRef<ComposerHandle>(null);
  const shortcutStartEnabled = state.shortcutStartEnabled ?? true;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      composerRef.current?.handleVoiceShortcut(event, shortcutStartEnabled);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [shortcutStartEnabled]);

  return (
    <>
      <button type="button">Outside composer</button>
      <div role="dialog" aria-label="Foreground surface">
        <button type="button">Foreground control</button>
      </div>
      <Composer
        ref={composerRef}
        sessionId={sessionId}
        onSend={() => undefined}
        disabled={state.disabled}
        initialDraft={state.initialDraft}
        onSearchAgents={state.mentions ? async () => [] : undefined}
        onSearchSessions={state.mentions ? async () => [] : undefined}
      />
    </>
  );
};

const composerView = (
  sessionId = 'shortcut-session',
  state: ComposerTestState = {},
) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <ComposerShortcutHarness sessionId={sessionId} state={state} />
    </ToastProvider>
  </I18nextProvider>
);

const renderComposer = (
  sessionId = 'shortcut-session',
  state: ComposerTestState = {},
) => render(composerView(sessionId, state));

beforeEach(() => {
  window.localStorage.clear();
  voiceMocks.abort.mockReset();
  voiceMocks.finish.mockReset();
  voiceMocks.getUserMedia.mockReset();
  voiceMocks.onStopped = undefined;
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

  it('starts and finishes from anywhere in the Chat page without taking foreign focus', async () => {
    renderComposer('scoped-shortcut-session');
    const outside = screen.getByRole('button', { name: 'Outside composer' });
    await act(async () => undefined);
    outside.focus();

    fireEvent.keyDown(outside, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    expect(document.activeElement).toBe(outside);

    fireEvent.keyDown(outside, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
  });

  it('does not start behind a foreground surface but always finishes an active recording', async () => {
    const view = renderComposer('gated-shortcut-session');
    const outside = screen.getByRole('button', { name: 'Outside composer' });
    const foreground = screen.getByRole('button', { name: 'Foreground control' });
    await act(async () => undefined);

    fireEvent.keyDown(foreground, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();

    fireEvent.keyDown(outside, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    view.rerender(composerView('gated-shortcut-session', { shortcutStartEnabled: false }));

    fireEvent.keyDown(foreground, { code: 'KeyZ', altKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
  });

  it('does not start when the Chat page is not eligible', async () => {
    renderComposer('inactive-shortcut-session', { shortcutStartEnabled: false });
    await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);

    fireEvent.keyDown(screen.getByRole('button', { name: 'Outside composer' }), {
      code: 'KeyZ',
      altKey: true,
    });
    expect(voiceMocks.getUserMedia).not.toHaveBeenCalled();
  });

  it('keeps a configured editor chord through the complete voice flow', async () => {
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
    textbox.focus();

    fireEvent.keyDown(textbox, { code: 'KeyZ', key: 'z', ctrlKey: true });
    await waitFor(() => expect(voiceMocks.getUserMedia).toHaveBeenCalledOnce());
    expect(textbox.textContent).toBe('Keep this draft');

    const finish = await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    await waitFor(() => expect(document.activeElement).toBe(finish));
    fireEvent.keyDown(finish, { code: 'KeyZ', key: 'z', ctrlKey: true });
    expect(voiceMocks.finish).toHaveBeenCalledOnce();
    act(() => voiceMocks.onStopped?.('finish', { pendingSegmentCount: 0 }));
    await waitFor(() => expect(document.activeElement).toBe(textbox));
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

    // Recording startup crosses promises; flush its effects, including the
    // plain-Escape listener, before dispatching the next keyboard interaction.
    await act(async () => {
      fireEvent.keyDown(textbox, { code: 'Escape', key: 'Escape', altKey: true });
    });
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
