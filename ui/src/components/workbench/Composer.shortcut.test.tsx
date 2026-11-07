/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { useEffect, useRef } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { VoiceContextSource } from '../../lib/voiceContext';

type VoiceStopped = (
  reason: 'finish' | 'abort' | 'error',
  metadata: { pendingSegmentCount: number },
) => void;
type VoiceSegment = (blob: Blob, metadata: { durationMs: number; final: boolean; unjudged?: boolean }) => void;

const voiceMocks = vi.hoisted(() => ({
  abort: vi.fn(),
  finish: vi.fn(),
  getUserMedia: vi.fn(),
  onSegment: undefined as VoiceSegment | undefined,
  onStopped: undefined as VoiceStopped | undefined,
  onError: undefined as ((error: unknown) => void) | undefined,
  onPcm: undefined as ((samples: Int16Array) => void) | undefined,
  trace: undefined as { entry: string; dictationId?: string; observePcm: (samples: Int16Array) => void } | undefined,
  onCaptureState: undefined as ((state: 'live' | 'recorder' | 'no_sound') => void) | undefined,
  pipelineStart: vi.fn(),
  realtimeAbort: vi.fn(),
  realtimeFinish: vi.fn(),
  realtimeStart: vi.fn(),
  realtimeOptions: [] as Array<{ context?: VoiceContextSource; onPreview?: (preview: { text: string; stash: string }) => void }>,
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
/** `ms` of the recorder's 16 kHz PCM at a steady loudness (RMS, 0–1). */
const pcm = (rms: number, ms: number) => new Int16Array(Math.round(16 * ms)).fill(Math.round(rms * 32_767));

vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/avibeFetch')>(),
  primeCloudToken: vi.fn(),
}));
vi.mock('../../lib/voiceRecording', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/voiceRecording')>(),
  VoiceRecordingPipeline: class {
    constructor(options: {
      onStopped: VoiceStopped;
      onSegment: VoiceSegment;
      onError?: (error: unknown) => void;
      onPcm?: (samples: Int16Array) => void;
      trace?: { entry: string; dictationId?: string; observePcm: (samples: Int16Array) => void };
      onCaptureState?: (state: 'live' | 'recorder' | 'no_sound') => void;
    }) {
      voiceMocks.trace = options.trace;
      voiceMocks.onCaptureState = options.onCaptureState;
      voiceMocks.onSegment = options.onSegment;
      voiceMocks.onStopped = options.onStopped;
      voiceMocks.onError = options.onError;
      // As the pipeline does: what it takes in goes into the take's trace, then to the take.
      voiceMocks.onPcm = (samples) => {
        options.trace?.observePcm(samples);
        options.onPcm?.(samples);
      };
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
  readVoiceContext?: () => VoiceContextSource;
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
        readVoiceContext={state.readVoiceContext}
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
  voiceMocks.onError = undefined;
  voiceMocks.onPcm = undefined;
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
  /** What the composer told the reliability log. */
  const telemetrySent = () => apiFetch.mock.calls
    .filter(([path]) => path === '/api/asr/telemetry')
    .map(([, init]) => JSON.parse((init as RequestInit).body as string) as Record<string, unknown>);

  it('traces a tap take from its microphone request, and logs a microphone that never opened', async () => {
    renderComposer();
    const textbox = await screen.findByRole('textbox');
    await act(async () => undefined);
    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.pipelineStart).toHaveBeenCalledOnce());
    expect(voiceMocks.trace).toMatchObject({ entry: 'tap', dictationId: expect.any(String) });
    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    await act(async () => voiceMocks.onStopped?.('abort', { pendingSegmentCount: 0 }));

    voiceMocks.getUserMedia.mockRejectedValueOnce(new DOMException('Permission denied', 'NotAllowedError'));
    fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(telemetrySent()).toContainEqual(expect.objectContaining({ event: 'capture_trace' })));
    expect(telemetrySent().filter((event) => event.event === 'capture_trace')).toEqual([expect.objectContaining({
      outcome: 'failed',
      entry: 'tap',
      captureClass: 'no_microphone',
      mic: 'NotAllowedError',
      dictationId: expect.any(String),
    })]);
  });

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

  it('gives realtime recognition the conversation present when dictation starts', async () => {
    let latest = { reply: 'Run make release on prod-7.', userPrevious: 'How do I deploy?' };
    renderComposer('reply-context-session', { readVoiceContext: () => latest });
    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);

    fireEvent.click(mic);
    latest = { reply: 'A reply that arrived after dictation started', userPrevious: 'later' };
    await waitFor(() => expect(voiceMocks.realtimeOptions).toHaveLength(1));

    await expect(voiceMocks.realtimeOptions[0].context).resolves.toEqual({
      reply: 'Run make release on prod-7.',
      userPrevious: 'How do I deploy?',
    });
  });

  it('gives ⌥Z dictation the same conversation', async () => {
    renderComposer('shortcut-context-session', {
      readVoiceContext: () => ({ reply: 'Ship it on Thursday.', userPrevious: 'When do we ship?' }),
    });
    await screen.findByRole('button', { name: en.chat.compose.voice });
    await act(async () => undefined);

    fireEvent.keyDown(screen.getByRole('textbox'), { code: 'KeyZ', altKey: true });
    await waitFor(() => expect(voiceMocks.realtimeOptions).toHaveLength(1));

    await expect(voiceMocks.realtimeOptions[0].context).resolves.toEqual({
      reply: 'Ship it on Thursday.',
      userPrevious: 'When do we ship?',
    });
  });

  describe('never drops what was recorded', () => {
    // The local relay answers; the cloud has no token (the compatibility path).
    const relayAnswers = (answer: () => Response) => {
      apiFetch.mockReset().mockImplementation(async (path: string) => {
        if (path === '/api/asr/transcribe') return answer();
        if (path === '/api/cloud/token') return Response.json({ error: 'cloud_unavailable' }, { status: 503 });
        if (path === '/api/asr/telemetry') return Response.json({ ok: true });
        return Response.json({ available: true, max_file_bytes: 10_000_000 });
      });
    };

    it('transcribes the audio recorded before the capture broke', async () => {
      relayAnswers(() => Response.json({ text: 'what was said before the call', cleanup: 'success' }));
      renderComposer('capture-broke-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);

      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });
      act(() => {
        voiceMocks.onError?.(new Error('voice microphone track ended unexpectedly'));
        voiceMocks.onSegment?.(new Blob(['audio']), { durationMs: 8_000, final: true });
        voiceMocks.onStopped?.('error', { pendingSegmentCount: 1 });
      });

      await waitFor(() => expect(textbox.value).toBe('what was said before the call'));
    });

    it('does not ask recognition about a dictation that held no speech', async () => {
      relayAnswers(() => Response.json({ text: 'Vibe, Vibey, Avibe voice input.', cleanup: 'success' }));
      renderComposer('silent-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);

      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });
      act(() => {
        voiceMocks.onPcm?.(pcm(0.003, 2_000));
        // A preview with no words says nothing about speech.
        voiceMocks.realtimeOptions[0]?.onPreview?.({ text: '', stash: ' ' });
        voiceMocks.onSegment?.(new Blob(['quiet room']), { durationMs: 2_000, final: true });
        voiceMocks.onStopped?.('finish', { pendingSegmentCount: 1 });
      });

      expect(await screen.findByText(en.chat.compose.voiceEmpty)).toBeTruthy();
      expect(apiFetch.mock.calls.map(([path]) => path)).not.toContain('/api/asr/transcribe');
      expect(textbox.value).toBe('');
      // A false block would show in the reliability log: the gate's evidence, nothing said.
      const gate = apiFetch.mock.calls
        .filter(([path]) => path === '/api/asr/telemetry')
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)))
        .find((event) => event.event === 'dictation_gated');
      expect(gate).toMatchObject({ outcome: 'empty', voicedReadings: 0, peakDbfs: -50 });
      expect(Object.keys(gate)).not.toContain('text');
    });

    it('leaves live recognition for a take recorded without Web Audio: its words come when it ends', async () => {
      renderComposer('recorder-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);
      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });

      act(() => voiceMocks.onCaptureState?.('recorder'));
      expect(voiceMocks.realtimeAbort).toHaveBeenCalledOnce();
      act(() => voiceMocks.onCaptureState?.('no_sound'));
      expect(voiceMocks.realtimeAbort).toHaveBeenCalledOnce();
    });

    it('says a microphone that delivered no sound did so, not that nothing was said', async () => {
      renderComposer('soundless-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);

      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });
      // Not one frame reached the take, and nothing was recorded without Web Audio either.
      act(() => {
        voiceMocks.onStopped?.('finish', { pendingSegmentCount: 0 });
      });

      expect(await screen.findByText(en.chat.compose.voiceNoSound)).toBeTruthy();
      expect(screen.queryByText(en.chat.compose.voiceEmpty)).toBeNull();
      const finalized = apiFetch.mock.calls
        .filter(([path]) => path === '/api/asr/telemetry')
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)))
        .find((event) => event.event === 'dictation_finalized');
      expect(finalized).toMatchObject({ outcome: 'empty', segmentCount: 0 });
    });

    it('does not judge audio this browser could not decode as silent', async () => {
      let answer = () => Response.json({ error: 'transcription_empty' }, { status: 422 });
      relayAnswers(() => answer());
      renderComposer('undecodable-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);

      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });
      act(() => {
        voiceMocks.onCaptureState?.('recorder');
        // What decoded was a quiet room; the rest could not be decoded, so its sound is unknown.
        voiceMocks.onPcm?.(pcm(0.003, 2_000));
        voiceMocks.onSegment?.(new Blob(['undecodable minute']), { durationMs: 60_000, final: true, unjudged: true });
        voiceMocks.onStopped?.('finish', { pendingSegmentCount: 1 });
      });

      // Recognition is asked, and an empty answer keeps the take for another try.
      const retry = await screen.findByRole('button', { name: en.chat.compose.voiceRetry });
      expect(apiFetch.mock.calls.map(([path]) => path)).toContain('/api/asr/transcribe');
      expect(screen.queryByText(en.chat.compose.voiceEmpty)).toBeNull();
      expect(await screen.findByText(en.chat.compose.voiceEmptyKept)).toBeTruthy();
      answer = () => Response.json({ text: 'said in the second minute', cleanup: 'success' });
      fireEvent.click(retry);
      await waitFor(() => expect(textbox.value).toBe('said in the second minute'));
    });

    it('keeps a take that held speech when nothing was recognized, and transcribes it again on retry', async () => {
      let answer = () => Response.json({ error: 'transcription_empty' }, { status: 422 });
      relayAnswers(() => answer());
      renderComposer('voiced-empty-session');
      await screen.findByRole('button', { name: en.chat.compose.voice });
      const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
      await act(async () => undefined);

      fireEvent.keyDown(textbox, { code: 'KeyZ', altKey: true });
      await screen.findByRole('button', { name: en.chat.compose.stopRecording });
      act(() => {
        voiceMocks.onPcm?.(pcm(0.05, 1_280));
        voiceMocks.onSegment?.(new Blob(['audio']), { durationMs: 3_000, final: true });
        voiceMocks.onStopped?.('finish', { pendingSegmentCount: 1 });
      });

      const retry = await screen.findByRole('button', { name: en.chat.compose.voiceRetry });
      expect(await screen.findByText(en.chat.compose.voiceEmptyKept)).toBeTruthy();
      answer = () => Response.json({ text: 'recognized the second time', cleanup: 'success' });
      fireEvent.click(retry);
      await waitFor(() => expect(textbox.value).toBe('recognized the second time'));
    });
  });
});

