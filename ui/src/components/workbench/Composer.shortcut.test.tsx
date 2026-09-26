/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { useEffect, useRef } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type VoiceStopped = (
  reason: 'finish' | 'abort' | 'error',
  metadata: { pendingSegmentCount: number },
) => void;
type VoiceSegment = (blob: Blob, metadata: { durationMs: number; final: boolean }) => void;

const voiceMocks = vi.hoisted(() => ({
  abort: vi.fn(),
  finish: vi.fn(),
  getUserMedia: vi.fn(),
  onSegment: undefined as VoiceSegment | undefined,
  onStopped: undefined as VoiceStopped | undefined,
  pipelineStart: vi.fn(),
  realtimeAbort: vi.fn(),
  realtimeFinish: vi.fn(),
  realtimeStart: vi.fn(),
  realtimeOptions: [] as Array<{ reply?: string }>,
}));
vi.hoisted(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
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
    constructor(options: { onStopped: VoiceStopped; onSegment: VoiceSegment }) {
      voiceMocks.onSegment = options.onSegment;
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
    constructor(options: { reply?: string }) {
      voiceMocks.realtimeOptions.push(options);
    }

    start = voiceMocks.realtimeStart;
    finish = voiceMocks.realtimeFinish;
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
  onSend?: (text: string) => void;
  readLatestAgentReply?: () => string | undefined;
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
        onSend={state.onSend ?? (() => undefined)}
        disabled={state.disabled}
        initialDraft={state.initialDraft}
        onSearchAgents={state.mentions ? async () => [] : undefined}
        onSearchSessions={state.mentions ? async () => [] : undefined}
        readLatestAgentReply={state.readLatestAgentReply}
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
  voiceMocks.onSegment = undefined;
  voiceMocks.onStopped = undefined;
  voiceMocks.pipelineStart.mockReset().mockResolvedValue(true);
  voiceMocks.realtimeAbort.mockReset();
  voiceMocks.realtimeFinish.mockReset();
  voiceMocks.realtimeStart.mockReset().mockReturnValue(new Promise(() => undefined));
  voiceMocks.realtimeOptions = [];
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

  it.each([false, true].flatMap((mentions) => (
    ['editor', 'outside', 'moved', 'dialog'].map((focus) => ({ mentions, focus }))
  )))('restores shortcut focus and Enter sending without taking new focus ($mentions, $focus)', async ({
    mentions,
    focus,
  }) => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    let finishTranscription!: (result: { text: string; cleanup: string }) => void;
    voiceMocks.realtimeFinish.mockReturnValue(new Promise((resolve) => {
      finishTranscription = resolve;
    }));
    voiceMocks.finish.mockImplementation(() => {
      voiceMocks.onSegment?.(new Blob(['audio']), { durationMs: 1000, final: true });
      voiceMocks.onStopped?.('finish', { pendingSegmentCount: 0 });
    });
    renderComposer(`transcript-shortcut-${mentions}-${focus}`, { mentions, onSend });
    await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    const outside = screen.getByRole('button', { name: 'Outside composer' });
    const foreground = screen.getByRole('button', { name: 'Foreground control' });
    await user.click(focus === 'outside' ? outside : textbox);
    await user.keyboard('{Alt>}z{/Alt}');
    await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    if (focus === 'dialog') await user.click(foreground);
    await user.keyboard('{Alt>}z{/Alt}');
    await screen.findByRole('button', { name: en.chat.compose.voiceProcessing });
    if (focus === 'moved') await user.click(outside);
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();

    await act(async () => finishTranscription({ text: 'Send this transcript', cleanup: 'success' }));

    const expectedFocus = focus === 'moved' ? outside : focus === 'dialog' ? foreground : textbox;
    await waitFor(() => expect(document.activeElement).toBe(expectedFocus));
    if (expectedFocus !== textbox) return;
    if (mentions) {
      const selection = window.getSelection();
      expect(selection?.anchorNode?.textContent).toBe('Send this transcript');
      expect(selection?.focusNode?.textContent).toBe('Send this transcript');
      expect(selection?.anchorOffset).toBe('Send this transcript'.length);
      expect(selection?.focusOffset).toBe('Send this transcript'.length);
    }
    await user.keyboard('{Enter}');
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    expect(onSend.mock.calls[0][0]).toBe('Send this transcript');
  });

  it('preserves a plain-text insertion caret inside an existing draft', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    voiceMocks.realtimeFinish.mockResolvedValue({
      text: 'inserted text',
      cleanup: 'success',
    });
    voiceMocks.finish.mockImplementation(() => {
      voiceMocks.onSegment?.(new Blob(['audio']), { durationMs: 1000, final: true });
      voiceMocks.onStopped?.('finish', { pendingSegmentCount: 0 });
    });
    renderComposer('textarea-caret-session', {
      initialDraft: 'before after',
      onSend,
    });
    const textbox = await screen.findByRole('textbox');
    await waitFor(() => expect((textbox as HTMLTextAreaElement).value).toBe('before after'));
    textbox.focus();
    textbox.setSelectionRange('before '.length, 'before '.length);

    await user.keyboard('{Alt>}z{/Alt}');
    await screen.findByRole('button', { name: en.chat.compose.stopRecording });
    await user.keyboard('{Alt>}z{/Alt}');

    await waitFor(() => expect((textbox as HTMLTextAreaElement).value).toBe('before inserted text after'));
    await waitFor(() => expect(document.activeElement).toBe(textbox));
    const caret = 'before inserted text '.length;
    expect(textbox.selectionStart).toBe(caret);
    expect(textbox.selectionEnd).toBe(caret);

    await user.keyboard('{Enter}');
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    expect(onSend.mock.calls[0][0]).toBe('before inserted text after');
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

  it('gives realtime recognition the chat reply present when dictation starts', async () => {
    let latestReply = 'Run make release on prod-7.';
    renderComposer('reply-context-session', { readLatestAgentReply: () => latestReply });
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);

    fireEvent.click(mic);
    latestReply = 'A reply that arrived after dictation started';
    await waitFor(() => expect(voiceMocks.realtimeOptions).toHaveLength(1));

    expect(voiceMocks.realtimeOptions[0].reply).toBe('Run make release on prod-7.');
  });
});
