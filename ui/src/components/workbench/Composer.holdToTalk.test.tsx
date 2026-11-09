/* @vitest-environment jsdom */

// Holding the empty composer to talk (ui/design: composer/hold-desktop/*, composer/hold-mobile/*).
// The gesture, the microphone, and the transcript are `useHoldToTalk`'s (its own tests cover
// them); these check what the composer decides: when a press may become a hold, which numbers a
// mouse and a finger get, what a spoken message is sent with, and what the composer shows.

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HoldToTalkOptions } from '../voice/useHoldToTalk';

vi.hoisted(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

const apiFetch = vi.hoisted(() => vi.fn());
const nativeHapticImpact = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../../lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/native')>()),
  nativeHapticImpact,
}));
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/avibeFetch')>(),
  primeCloudToken: vi.fn(),
}));

type Phase = 'idle' | 'pressing' | 'listening' | 'recording' | 'armed' | 'settling' | 'interrupted';
const hookMock = vi.hoisted(() => ({
  options: null as HoldToTalkOptions | null,
  state: {} as Record<string, unknown>,
  bind: {
    onPointerDown: vi.fn(),
    onPointerMove: vi.fn(),
    onPointerUp: vi.fn(),
    onPointerCancel: vi.fn(),
    onLostPointerCapture: vi.fn(),
    onContextMenu: vi.fn((event: { preventDefault: () => void }) => event.preventDefault()),
    onClick: vi.fn(),
  },
  discard: vi.fn(),
  sendInterrupted: vi.fn(),
  retry: vi.fn(),
  dismiss: vi.fn(),
}));
vi.mock('../voice/useHoldToTalk', async (importOriginal) => ({
  ...await importOriginal<typeof import('../voice/useHoldToTalk')>(),
  useHoldToTalk: (options: HoldToTalkOptions) => {
    hookMock.options = options;
    return {
      phase: 'idle' as Phase,
      interruptReason: null,
      text: '',
      stash: '',
      seconds: 0,
      progress: 0,
      outcome: null,
      available: true,
      holdable: true,
      canRetry: false,
      bind: { ...hookMock.bind, style: undefined },
      sendInterrupted: hookMock.sendInterrupted,
      retry: hookMock.retry,
      discard: hookMock.discard,
      dismiss: hookMock.dismiss,
      ...hookMock.state,
    };
  },
}));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { VOICE_MESSAGE_METADATA } from '../../lib/voiceMessage';
import { Composer, type ComposerAttachment } from './Composer';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

type Props = Partial<React.ComponentProps<typeof Composer>>;

const view = (props: Props = {}) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer sessionId="ses_hold" layout="stacked" onSend={() => undefined} {...props} />
    </ToastProvider>
  </I18nextProvider>
);

const renderComposer = (props: Props = {}) => render(view(props));
const box = () => document.querySelector<HTMLElement>('[data-hold-to-talk], .composer-edge')!;
const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement;

const asrAvailable = async () => {
  // The composer reads speech availability once; until it answers, nothing may be held.
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
  await waitFor(() => expect(box().getAttribute('data-hold-to-talk')).toBe('ready'));
};

beforeEach(() => {
  hookMock.options = null;
  hookMock.state = {};
  for (const fn of [...Object.values(hookMock.bind), hookMock.discard, hookMock.sendInterrupted, hookMock.retry, hookMock.dismiss]) fn.mockClear();
  apiFetch.mockReset().mockImplementation(async (path: string) => {
    if (path === '/api/asr/status') return new Response(JSON.stringify({ available: true }), { status: 200 });
    return new Response('{}', { status: 200 });
  });
  nativeHapticImpact.mockReset().mockResolvedValue(true);
});

afterEach(() => cleanup());

describe('when a press may become a hold', () => {
  it('lets a mouse hold an empty composer, focused or not, and says so in the placeholder', async () => {
    renderComposer();
    await asrAvailable();
    expect(textarea().placeholder).toBe(en.voice.composer.placeholder);
    expect(hookMock.options!.canHold!('mouse')).toBe(true);
    textarea().focus();
    expect(hookMock.options!.canHold!('mouse')).toBe(true);
  });

  it('lets a finger hold only while the keyboard is down', async () => {
    renderComposer();
    await asrAvailable();
    expect(hookMock.options!.canHold!('touch')).toBe(true);
    act(() => textarea().focus());
    expect(hookMock.options!.canHold!('touch')).toBe(false);
    expect(hookMock.options!.canHold!('pen')).toBe(false);
  });

  it('gives the press back to the system once there is text', async () => {
    renderComposer();
    await asrAvailable();
    fireEvent.change(textarea(), { target: { value: 'hello' } });
    expect(hookMock.options!.canHold!('mouse')).toBe(false);
    expect(hookMock.options!.canHold!('touch')).toBe(false);
    expect(textarea().placeholder).toBe(en.chat.compose.placeholder);
    expect(box().getAttribute('data-hold-to-talk')).toBeNull();
  });

  it('holds nothing without speech recognition, and shows no hint', async () => {
    apiFetch.mockImplementation(async () => new Response(JSON.stringify({ available: false }), { status: 200 }));
    renderComposer();
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
    expect(hookMock.options!.canHold!('mouse')).toBe(false);
    expect(textarea().placeholder).toBe(en.chat.compose.placeholder);
  });

  it('holds nothing while the composer is disabled', async () => {
    renderComposer({ disabled: true });
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
    expect(hookMock.options!.canHold!('mouse')).toBe(false);
  });

  it('uses the caller’s hold placeholder where it overrides the idle one', async () => {
    renderComposer({ placeholder: 'Tell the agent what you want…', holdPlaceholder: 'Tell the agent, or hold to talk' });
    await asrAvailable();
    expect(textarea().placeholder).toBe('Tell the agent, or hold to talk');
  });

  it('keeps the busy placeholder while a turn runs', async () => {
    renderComposer({ busy: true, busyPlaceholder: 'Chief Security Officer is working…' });
    await asrAvailable();
    expect(textarea().placeholder).toBe('Chief Security Officer is working…');
  });
});

describe('the gesture', () => {
  it('gives a mouse the longer hold with a ring and leave-to-cancel, and a finger the defaults', async () => {
    renderComposer();
    await asrAvailable();
    const gesture = hookMock.options!.gesture as (pointerType: string) => object;
    expect(gesture('mouse')).toEqual({ holdMs: 350, ringDelayMs: 100, slopPx: 6, cancel: 'leave', cancelPx: 16, uncancelPx: 0 });
    expect(gesture('touch')).toEqual({});
    expect(hookMock.options!.onSystemInterrupt).toBe('interrupt');
  });

  it("hears a take in the composer's own conversation", async () => {
    const context = { reply: 'Ship it Thursday.', userPrevious: 'When do we ship?' };
    renderComposer({ readVoiceContext: () => context });
    await asrAvailable();
    expect(hookMock.options!.context!()).toBe(context);
  });

  it('measures leave-to-cancel against the whole box', async () => {
    renderComposer();
    await asrAvailable();
    const rect = new DOMRect(10, 20, 300, 90);
    box().getBoundingClientRect = () => rect;
    expect(hookMock.options!.bounds!()).toBe(rect);
  });

  it('never takes a press on one of the box’s controls', async () => {
    renderComposer();
    await asrAvailable();
    fireEvent.pointerDown(screen.getByRole('button', { name: en.chat.compose.attach }), { button: 0, pointerType: 'mouse' });
    expect(hookMock.bind.onPointerDown).not.toHaveBeenCalled();
    fireEvent.pointerDown(textarea(), { button: 0, pointerType: 'mouse' });
    expect(hookMock.bind.onPointerDown).toHaveBeenCalledTimes(1);
  });

  it('leaves the right-click menu alone at rest', async () => {
    renderComposer();
    await asrAvailable();
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    textarea().dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(hookMock.bind.onContextMenu).not.toHaveBeenCalled();
  });

  it('keeps the system menu away during a live press', async () => {
    hookMock.state = { phase: 'recording' };
    renderComposer();
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    textarea().dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it('focuses the input on a tap', async () => {
    renderComposer();
    await asrAvailable();
    act(() => hookMock.options!.onTap!());
    expect(document.activeElement).toBe(textarea());
  });
});

describe('sending what was said', () => {
  it('sends the text marked as spoken, with the staged files, and clears them', async () => {
    const onSend = vi.fn<(text: string, attachments?: ComposerAttachment[], references?: unknown, metadata?: Record<string, unknown>) => boolean>(() => true);
    const { container } = renderComposer({ onSend, stageMedia: true, sessionId: undefined });
    await asrAvailable();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(['png'], 'scan.png', { type: 'image/png' });
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:scan');
    globalThis.URL.revokeObjectURL = vi.fn();
    fireEvent.change(input, { target: { files: [file] } });
    await screen.findByText('scan.png');
    let accepted = false;
    await act(async () => {
      accepted = await hookMock.options!.send('Fix the token refresh first.', VOICE_MESSAGE_METADATA);
    });
    expect(accepted).toBe(true);
    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, attachments, references, metadata] = onSend.mock.calls[0];
    expect(text).toBe('Fix the token refresh first.');
    expect(attachments?.map((attachment) => attachment.name)).toEqual(['scan.png']);
    expect(references).toBeUndefined();
    expect(metadata).toEqual({ input: 'voice' });
    await waitFor(() => expect(screen.queryByText('scan.png')).toBeNull());
    expect(nativeHapticImpact).not.toHaveBeenCalled();
  });

  it('reports a refused send to the hook, which keeps the words', async () => {
    renderComposer({ onSend: () => false });
    await asrAvailable();
    let accepted = true;
    await act(async () => {
      accepted = await hookMock.options!.send('Hello', VOICE_MESSAGE_METADATA);
    });
    expect(accepted).toBe(false);
  });

  it('reports a send that threw as refused', async () => {
    renderComposer({ onSend: () => { throw new Error('offline'); } });
    await asrAvailable();
    await expect(hookMock.options!.send('Hello', VOICE_MESSAGE_METADATA)).resolves.toBe(false);
  });
});

describe('typed send haptics', () => {
  it('fires after a typed send is accepted', async () => {
    const onSend = vi.fn(() => true);
    renderComposer({ onSend });
    fireEvent.change(textarea(), { target: { value: 'Ship it' } });

    fireEvent.click(screen.getByRole('button', { name: en.chat.compose.send }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith('Ship it', [], undefined));
    expect(nativeHapticImpact).toHaveBeenCalledWith('light');
  });

  it('does not fire when the send is refused', async () => {
    renderComposer({ onSend: () => false });
    fireEvent.change(textarea(), { target: { value: 'Try again' } });

    fireEvent.click(screen.getByRole('button', { name: en.chat.compose.send }));
    await waitFor(() => expect(textarea().value).toBe('Try again'));
    expect(nativeHapticImpact).not.toHaveBeenCalled();
  });
});

describe('what the composer shows', () => {
  it('shows the take above the composer and the box as the button being held', async () => {
    hookMock.state = { phase: 'recording', text: 'Fix the token refresh, ', stash: 'then run the regression', seconds: 6 };
    renderComposer({ voiceTarget: { name: 'Chief Security Officer', avatar: null } });
    expect(await screen.findByText('To Chief Security Officer')).toBeTruthy();
    expect(screen.getByText('Fix the token refresh,')).toBeTruthy();
    expect(screen.getAllByText(en.voice.composer.releaseToSend).length).toBeGreaterThan(0);
  });

  it('puts an interrupted take in the draft to edit, and lets the take go', async () => {
    const onDraftChange = vi.fn();
    hookMock.state = { phase: 'interrupted', interruptReason: 'dropped', text: 'Fix the token refresh first' };
    renderComposer({ onDraftChange, voiceTarget: { name: 'Fix the cache', title: 'To “Fix the cache”' } });
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
    fireEvent.click(screen.getByRole('button', { name: en.voice.composer.editFirst }));
    expect(textarea().value).toBe('Fix the token refresh first');
    expect(onDraftChange).toHaveBeenLastCalledWith('Fix the token refresh first');
    expect(hookMock.discard).toHaveBeenCalledTimes(1);
  });

  it('offers retry only when the recording can be transcribed again', async () => {
    hookMock.state = { phase: 'interrupted', interruptReason: 'failed', text: 'Hello', canRetry: false };
    const { rerender } = renderComposer();
    expect(screen.queryByRole('button', { name: en.voice.retry })).toBeNull();
    hookMock.state = { ...hookMock.state, canRetry: true };
    rerender(view());
    fireEvent.click(screen.getByRole('button', { name: en.voice.retry }));
    expect(hookMock.retry).toHaveBeenCalledTimes(1);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: en.voice.send }));
    expect(hookMock.sendInterrupted).toHaveBeenCalledTimes(1);
  });

  it('turns the mic button off during a hold, so a tap dictation cannot start over it', async () => {
    hookMock.state = { phase: 'listening' };
    renderComposer();
    await waitFor(() => expect(screen.getByRole('button', { name: en.chat.compose.voice })).toHaveProperty('disabled', true));
  });

  it('says what a press that sent nothing led to', async () => {
    hookMock.state = { outcome: 'denied' };
    renderComposer();
    expect(await screen.findByText(en.voice.deniedTitle)).toBeTruthy();
    expect(screen.getByText(en.voice.composer.deniedBrowser)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: en.voice.dismiss }));
    expect(hookMock.dismiss).toHaveBeenCalled();
  });
});

describe('one send at a time', () => {
  it('keeps Send and Enter off while a take owns the box, so staged files go with one message', async () => {
    hookMock.state = { phase: 'settling', text: 'Look at this' };
    const onSend = vi.fn(() => true);
    const { container } = renderComposer({ onSend });
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/asr/status'));
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:scan');
    globalThis.URL.revokeObjectURL = vi.fn();
    fireEvent.change(container.querySelector<HTMLInputElement>('input[type="file"]')!, {
      target: { files: [new File(['png'], 'scan.png', { type: 'image/png' })] },
    });
    await screen.findByText('scan.png');
    expect(screen.getByRole('button', { name: en.chat.compose.send })).toHaveProperty('disabled', true);
    fireEvent.keyDown(textarea(), { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
  });

  it('refuses a spoken send while another send is on its way', async () => {
    let release!: (value: boolean) => void;
    const onSend = vi.fn(() => new Promise<boolean>((resolve) => { release = resolve; }));
    renderComposer({ onSend });
    await asrAvailable();
    let first!: Promise<boolean>;
    act(() => {
      first = hookMock.options!.send('First', VOICE_MESSAGE_METADATA);
    });
    await expect(hookMock.options!.send('Second', VOICE_MESSAGE_METADATA)).resolves.toBe(false);
    await act(async () => {
      release(true);
      await first;
    });
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});

describe('the system’s touch gestures', () => {
  const touchStart = (target: Element) => {
    const event = new Event('touchstart', { bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    return event;
  };

  it('keeps a touch the hold took away from the system, even once the hold has started', async () => {
    const { rerender } = renderComposer();
    await asrAvailable();
    fireEvent.pointerDown(textarea(), { button: 0, pointerType: 'touch' });
    // The hold's own state lands before the touch's touchstart.
    hookMock.state = { phase: 'pressing' };
    rerender(view());
    expect(touchStart(textarea()).defaultPrevented).toBe(true);
    // And the touch styles stay on through the press (jsdom keeps only the standard one).
    expect(box().style.touchAction).toBe('none');
  });

  it('lights the box for a finger only once the press outlasts the ring delay, so a tap does not flash', async () => {
    const { rerender } = renderComposer();
    await asrAvailable();
    fireEvent.pointerDown(textarea(), { button: 0, pointerType: 'touch' });
    hookMock.state = { phase: 'pressing', progress: 0 };
    rerender(view());
    expect(document.querySelector('.bg-mint\\/10')).toBeNull();
    hookMock.state = { phase: 'pressing', progress: 0.3 };
    rerender(view());
    expect(document.querySelector('.bg-mint\\/10')).not.toBeNull();
  });

  it('leaves a touch the hold refused to the system', async () => {
    renderComposer();
    await asrAvailable();
    act(() => textarea().focus());
    fireEvent.pointerDown(textarea(), { button: 0, pointerType: 'touch' });
    expect(touchStart(textarea()).defaultPrevented).toBe(false);
    fireEvent.pointerDown(screen.getByRole('button', { name: en.chat.compose.attach }), { button: 0, pointerType: 'touch' });
    expect(touchStart(screen.getByRole('button', { name: en.chat.compose.attach })).defaultPrevented).toBe(false);
  });
});

describe('a refused spoken send', () => {
  it('stays with the take while the composer is here', async () => {
    const onDraftChange = vi.fn();
    renderComposer({ onSend: () => false, onDraftChange });
    await asrAvailable();
    await expect(hookMock.options!.send('Keep these words', VOICE_MESSAGE_METADATA)).resolves.toBe(false);
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it('goes to the session’s draft once the user has left the conversation', async () => {
    let refuse!: (value: boolean) => void;
    const onDraftChange = vi.fn();
    const { unmount } = renderComposer({ onSend: () => new Promise<boolean>((resolve) => { refuse = resolve; }), onDraftChange });
    await asrAvailable();
    const pending = hookMock.options!.send('Keep these words', VOICE_MESSAGE_METADATA);
    unmount();
    refuse(false);
    await expect(pending).resolves.toBe(false);
    expect(onDraftChange).toHaveBeenLastCalledWith('Keep these words');
  });
});
