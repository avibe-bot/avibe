/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NATIVE_HOLD_GONE } from '../../lib/nativeVoiceCapture';
import { NATIVE_HOLD_GRACE_MS } from '../../lib/nativeVoiceHold';
import { answerVoiceConsent, closeVoiceConsent, noteVoiceStatus, voiceConsentAsking } from '../../lib/voiceConsent';
import { VoiceTranscriptionError } from '../../lib/voiceTranscription';
import { MobileBotCenter } from '../MobileChrome';
import { MOUSE_HOLD_GESTURE, useHoldToTalk, type HoldToTalkOptions } from './useHoldToTalk';

// The hold's whole path (ui/design: mobile/main-bot-voice/*) against a stand-in for the
// dictation pipeline: what a press becomes, what is sent, and what is kept when a take
// is cut off. The pipeline itself is ShowPageVoiceDictation's own test.

type Fake = {
  input: {
    onPreviewParts?: (preview: { text: string; stash: string }) => void;
    context?: unknown;
    onLevel?: (rms: number) => void;
    onCaptureState?: (state: 'live' | 'recorder' | 'no_sound') => void;
    nativeCapture?: boolean | { adopt: string };
    pressedAt?: number;
    holdMiss?: string;
  };
  done: Promise<string>;
  resolve: (text: string) => void;
  reject: (error: unknown) => void;
  start: ReturnType<typeof vi.fn>;
  finish: ReturnType<typeof vi.fn>;
  abort: ReturnType<typeof vi.fn>;
  canRetry: ReturnType<typeof vi.fn>;
  retry: ReturnType<typeof vi.fn>;
  voiced: ReturnType<typeof vi.fn>;
  id: string;
  captureClass: ReturnType<typeof vi.fn>;
  landed: ReturnType<typeof vi.fn>;
  detach: ReturnType<typeof vi.fn>;
  setAside: ReturnType<typeof vi.fn>;
};

const pipeline = vi.hoisted(() => ({
  takes: [] as Fake[],
  asr: { available: true, consent: 'granted' as 'granted' | 'declined' | null, appHold: true },
  permission: 'granted' as PermissionState,
  captureActive: false,
  /** How long the microphone and audio graph take to open. */
  startMs: 0,
  startError: null as unknown,
  /** What the dictation decides when the reader stops waiting: keep the take, or discard it. */
  keepOnSetAside: true,
}));
const native = vi.hoisted(() => ({
  impact: vi.fn(),
  notify: vi.fn(),
  selection: vi.fn(),
  permission: vi.fn(),
  capture: vi.fn(() => false),
  prepare: vi.fn(async () => true),
  /** The App recognises holds itself (`voiceHold`), what it answers to a list of surfaces, the
   *  takes the page refused, and what listens to its word about a press. */
  hold: vi.fn(() => false),
  surfaces: vi.fn(async (_surfaces: { id: string }[], _holdMs: number) => 'ok'),
  reject: vi.fn(async (_take: string) => undefined),
  holdListeners: [] as ((event: { phase: string; take: string; surface?: string; step?: string }) => void)[],
}));

vi.mock('../../lib/showPageVoiceDictation', () => ({
  ShowPageVoiceDictation: class {
    constructor(input: Fake['input']) {
      let resolve!: (text: string) => void;
      let reject!: (error: unknown) => void;
      const done = new Promise<string>((ok, no) => {
        resolve = ok;
        reject = no;
      });
      void done.catch(() => undefined);
      const take: Fake = {
        input,
        done,
        resolve,
        reject,
        start: vi.fn(() => {
          const { startError, startMs } = pipeline;
          return new Promise<void>((ready, refuse) => setTimeout(() => {
            if (startError) refuse(startError);
            else ready();
          }, startMs));
        }),
        finish: vi.fn(),
        abort: vi.fn(() => reject(new VoiceTranscriptionError('cancelled'))),
        canRetry: vi.fn(() => true),
        retry: vi.fn(async () => 'heard again'),
        voiced: vi.fn(() => false),
        id: `take-${pipeline.takes.length + 1}`,
        captureClass: vi.fn(() => 'sound'),
        landed: vi.fn(),
        detach: vi.fn(),
        // As the dictation does: its requests stop, `done` rejects as cancelled, and it says
        // whether the take was kept.
        setAside: vi.fn(async () => {
          reject(new VoiceTranscriptionError('cancelled'));
          return pipeline.keepOnSetAside;
        }),
      };
      pipeline.takes.push(take);
      return take;
    }
  },
}));
const telemetry = vi.hoisted(() => ({ emit: vi.fn() }));
vi.mock('../../lib/voiceTelemetry', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/voiceTelemetry')>()),
  emitVoiceTelemetry: telemetry.emit,
}));
vi.mock('../../lib/apiFetch', () => ({
  apiFetch: vi.fn(async () => ({
    ok: true,
    json: async () => ({ available: pipeline.asr.available, consent: pipeline.asr.consent, app_hold: pipeline.asr.appHold }),
  })),
}));
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/avibeFetch')>()),
  primeCloudToken: vi.fn(),
}));
vi.mock('../../lib/voiceRecording', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/voiceRecording')>()),
  prewarmVoiceCapture: vi.fn(async () => undefined),
  isVoiceCaptureActive: () => pipeline.captureActive,
}));
vi.mock('../../lib/native', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/native')>()),
  nativeHapticImpact: native.impact,
  nativeHapticNotify: native.notify,
  nativeHapticSelection: native.selection,
  nativeVoicePermission: native.permission,
  hasNativeVoiceCapture: native.capture,
  nativeVoiceCapturePrepare: native.prepare,
  hasNativeVoiceHold: native.hold,
  nativeVoiceHoldSurfaces: native.surfaces,
  nativeVoiceHoldReject: native.reject,
  onNativeVoiceHold: (handler: (typeof native.holdListeners)[number]) => {
    native.holdListeners.push(handler);
    return () => {
      native.holdListeners = native.holdListeners.filter((listener) => listener !== handler);
    };
  },
}));

class PointerEventShim extends MouseEvent {
  pointerId: number;
  pointerType: string;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? 'touch';
  }
}
vi.stubGlobal('PointerEvent', PointerEventShim);

// The latest render's hook value, published from an effect as a test probe reads it.
const probe: { current: ReturnType<typeof useHoldToTalk> | null } = { current: null };
function Harness(props: HoldToTalkOptions) {
  const value = useHoldToTalk(props);
  useLayoutEffect(() => {
    probe.current = value;
  });
  const { attachSurface, bind } = value;
  return <button type="button" data-testid="held" ref={attachSurface} {...bind} />;
}
const hold = new Proxy({} as ReturnType<typeof useHoldToTalk>, {
  get: (_, key: keyof ReturnType<typeof useHoldToTalk>) => probe.current![key],
});

const send = vi.fn<HoldToTalkOptions['send']>();
const onTap = vi.fn();

async function mount(options: Partial<HoldToTalkOptions> = {}) {
  render(<Harness send={send} onTap={onTap} {...options} />);
  await act(async () => undefined); // the ASR status read
  return screen.getByTestId('held');
}
const flush = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});
const down = (el: HTMLElement, y = 500, pointerType = 'touch') => fireEvent.pointerDown(el, { button: 0, pointerId: 1, clientX: 100, clientY: y, pointerType });
const move = (el: HTMLElement, y: number, x = 100) => fireEvent.pointerMove(el, { pointerId: 1, clientX: x, clientY: y });
const up = (el: HTMLElement, y = 500) => fireEvent.pointerUp(el, { pointerId: 1, clientX: 100, clientY: y });
async function holdFor(el: HTMLElement, ms = 200) {
  down(el);
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
  await flush();
  // The microphone opens on a timer of its own (pipeline.startMs, 0 by default).
  await act(async () => {
    vi.advanceTimersByTime(0);
  });
  await flush();
}
const speak = (text: string, stash = '') => act(() => pipeline.takes.at(-1)!.input.onPreviewParts?.({ text, stash }));
const elapse = (ms: number) => act(async () => {
  vi.advanceTimersByTime(ms);
});

beforeEach(() => {
  vi.useFakeTimers();
  pipeline.takes = [];
  pipeline.asr.available = true;
  pipeline.asr.consent = 'granted';
  pipeline.asr.appHold = true;
  closeVoiceConsent();
  pipeline.permission = 'granted';
  pipeline.captureActive = false;
  pipeline.startMs = 0;
  pipeline.startError = null;
  pipeline.keepOnSetAside = true;
  send.mockReset().mockResolvedValue(true);
  telemetry.emit.mockReset();
  onTap.mockReset();
  native.impact.mockReset().mockResolvedValue(true);
  native.notify.mockReset().mockResolvedValue(true);
  native.selection.mockReset().mockResolvedValue(true);
  native.permission.mockReset().mockResolvedValue(null);
  native.capture.mockReset().mockReturnValue(false);
  native.prepare.mockReset().mockResolvedValue(true);
  native.hold.mockReset().mockReturnValue(false);
  native.surfaces.mockReset().mockResolvedValue('ok');
  native.reject.mockReset().mockResolvedValue(undefined);
  native.holdListeners = [];
  Object.defineProperty(navigator, 'permissions', {
    configurable: true,
    value: { query: vi.fn(async () => ({ state: pipeline.permission })) },
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [] })) },
  });
});
afterEach(async () => {
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  // A Desktop take keeps the Mac's answer for later pages: none of it reaches the next test.
  window.localStorage.removeItem('avibe.voice.desktopMicOpened');
  cleanup();
  // A frame, and the App's answer: an unmounted surface is taken back from the App (lib/nativeVoiceHold).
  await vi.advanceTimersByTimeAsync(20);
  vi.useRealTimers();
});

describe('useHoldToTalk', () => {
  it('in the iOS App, readies its microphone on entering and lets the App record an allowed hold', async () => {
    native.capture.mockReturnValue(true);
    native.permission.mockResolvedValue('granted');
    const prewarm = vi.mocked((await import('../../lib/voiceRecording')).prewarmVoiceCapture);
    prewarm.mockClear();
    const el = await mount();
    // Ready on entering, nothing started: no microphone indicator until a hold.
    expect(native.prepare).toHaveBeenCalledTimes(1);
    const pressedAt = Date.now();
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
    // The press's start, for the trace: press to first audio is what the hold is judged by.
    expect(pipeline.takes[0].input.pressedAt).toBe(pressedAt);
    // The page's own audio context is not warmed for a take the App records.
    expect(prewarm).not.toHaveBeenCalled();
  });

  it('records through the page when the App cannot', async () => {
    native.capture.mockReturnValue(false);
    const el = await mount();
    expect(native.prepare).not.toHaveBeenCalled();
    await holdFor(el);
    expect(pipeline.takes[0].input.nativeCapture).toBeUndefined();
  });

  it('treats a short press as a tap, inside the release, and opens no microphone', async () => {
    const el = await mount();
    down(el);
    // A finger holds 200 ms (owner decision, 2026-11-04): released before it, the press is a tap.
    await elapse(150);
    up(el);
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('idle');
  });

  it('reads the conversation when the microphone opens, not on a tap', async () => {
    const context = vi.fn(() => ({ reply: '上一条回复', userPrevious: '上一条消息' }));
    const el = await mount({ context });
    down(el);
    await elapse(150);
    up(el);
    expect(context).not.toHaveBeenCalled();

    await holdFor(el);
    expect(context).toHaveBeenCalledOnce();
    expect(pipeline.takes[0].input.context).toEqual({ reply: '上一条回复', userPrevious: '上一条消息' });
  });

  it("hands the card the microphone's level while the take records, and quiet once it ends", async () => {
    const el = await mount();
    await holdFor(el);
    act(() => pipeline.takes[0].input.onLevel?.(0.3));
    expect(hold.level.read()).toBe(0.3);

    await speak('好');
    await elapse(1_000);
    act(() => pipeline.takes[0].input.onLevel?.(0.2));
    up(el);
    await act(async () => pipeline.takes[0].resolve('好'));
    await flush();
    expect(hold.phase).toBe('idle');
    expect(hold.level.read()).toBe(0);
  });

  it('gives a press that moves before the hold back to the page: no tap, no take', async () => {
    const el = await mount();
    down(el);
    move(el, 488);
    await elapse(400);
    up(el, 488);
    expect(onTap).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(0);
  });

  // Before the hold a mouse press is not captured (its mouseup and click stay the element's), so
  // the window follows it: off the element, a move still gives it back and a release still ends it.
  it('follows a mouse press off the element until the hold, so the microphone never opens after the button is up', async () => {
    const el = await mount();
    down(el, 500, 'mouse');
    fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 100, clientY: 460 });
    expect(hold.phase).toBe('idle');
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 460 });

    down(el, 500, 'mouse');
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 503 });
    expect(hold.phase).toBe('idle');
    await elapse(600);
    await flush();
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('idle');
  });

  // The owner (11-20): a word spoken right at the cue was lost. On his iPhone the microphone opens
  // about 0.5 s (p90 1 s) after the hold is reached (602 takes' capture traces), so the cue that
  // says "speak now" (the haptic and the listening card) comes only once capture has started.
  it('gives the speak-now cue only once the microphone is capturing, not at the hold', async () => {
    pipeline.startMs = 500;
    const el = await mount();
    down(el);
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    expect(hold.phase).not.toBe('listening');
    expect(native.impact).not.toHaveBeenCalledWith('medium');

    await elapse(500);
    await flush();
    expect(hold.phase).toBe('listening');
    expect(native.impact).toHaveBeenCalledWith('medium');
  });

  it('gives no speak-now cue to a take armed to cancel when capture starts, and shows it once disarmed', async () => {
    pipeline.startMs = 500;
    const el = await mount();
    down(el);
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    await flush();
    move(el, 400);
    expect(hold.phase).toBe('armed');
    await elapse(500);
    await flush();
    expect(native.impact).not.toHaveBeenCalledWith('medium');
    move(el, 480);
    expect(hold.phase).toBe('listening');
  });

  it('keeps a release before the microphone is capturing a slip, not a sent take', async () => {
    pipeline.startMs = 500;
    const el = await mount();
    down(el);
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    await flush();
    move(el, 470);
    expect(hold.phase).not.toBe('listening');
    up(el);
    await elapse(500);
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(native.impact).not.toHaveBeenCalledWith('medium');
  });

  it('records from 200 ms, shows the live words, and sends the final text as a voice message on release', async () => {
    const el = await mount();
    await holdFor(el);
    expect(hold.phase).toBe('listening');
    await speak('帮我看一下', '周一');
    expect(hold.phase).toBe('recording');
    expect([hold.text, hold.stash]).toEqual(['帮我看一下', '周一']);

    await elapse(2_000);
    up(el);
    const take = pipeline.takes[0];
    expect(take.finish).toHaveBeenCalled();
    expect(hold.phase).toBe('settling');
    await act(async () => take.resolve('帮我看一下周一的发布'));
    await flush();

    expect(send).toHaveBeenCalledWith('帮我看一下周一的发布', { input: 'voice' });
    expect(hold.outcome).toBe('sent');
    expect(hold.phase).toBe('idle');
    expect(native.impact).toHaveBeenNthCalledWith(1, 'medium');
    expect(native.impact).toHaveBeenNthCalledWith(2, 'light');
  });

  it('arms cancel 80 px up, disarms under 60 px, and a release while armed sends nothing', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('不要了');
    move(el, 500 - 81);
    expect(hold.phase).toBe('armed');
    move(el, 500 - 70);
    expect(hold.phase).toBe('armed');
    move(el, 500 - 59);
    expect(hold.phase).toBe('recording');
    move(el, 500 - 90);
    await elapse(2_000);
    up(el, 410);
    await flush();

    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    expect(hold.outcome).toBe('cancelled');
    // Into the cancel position and back out of it feel different: rigid in, soft out.
    expect(native.impact.mock.calls.map(([style]) => style)).toEqual(['medium', 'rigid', 'soft', 'rigid']);
    expect(native.selection).not.toHaveBeenCalled();
    expect(native.notify).toHaveBeenCalledWith('warning');
  });

  // WebKit can lose a finger's capture during a slide without a lost-capture, and the release then
  // goes to where the finger lifted (iOS hold smoke: pointer-up on the body). The window hears it.
  it("ends a finger's hold on a release the element never hears: sends it, or cancels it when armed", async () => {
    const el = await mount();
    await holdFor(el);
    await speak('发出去');
    await elapse(2_000);
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 500 });
    expect(pipeline.takes[0].finish).toHaveBeenCalledTimes(1);
    await act(async () => pipeline.takes[0].resolve('发出去'));
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
    expect(hold.outcome).toBe('sent');

    await holdFor(el);
    await speak('不要了');
    move(el, 410);
    expect(hold.phase).toBe('armed');
    await elapse(2_000);
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 350 });
    await flush();
    expect(pipeline.takes[1].abort).toHaveBeenCalled();
    expect(send).toHaveBeenCalledTimes(1);
    expect(hold.outcome).toBe('cancelled');
    expect(hold.phase).toBe('idle');
  });

  // Once the capture is lost, the moves go where the finger is too: a slide up to cancel is still
  // a cancel, and a press that moved off before the hold is still given back to the page.
  it("judges a finger's slide by the moves only the window hears", async () => {
    const el = await mount();
    await holdFor(el);
    await speak('不要了');
    fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 100, clientY: 500 - 81 });
    expect(hold.phase).toBe('armed');
    await elapse(2_000);
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 400 });
    await flush();
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    expect(hold.outcome).toBe('cancelled');
    // Into the cancel position once, for a move both paths heard: one rigid.
    move(el, 500 - 90);
    expect(native.impact.mock.calls.map(([style]) => style)).toEqual(['medium', 'rigid']);

    down(el);
    fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 100, clientY: 470 });
    await elapse(400);
    fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 100, clientY: 470 });
    expect(onTap).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(1);
  });

  it('ends a hold once when both the element and the window hear its release', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('一次');
    await elapse(2_000);
    up(el);
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 100, clientY: 500 });
    fireEvent.pointerCancel(window, { pointerId: 1 });
    expect(pipeline.takes[0].finish).toHaveBeenCalledTimes(1);
    await act(async () => pipeline.takes[0].resolve('一次'));
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
    expect(hold.outcome).toBe('sent');

    // A tap, likewise: one tap, no take.
    down(el);
    await elapse(100);
    up(el);
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 100, clientY: 500 });
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(pipeline.takes).toHaveLength(1);
  });

  it("leaves a finger's hold alone when another pointer is released or cancelled", async () => {
    const el = await mount();
    await holdFor(el);
    await speak('还在说');
    fireEvent.pointerUp(document.body, { pointerId: 2, clientX: 100, clientY: 500 });
    fireEvent.pointerCancel(document.body, { pointerId: 2 });
    expect(hold.phase).toBe('recording');
    expect(pipeline.takes[0].finish).not.toHaveBeenCalled();
    expect(pipeline.takes[0].abort).not.toHaveBeenCalled();
    await elapse(2_000);
    up(el);
    expect(pipeline.takes[0].finish).toHaveBeenCalledTimes(1);
  });

  it("stops the window hearing a finger's release once the press ends or the surface goes", async () => {
    const added = vi.spyOn(window, 'addEventListener');
    const removed = vi.spyOn(window, 'removeEventListener');
    const ends = (spy: typeof added) => spy.mock.calls
      .filter(([type, , options]) => ['pointermove', 'pointerup', 'pointercancel'].includes(type) && options === true)
      .map(([type, listener]) => [type, listener]);
    const el = await mount();
    down(el);
    await elapse(100);
    up(el);
    expect(ends(added)).toHaveLength(3);
    expect(ends(removed)).toEqual(ends(added));

    // Unmounted mid-hold.
    await holdFor(el);
    expect(ends(added)).toHaveLength(6);
    cleanup();
    expect(ends(removed)).toEqual(ends(added));
    added.mockRestore();
    removed.mockRestore();
  });

  // The wait at the hold was the device starting only then (owner, 11-25: the ring is full and
  // nothing happens yet). The press starts it, so at the hold the take only begins on it.
  describe('the microphone during a press', () => {
    const openStream = () => {
      const track = { stop: vi.fn() };
      vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementation(async () => ({ getTracks: () => [track] }) as unknown as MediaStream);
      return track;
    };
    const opened = () => vi.mocked(navigator.mediaDevices.getUserMedia).mock.calls.length;
    const mouse = () => mount({ gesture: (pointerType) => (pointerType === 'mouse' ? MOUSE_HOLD_GESTURE : {}) });
    /** What this page knows of the microphone, forgotten as a hold the microphone refused forgets
     *  it (earlier tests leave it known). Asks nothing of the device itself. */
    const forgetMicrophone = async (el: HTMLElement) => {
      pipeline.startError = new DOMException('Blocked', 'NotAllowedError');
      await holdFor(el);
      up(el);
      await flush();
      pipeline.startError = null;
    };

    it('opens it 150 ms into a press and hands it to the take at the hold, opening nothing then', async () => {
      const track = openStream();
      const el = await mount();
      down(el);
      await elapse(149);
      expect(opened()).toBe(0);
      await elapse(1);
      await flush();
      expect(opened()).toBe(1);
      expect(pipeline.takes).toHaveLength(0);
      await elapse(50);
      await flush();
      // The hold: the take starts on the open microphone, and nothing new is asked of the device.
      expect(pipeline.takes).toHaveLength(1);
      expect(opened()).toBe(1);
      const stream = await pipeline.takes[0].input.stream;
      expect(stream?.getTracks()).toEqual([track]);
      expect(track.stop).not.toHaveBeenCalled();
    });

    it('closes it when the press ends before the hold: a tap records and sends nothing', async () => {
      const track = openStream();
      const el = await mount();
      down(el);
      await elapse(170);
      await flush();
      expect(opened()).toBe(1);
      up(el);
      await flush();
      expect(onTap).toHaveBeenCalledTimes(1);
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(pipeline.takes).toHaveLength(0);
      expect(send).not.toHaveBeenCalled();

      // A press that moved off is given back to the page, and closes it too.
      down(el);
      await elapse(170);
      await flush();
      move(el, 470);
      await flush();
      expect(track.stop).toHaveBeenCalledTimes(2);
      expect(pipeline.takes).toHaveLength(0);
    });

    it('opens nothing for an ordinary tap or click', async () => {
      openStream();
      const el = await mouse();
      down(el);
      await elapse(130);
      up(el);
      down(el, 500, 'mouse');
      await elapse(130);
      up(el);
      await flush();
      await elapse(400);
      expect(opened()).toBe(0);

      // A mouse held past it opens it, and its hold takes it.
      down(el, 500, 'mouse');
      await elapse(150);
      await flush();
      expect(opened()).toBe(1);
      await elapse(200);
      await flush();
      expect(pipeline.takes).toHaveLength(1);
      expect(pipeline.takes[0].input.stream).toBeDefined();
      expect(opened()).toBe(1);
    });

    it('opens nothing during a press unless the microphone is known to be allowed', async () => {
      openStream();
      const el = await mount();
      await forgetMicrophone(el);
      // The question this hold brings up is refused, so this page has no allowed answer after it.
      vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(new DOMException('Blocked', 'NotAllowedError'));
      pipeline.permission = 'prompt';
      down(el);
      await elapse(170);
      await flush();
      // The question stays the hold's, as before: it comes at the hold, not during the press.
      expect(opened()).toBe(0);
      await elapse(30);
      await flush();
      expect(opened()).toBe(1);
      expect(hold.outcome).toBe('denied');

      pipeline.permission = 'denied';
      down(el);
      await elapse(170);
      await flush();
      expect(opened()).toBe(1);
    });

    // WebKit on iOS can ask again after Allow, even where it answers `granted`.
    it('in a browser on iOS, opens nothing during a press', async () => {
      const agent = vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15');
      try {
        openStream();
        const el = await mount();
        down(el);
        await elapse(190);
        await flush();
        expect(opened()).toBe(0);
        await elapse(10);
        await flush();
        expect(pipeline.takes).toHaveLength(1);
        expect(pipeline.takes[0].input.stream).toBeUndefined();
      } finally {
        agent.mockRestore();
      }
    });

    it('on Desktop, opens it during a press only while a take in this page has opened it', async () => {
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
      openStream();
      const el = await mount();
      await forgetMicrophone(el);
      expect(opened()).toBe(0);

      // Desktop cannot read the Mac's permission: the first take opens its own, at the hold.
      down(el);
      await elapse(150);
      await flush();
      expect(opened()).toBe(0);
      await elapse(50);
      await flush();
      await elapse(0);
      await flush();
      expect(pipeline.takes.at(-1)!.input.stream).toBeUndefined();
      expect(hold.phase).toBe('listening');
      fireEvent.keyDown(window, { key: 'Escape' });
      await flush();

      // After it, a press opens it ahead of the hold.
      down(el);
      await elapse(150);
      await flush();
      expect(opened()).toBe(1);
      up(el);
      await flush();

      // Refused there (withdrawn in System Settings): the page no longer knows it allowed.
      vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(new DOMException('Blocked', 'NotAllowedError'));
      down(el);
      await elapse(150);
      await flush();
      expect(opened()).toBe(2);
      up(el);
      await flush();
      down(el);
      await elapse(170);
      await flush();
      expect(opened()).toBe(2);
    });

    // Owner, 2026-11-28: after a start, the first hold opened the microphone only at the hold
    // (350 ms, then about 350 ms more for a cold device) and lost its first words.
    it('on Desktop, opens it during a page\'s first press where a take opened it before', async () => {
      Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
      const key = 'avibe.voice.desktopMicOpened';
      try {
        openStream();
        const el = await mouse();
        // This page has opened nothing yet (as after a start): it forgets any earlier take's.
        await forgetMicrophone(el);
        expect(window.localStorage.getItem(key)).toBeNull();
        // An earlier launch opened it: the Mac's answer is kept.
        window.localStorage.setItem(key, '1');
        down(el, 500, 'mouse');
        await elapse(150);
        await flush();
        expect(opened()).toBe(1);
        await elapse(200);
        await flush();
        // The hold takes the stream the press opened, and opens nothing more.
        expect(pipeline.takes.at(-1)!.input.stream).toBeDefined();
        expect(opened()).toBe(1);
        fireEvent.keyDown(window, { key: 'Escape' });
        await flush();

        // Refused (withdrawn in System Settings): forgotten, for later pages too.
        vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(new DOMException('Blocked', 'NotAllowedError'));
        down(el, 500, 'mouse');
        await elapse(150);
        await flush();
        expect(opened()).toBe(2);
        up(el);
        await flush();
        expect(window.localStorage.getItem(key)).toBeNull();
        down(el, 500, 'mouse');
        await elapse(170);
        await flush();
        expect(opened()).toBe(2);
        up(el);
        await flush();
      } finally {
        window.localStorage.removeItem(key);
      }
    });

    it('in a browser, keeps no earlier page\'s answer', async () => {
      const key = 'avibe.voice.desktopMicOpened';
      try {
        openStream();
        const el = await mount();
        await forgetMicrophone(el);
        window.localStorage.setItem(key, '1');
        // A browser whose permission cannot be read: only this page's own take would count.
        vi.mocked(navigator.permissions.query).mockRejectedValueOnce(new TypeError('unsupported'));
        down(el, 500, 'mouse');
        await elapse(170);
        await flush();
        expect(opened()).toBe(0);
        up(el);
        await flush();
      } finally {
        window.localStorage.removeItem(key);
      }
    });

    it('opens nothing for a surface gone while the permission was still being read', async () => {
      openStream();
      let answer!: (status: { state: PermissionState }) => void;
      vi.mocked(navigator.permissions.query).mockImplementationOnce(() => new Promise((resolve) => { answer = resolve as typeof answer; }));
      const el = await mount();
      down(el);
      await elapse(150);
      await flush();
      cleanup();
      await act(async () => answer({ state: 'granted' }));
      await flush();
      expect(opened()).toBe(0);
    });

    it('in the iOS App, leaves the microphone to the App', async () => {
      native.capture.mockReturnValue(true);
      native.permission.mockResolvedValue('granted');
      openStream();
      const el = await mount();
      await holdFor(el);
      expect(opened()).toBe(0);
      expect(pipeline.takes[0].input.stream).toBeUndefined();
    });
  });

  it('drops a take shorter than half a second as too short', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(300);
    up(el);
    await flush();
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    expect(hold.outcome).toBe('too-short');
  });

  it('calls a take that heard nothing too short', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('empty')));
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(hold.outcome).toBe('too-short');
  });

  // The reliability log (`dictation_finalized`, entry `hold`) says how every hold ended, and
  // tells a microphone that gave nothing apart from a take that was silent.
  const holdLogs = () => telemetry.emit.mock.calls
    .map(([event]) => event as { event: string; entry?: string })
    .filter((event) => event.event === 'dictation_finalized' && event.entry === 'hold');

  it.each([
    ['no_frames', 'no-sound', 'no_frames'],
    ['muted', 'mic-busy', 'muted'],
    ['silence', 'too-short', 'too_short'],
  ] as const)('says a take its microphone delivered as %s is %s, and logs it so', async (delivered, said, logged) => {
    const el = await mount();
    await holdFor(el);
    await elapse(3_000);
    up(el);
    pipeline.takes[0].captureClass.mockReturnValue(delivered);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('empty')));
    await flush();

    expect(hold.outcome).toBe(said);
    expect(holdLogs()).toEqual([expect.objectContaining({
      outcome: logged,
      dictationId: 'take-1',
      captureClass: delivered,
      attemptCount: 1,
    })]);
  });

  it('shows how a take is recorded, and starts the next one live', async () => {
    const el = await mount();
    await holdFor(el);
    await act(async () => pipeline.takes[0].input.onCaptureState?.('no_sound'));
    expect(hold.captureState).toBe('no_sound');
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('empty')));
    await flush();

    await holdFor(el);
    expect(hold.captureState).toBe('live');
  });

  it('logs a sent hold, and a press the microphone refused', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].resolve('hello'));
    await flush();
    expect(holdLogs()).toEqual([expect.objectContaining({ outcome: 'sent', dictationId: 'take-1' })]);

    telemetry.emit.mockReset();
    pipeline.permission = 'denied';
    await holdFor(el);
    await flush();
    expect(hold.outcome).toBe('denied');
    expect(holdLogs()).toEqual([expect.objectContaining({ outcome: 'denied' })]);
    expect(holdLogs()[0]).not.toHaveProperty('dictationId');
  });

  it('logs an interrupted hold with its reason', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(3_000);
    up(el);
    pipeline.takes[0].voiced.mockReturnValue(true);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('empty')));
    await flush();
    expect(holdLogs()).toEqual([expect.objectContaining({ outcome: 'interrupted', interruptReason: 'failed', dictationId: 'take-1' })]);
  });

  it('keeps a take that held speech when nothing was recognized, and offers to retry it', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(3_000);
    up(el);
    pipeline.takes[0].voiced.mockReturnValue(true);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('empty')));
    await flush();

    expect(send).not.toHaveBeenCalled();
    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('failed');
    expect(hold.canRetry).toBe(true);
    expect(pipeline.takes[0].abort).not.toHaveBeenCalled();
  });

  it('says the network dropped again when a retry cannot reach the service, and keeps the take', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('unavailable')));
    await flush();
    expect(hold.interruptReason).toBe('dropped');

    pipeline.takes[0].retry.mockRejectedValueOnce(new VoiceTranscriptionError('unavailable'));
    await act(async () => hold.retry());
    await flush();
    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('dropped');
    expect(hold.canRetry).toBe(true);

    await act(async () => hold.retry());
    await flush();
    expect(send).toHaveBeenCalledWith('heard again', { input: 'voice' });
  });

  it('keeps what was heard when the take is cut off, and sends it on request', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('帮我看一下周一', '发布前');
    await act(async () => pipeline.takes[0].reject(new Error('track ended')));
    await flush();

    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('dropped');
    expect(hold.text).toBe('帮我看一下周一发布前');
    up(el);
    expect(send).not.toHaveBeenCalled();

    // A pending take holds the next press back: it belongs to the system.
    down(el);
    await elapse(400);
    expect(pipeline.takes).toHaveLength(1);

    await act(async () => hold.sendInterrupted());
    await flush();
    expect(send).toHaveBeenCalledWith('帮我看一下周一发布前', { input: 'voice' });
    expect(hold.outcome).toBe('sent');
  });

  it('retries a failed transcription from the kept recording, then sends', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('failed')));
    await flush();
    expect(hold.interruptReason).toBe('failed');
    expect(hold.canRetry).toBe(true);

    await act(async () => hold.retry());
    await flush();
    expect(pipeline.takes[0].retry).toHaveBeenCalled();
    expect(send).toHaveBeenCalledWith('heard again', { input: 'voice' });
  });

  it('keeps the text when the send fails, and sends it again on request', async () => {
    send.mockResolvedValueOnce(false);
    const el = await mount();
    await holdFor(el);
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].resolve('发出去'));
    await flush();
    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('send-failed');
    expect(hold.text).toBe('发出去');

    await act(async () => hold.sendInterrupted());
    await flush();
    expect(send).toHaveBeenCalledTimes(2);
    expect(hold.outcome).toBe('sent');
  });

  it('reports a refused first permission probe, and tries again on the next hold', async () => {
    pipeline.permission = 'prompt';
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(new DOMException('Blocked', 'NotAllowedError'));
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('denied');
    expect(hold.phase).toBe('idle');
    await holdFor(el);
    expect(hold.outcome).toBe('denied');
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2);
    expect(telemetry.emit).toHaveBeenCalledWith(expect.objectContaining({
      event: 'capture_trace', entry: 'hold', mic: 'NotAllowedError', captureClass: 'no_microphone',
    }));
  });

  it('lets an explicit Desktop hold recheck capture despite a stale browser denial', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    pipeline.permission = 'denied';
    const el = await mount();
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(1);
    expect(hold.phase).toBe('listening');
    expect(navigator.permissions.query).not.toHaveBeenCalled();
  });

  it('keeps a native permission denial authoritative, including in a Desktop-marked page', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    native.permission.mockResolvedValue('denied');
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('denied');
    expect(pipeline.takes).toHaveLength(0);
    expect(navigator.permissions.query).not.toHaveBeenCalled();
  });

  it('logs input counts and an origin category without device identities', async () => {
    pipeline.permission = 'prompt';
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(new DOMException('private message', 'NotAllowedError'));
    Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', {
      value: vi.fn(async () => [
        { kind: 'audioinput', label: 'private microphone', deviceId: 'private-device', groupId: 'private-group' },
        { kind: 'audiooutput', label: 'private speaker' },
      ]),
    });
    const el = await mount();
    await holdFor(el);
    const trace = telemetry.emit.mock.calls.map(([event]) => event).find((event) => event.event === 'capture_trace');
    expect(trace).toMatchObject({ mic: 'NotAllowedError', audioInputCount: 1, origin: 'loopback' });
    expect(JSON.stringify(trace)).not.toContain('private');
  });

  it('does not replace a later take with an obsolete permission failure', async () => {
    pipeline.permission = 'prompt';
    let refuse!: (error: Error) => void;
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise((_resolve, reject) => { refuse = reject; }));
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('asking');
    pipeline.permission = 'granted';
    await holdFor(el);
    expect(hold.phase).toBe('listening');
    await act(async () => refuse(new DOMException('Blocked', 'NotAllowedError')));
    expect(hold.phase).toBe('listening');
    expect(hold.outcome).toBeNull();
  });

  it('does not redisplay a dismissed permission request when its answer arrives', async () => {
    pipeline.permission = 'prompt';
    let refuse!: (error: Error) => void;
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise((_resolve, reject) => { refuse = reject; }));
    const el = await mount();
    await holdFor(el);
    act(() => hold.dismiss());
    await act(async () => refuse(new DOMException('Blocked', 'NotAllowedError')));
    expect(hold.outcome).toBeNull();
  });

  it.each([
    ['NotFoundError', 'no-device'],
    ['NotReadableError', 'mic-busy'],
    ['AbortError', 'mic-failed'],
  ])('reports %s from the first permission probe as %s', async (name, outcome) => {
    pipeline.permission = 'prompt';
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(new DOMException('Unavailable', name));
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe(outcome);
    expect(pipeline.takes).toHaveLength(0);
  });

  it.each([
    ['NotAllowedError', 'denied'],
    ['NotFoundError', 'no-device'],
    ['NotReadableError', 'mic-busy'],
    ['TypeError', 'mic-failed'],
  ])('classifies %s when a Desktop take cannot open as %s and permits a second hold', async (name, outcome) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    pipeline.permission = 'denied';
    pipeline.startError = new DOMException('Blocked', name);
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe(outcome);
    expect(hold.phase).toBe('idle');
    pipeline.startError = null;
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(2);
    expect(hold.phase).toBe('listening');
  });

  it('reports a Desktop refusal that arrives after the permission alert took the press', async () => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    pipeline.startMs = 5_000;
    pipeline.startError = new DOMException('Blocked', 'NotAllowedError');
    const el = await mount();
    await holdFor(el);
    act(() => window.dispatchEvent(new Event('blur')));
    expect(hold.outcome).toBe('asking');
    await elapse(5_000);
    expect(hold.outcome).toBe('denied');
    expect(hold.phase).toBe('idle');
    expect(send).not.toHaveBeenCalled();
  });

  it.each(['dismiss', 'new-hold', 'cancel'] as const)('ignores a late Desktop refusal after %s', async (action) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    pipeline.startMs = 5_000;
    pipeline.startError = new DOMException('Blocked', 'NotAllowedError');
    const el = await mount();
    await holdFor(el);
    if (action === 'cancel') {
      fireEvent.keyDown(window, { key: 'Escape' });
    } else {
      act(() => window.dispatchEvent(new Event('blur')));
      if (action === 'dismiss') act(() => hold.dismiss());
      else {
        pipeline.startMs = 0;
        pipeline.startError = null;
        await holdFor(el);
      }
    }
    await elapse(5_000);
    expect(hold.outcome).toBe(action === 'cancel' ? 'cancelled' : null);
    expect(hold.phase).toBe(action === 'new-hold' ? 'listening' : 'idle');
    expect(send).not.toHaveBeenCalled();
  });

  it('lets the permission prompt take the first hold, records the next once allowed, and says why a refused one does nothing', async () => {
    pipeline.permission = 'prompt';
    let allow!: (stream: MediaStream) => void;
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementationOnce(() => new Promise((resolve) => {
      allow = resolve;
    }));
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('asking');
    expect(pipeline.takes).toHaveLength(0);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalled();

    // The system alert took the press; Allow comes after it ended, and its stream is closed at once.
    fireEvent.pointerCancel(el, { pointerId: 1 });
    const track = { stop: vi.fn() };
    await act(async () => allow({ getTracks: () => [track] } as unknown as MediaStream));
    expect(track.stop).toHaveBeenCalled();

    // iOS Safari can go on answering `prompt` after Allow: the next press records.
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(1);
    expect(hold.phase).toBe('listening');
    fireEvent.keyDown(window, { key: 'Escape' });

    pipeline.permission = 'denied';
    await holdFor(el);
    expect(hold.outcome).toBe('denied');
    expect(pipeline.takes).toHaveLength(1);
  });

  it('uses the shell permission probe before the browser Permissions API', async () => {
    native.permission.mockResolvedValue('granted');
    vi.mocked(navigator.permissions.query).mockResolvedValue({ state: 'denied' } as PermissionStatus);
    const el = await mount();

    await holdFor(el);

    expect(native.permission).toHaveBeenCalled();
    expect(navigator.permissions.query).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(1);
  });

  it('says the system is asking when its alert takes the press before the microphone opens', async () => {
    // iOS asks again once the microphone has been idle for about a minute, though the query said
    // granted: the alert comes up while the take waits for the microphone, and takes the press.
    pipeline.startMs = 5_000;
    const el = await mount();
    await holdFor(el);
    // Waiting for the microphone: still a press, no speak-now cue yet.
    expect(hold.phase).toBe('pressing');
    fireEvent.pointerCancel(el, { pointerId: 1 });
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(hold.outcome).toBe('asking');
    expect(hold.phase).toBe('idle');

    // The answer arrives after the press ended: the next press is a new take.
    await elapse(5_000);
    pipeline.startMs = 0;
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(2);
    expect(hold.phase).toBe('listening');
  });

  it('says voice is unavailable when the server has no speech service', async () => {
    pipeline.asr.available = false;
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('unavailable');
    expect(pipeline.takes).toHaveLength(0);
  });

  // ui/design: composer/voice-consent/*
  it('asks before the first take and opens no microphone until the reader said yes', async () => {
    pipeline.asr.consent = null;
    const el = await mount();
    await holdFor(el);
    expect(voiceConsentAsking()).toBe(true);
    expect(native.permission).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.outcome).toBeNull();
  });

  // A "no" is recoverable (owner decision, 2026-11-17): the next press asks again, and only a yes
  // lets a later press record.
  it('asks again on a press after a no, and records on the press after Allow', async () => {
    pipeline.asr.consent = 'declined';
    const el = await mount();
    await holdFor(el);
    up(el);
    expect(voiceConsentAsking()).toBe(true);
    expect(hold.outcome).toBeNull();
    expect(native.permission).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(0);

    await act(async () => { await answerVoiceConsent('granted'); });
    closeVoiceConsent();
    await holdFor(el);
    expect(voiceConsentAsking()).toBe(false);
    expect(pipeline.takes).toHaveLength(1);
  });

  it('a second no keeps voice off, and the press after it asks again', async () => {
    pipeline.asr.consent = 'declined';
    const el = await mount();
    await holdFor(el);
    up(el);
    expect(voiceConsentAsking()).toBe(true);
    await act(async () => { await answerVoiceConsent('declined'); });
    closeVoiceConsent();

    await holdFor(el);
    up(el);
    expect(voiceConsentAsking()).toBe(true);
    expect(pipeline.takes).toHaveLength(0);
  });

  it('leaves the press alone while another voice capture holds the microphone', async () => {
    pipeline.captureActive = true;
    const el = await mount();
    await holdFor(el);
    up(el);
    expect(hold.phase).toBe('idle');
    expect(onTap).not.toHaveBeenCalled();
    expect(pipeline.takes).toHaveLength(0);
  });

  it('keeps the words, unsent, when the system takes the press and the surface asks to keep them', async () => {
    const el = await mount({ onSystemInterrupt: 'interrupt' });
    await holdFor(el);
    await speak('写到一半');
    await elapse(1_000);
    act(() => {
      window.dispatchEvent(new Event('blur'));
    });
    expect(pipeline.takes[0].finish).toHaveBeenCalled();
    await act(async () => pipeline.takes[0].resolve('写到一半了'));
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('system');
    expect(hold.text).toBe('写到一半了');
  });

  // Another voice surface claiming the microphone finishes the pipeline without a release.
  it('sends nothing when the take finishes without a release, and keeps its words', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('还没说完');
    await act(async () => pipeline.takes[0].resolve('还没说完'));
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(hold.phase).toBe('interrupted');
    expect(hold.interruptReason).toBe('system');
    expect(hold.text).toBe('还没说完');
    up(el);
    await flush();
    expect(send).not.toHaveBeenCalled();
  });

  it('cancels at once on Escape, wherever focus is', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('算了');
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await flush();
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(hold.outcome).toBe('cancelled');
    up(el);
    expect(send).not.toHaveBeenCalled();
  });

  // The card's × while the words are finalized (ui/design: composer/hold-mobile/f-settling).
  it.each([
    { how: 'the card’s ×', kept: true, outcome: 'kept' },
    { how: 'the card’s ×', kept: false, outcome: 'cancelled' },
    { how: 'Esc', kept: true, outcome: 'kept' },
  ] as const)('stops waiting for the words on $how: the card closes, nothing is ever sent, and the take is $outcome', async ({ how, kept, outcome }) => {
    pipeline.keepOnSetAside = kept;
    const el = await mount();
    await holdFor(el);
    await speak('帮我看一下', '周一');
    // While it records there is nothing to stop waiting for: release sends, sliding up cancels.
    expect(hold.cancelSettling).toBeNull();
    await elapse(4_000);
    up(el);
    expect(hold.phase).toBe('settling');

    if (how === 'Esc') {
      act(() => {
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      });
    } else {
      act(() => hold.cancelSettling!());
    }
    await flush();
    const take = pipeline.takes[0];
    expect(take.setAside).toHaveBeenCalledOnce();
    expect(take.abort).not.toHaveBeenCalled();
    expect(hold.phase).toBe('idle');
    expect(hold.cancelSettling).toBeNull();
    expect(hold.outcome).toBe(outcome);
    expect([hold.text, hold.stash]).toEqual(['', '']);
    expect(native.notify).toHaveBeenCalledWith('warning');

    // Words that arrive after the cancel, partial or final, are never sent.
    await act(async () => take.resolve('帮我看一下周一的发布'));
    await elapse(10_000);
    await flush();
    expect(send).not.toHaveBeenCalled();
    expect(hold.phase).toBe('idle');
  });

  it('stops waiting for a retry of a cut-off take the same way', async () => {
    const el = await mount();
    await holdFor(el);
    await elapse(4_000);
    up(el);
    await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('unavailable')));
    await flush();
    expect(hold.phase).toBe('interrupted');
    expect(hold.cancelSettling).toBeNull();

    pipeline.takes[0].retry.mockReturnValueOnce(new Promise(() => undefined));
    await act(async () => hold.retry());
    expect(hold.phase).toBe('settling');
    act(() => hold.cancelSettling!());
    await flush();
    expect(pipeline.takes[0].setAside).toHaveBeenCalledOnce();
    expect(hold.phase).toBe('idle');
    expect(hold.outcome).toBe('kept');
    expect(send).not.toHaveBeenCalled();
  });

  it('offers no way to stop once the words are on their way as a message', async () => {
    let accept!: (ok: boolean) => void;
    send.mockImplementationOnce(() => new Promise((resolve) => {
      accept = resolve;
    }));
    const el = await mount();
    await holdFor(el);
    await elapse(4_000);
    up(el);
    expect(hold.cancelSettling).not.toBeNull();
    await act(async () => pipeline.takes[0].resolve('发出去'));
    await flush();
    expect(send).toHaveBeenCalledWith('发出去', { input: 'voice' });
    expect(hold.phase).toBe('settling');
    expect(hold.cancelSettling).toBeNull();
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    expect(pipeline.takes[0].setAside).not.toHaveBeenCalled();

    await act(async () => accept(true));
    await flush();
    expect(hold.outcome).toBe('sent');
  });

  it('starts a new take with an empty transcript, not the words the last one sent', async () => {
    const el = await mount();
    await holdFor(el);
    await speak('第一句');
    await elapse(1_000);
    up(el);
    await act(async () => pipeline.takes[0].resolve('第一句'));
    await flush();
    expect(hold.outcome).toBe('sent');

    await holdFor(el);
    expect(hold.phase).toBe('listening');
    expect([hold.text, hold.stash]).toEqual(['', '']);
  });

  // A composer on a touchscreen laptop gets a mouse and a finger: each press holds by its own numbers.
  it('reads the gesture and canHold for the pointer that pressed', async () => {
    const canHold = vi.fn((pointerType: string) => pointerType !== 'pen');
    const el = await mount({
      canHold,
      gesture: (pointerType) => (pointerType === 'mouse' ? { holdMs: 500, cancel: 'leave', cancelPx: 16, uncancelPx: 0 } : {}),
    });

    down(el, 500, 'mouse');
    await elapse(300);
    expect(hold.phase).toBe('pressing');
    await elapse(200);
    await flush();
    await elapse(0);
    await flush();
    expect(hold.phase).toBe('listening');
    // A mouse cancels by leaving the box (16 px out), not by sliding up.
    el.getBoundingClientRect = () => ({ left: 0, top: 400, right: 200, bottom: 600, width: 200, height: 200, x: 0, y: 400, toJSON: () => ({}) });
    move(el, 420);
    expect(hold.phase).toBe('listening');
    move(el, 500, 217);
    expect(hold.phase).toBe('armed');
    up(el, 500);
    await flush();
    expect(hold.outcome).toBe('cancelled');

    await holdFor(el);
    expect(hold.phase).toBe('listening');
    up(el);
    await flush();

    down(el, 500, 'pen');
    await elapse(600);
    expect(hold.phase).not.toBe('listening');
    expect(canHold.mock.calls.map(([type]) => type)).toEqual(['mouse', 'touch', 'pen']);
  });

  it('keeps the right-click menu when idle, and takes it only while a press is live', async () => {
    const el = await mount();
    expect(fireEvent.contextMenu(el)).toBe(true);
    down(el);
    expect(fireEvent.contextMenu(el)).toBe(false);
    await elapse(300);
    await flush();
    await elapse(0);
    await flush();
    expect(hold.phase).toBe('listening');
    expect(fireEvent.contextMenu(el)).toBe(false);
  });

  it('measures the half-second floor from the microphone opening, not from the hold', async () => {
    pipeline.startMs = 900;
    const el = await mount();
    await holdFor(el);
    await elapse(900);
    await flush();
    await elapse(50);
    up(el);
    await flush();
    expect(pipeline.takes[0].finish).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    expect(hold.outcome).toBe('too-short');
  });
});

// The main Bot's center with a live hold (ui/design: mobile/workbench-tab/team): a tap
// opens the conversation exactly once, however the press went.
describe('the main Bot center', () => {
  const onClick = vi.fn();
  function Center() {
    const value = useHoldToTalk({ send, onTap: onClick });
    return <MobileBotCenter avatar={null} name="Vibey" label="Vibey" unread={0} running={false} onClick={onClick} hold={value.bind} />;
  }
  const tap = async (el: HTMLElement) => {
    down(el);
    await elapse(100);
    up(el);
    fireEvent.click(el, { detail: 1 });
  };
  beforeEach(() => onClick.mockReset());

  it('opens the conversation once for a tap the hold answered', async () => {
    render(<Center />);
    await act(async () => undefined);
    await tap(screen.getByRole('button', { name: 'Vibey' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('still opens it while another recorder holds the microphone', async () => {
    pipeline.captureActive = true;
    render(<Center />);
    await act(async () => undefined);
    await tap(screen.getByRole('button', { name: 'Vibey' }));
    expect(pipeline.takes).toHaveLength(0);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('opens it from the keyboard', async () => {
    render(<Center />);
    await act(async () => undefined);
    fireEvent.click(screen.getByRole('button', { name: 'Vibey' }), { detail: 0 });
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});


// In the iOS App the App recognises the hold itself (lib/nativeVoiceHold): it times the touch,
// gives the haptic at the threshold and has the microphone open from touch-down. The page names
// where, and takes the take at the App's word; its own recognition stays the fallback.
describe('a hold the iOS App recognises', () => {
  const viewport = (content: string | null) => {
    document.querySelector('meta[name="viewport"]')?.remove();
    if (content === null) return;
    const meta = document.createElement('meta');
    meta.setAttribute('name', 'viewport');
    meta.setAttribute('content', content);
    document.head.append(meta);
  };
  /** The harness in an App that recognises holds, its button a box at y 470–530. */
  async function mountFollowed(options: Partial<HoldToTalkOptions> = {}) {
    native.hold.mockReturnValue(true);
    native.capture.mockReturnValue(true);
    native.permission.mockResolvedValue('granted');
    const el = await mount(options);
    el.getBoundingClientRect = () => new DOMRect(0, 470, 300, 60);
    // Nothing lies over the button: a touch in its box reaches it.
    document.elementFromPoint = () => el;
    // The microphone's answer, a frame, and the App's answer to the list.
    await flush();
    await elapse(20);
    await flush();
    return el;
  }
  const namedSurfaces = () => native.surfaces.mock.calls.at(-1)?.[0] ?? [];
  const surfaceId = () => namedSurfaces()[0].id;
  const say = (event: { phase: string; take: string; surface?: string; step?: string }) => act(() => {
    for (const listener of [...native.holdListeners]) listener(event);
  });

  beforeEach(() => viewport('width=device-width, initial-scale=1.0, viewport-fit=cover'));
  afterEach(() => viewport(null));

  it('names its surface where a hold may record now, with the finger\'s threshold', async () => {
    await mountFollowed();
    expect(native.surfaces).toHaveBeenLastCalledWith([{ id: expect.stringMatching(/^[A-Za-z0-9_-]{1,64}$/), x: 0, y: 470, width: 300, height: 60 }], 200);
  });

  // A surface whose owner asks that a touch open no microphone (the main Bot's button, when the
  // instance says so): the App is told with the surface.
  it('names its surface as starting at recognition when its owner asks for that', async () => {
    await mountFollowed({ startsAtRecognition: () => true });
    expect(native.surfaces).toHaveBeenLastCalledWith([{ id: expect.any(String), x: 0, y: 470, width: 300, height: 60, start: 'recognition' }], 200);
    cleanup();
    await elapse(20);
    native.surfaces.mockClear();
    await mountFollowed({ startsAtRecognition: () => false });
    expect(native.surfaces).toHaveBeenLastCalledWith([{ id: expect.any(String), x: 0, y: 470, width: 300, height: 60 }], 200);
  });

  it('names none while voice may not record there: no consent, no microphone answer, or a press it would not take', async () => {
    pipeline.asr.consent = null;
    await mountFollowed();
    expect(native.surfaces.mock.calls.every(([surfaces]) => surfaces.length === 0)).toBe(true);
    cleanup();
    await elapse(20);

    pipeline.asr.consent = 'granted';
    native.surfaces.mockClear();
    const el = await mountFollowed({ canHold: () => false });
    expect(native.surfaces.mock.calls.every(([surfaces]) => surfaces.length === 0)).toBe(true);
    // And the press is the system's, as before.
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(0);
    cleanup();
    await elapse(20);

    native.surfaces.mockClear();
    native.hold.mockReturnValue(true);
    native.capture.mockReturnValue(true);
    native.permission.mockResolvedValue('prompt');
    const asking = await mount();
    asking.getBoundingClientRect = () => new DOMRect(0, 470, 300, 60);
    document.elementFromPoint = () => asking;
    await flush();
    await elapse(20);
    expect(native.surfaces.mock.calls.every(([surfaces]) => surfaces.length === 0)).toBe(true);
  });

  it('takes the take at the App\'s word, with nothing awaited, and shows the card at once', async () => {
    // The take's audio reaches the page a while after the cue.
    pipeline.startMs = 300;
    const el = await mountFollowed();
    native.impact.mockClear();
    native.permission.mockClear();
    const pressedAt = Date.now();
    down(el);
    await elapse(190);
    expect(hold.phase).toBe('pressing');
    expect(pipeline.takes).toHaveLength(0);

    // The App's word comes before the page's own timer. Within it, synchronously (not awaited
    // here, so nothing queued has run): the take is the App's, taken, with no consent read, no
    // permission read and no request in between.
    const id = surfaceId();
    act(() => {
      for (const listener of [...native.holdListeners]) listener({ phase: 'recognised', take: 'n1', surface: id });
    });
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toEqual({ adopt: 'n1' });
    // A take the App recorded says no miss.
    expect(pipeline.takes[0].input.holdMiss).toBeUndefined();
    expect(pipeline.takes[0].input.pressedAt).toBe(pressedAt);
    expect(pipeline.takes[0].start).toHaveBeenCalledTimes(1);
    expect(native.permission).not.toHaveBeenCalled();
    expect(native.reject).not.toHaveBeenCalled();
    // The card comes with the App's haptic, not when the audio has arrived.
    expect(hold.phase).toBe('listening');

    // The page's own timer does not start another, and gives no haptic of its own.
    await elapse(400);
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    expect(hold.phase).toBe('listening');
    expect(native.impact).not.toHaveBeenCalledWith('medium');

    await speak('你好');
    expect(hold.phase).toBe('recording');
    await elapse(400);
    up(el);
    expect(pipeline.takes[0].finish).toHaveBeenCalledTimes(1);
    await act(async () => pipeline.takes[0].resolve('你好'));
    await flush();
    expect(send).toHaveBeenCalledTimes(1);
    expect(hold.outcome).toBe('sent');
    expect(native.reject).not.toHaveBeenCalled();
  });

  it('runs the take from its cue: what was said before the audio arrived is not a slip', async () => {
    pipeline.startMs = 300;
    const el = await mountFollowed();
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    // Released 550 ms after the cue, 250 ms after the take's audio reached the page.
    await elapse(550);
    await flush();
    up(el);
    expect(pipeline.takes[0].finish).toHaveBeenCalledTimes(1);
    expect(pipeline.takes[0].abort).not.toHaveBeenCalled();
  });

  it('waits a moment past its own threshold for the App, then recognises the hold itself', async () => {
    const el = await mountFollowed();
    down(el);
    await elapse(200);
    await flush();
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('pressing');

    await elapse(NATIVE_HOLD_GRACE_MS);
    await flush();
    await elapse(0);
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    // Its own start, as in an App that does not recognise holds.
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
    // The trace says the App followed it and never answered.
    expect(pipeline.takes[0].input.holdMiss).toBe('unanswered');
    expect(hold.phase).toBe('listening');
    expect(native.impact).toHaveBeenCalledWith('medium');

    // The App's word after that is too late: its take is refused, not a second one.
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(pipeline.takes).toHaveLength(1);
    expect(native.reject).toHaveBeenCalledWith('n1');
  });

  it('takes the App\'s take while its own checks are still being read: nothing of its own has started', async () => {
    const el = await mountFollowed();
    // A busy page: its timer runs before the App's word arrives, and its microphone read is slow.
    let answer!: (state: PermissionState) => void;
    native.permission.mockReturnValue(new Promise<PermissionState>((resolve) => { answer = resolve; }));
    down(el);
    await elapse(200 + NATIVE_HOLD_GRACE_MS);
    await flush();
    expect(pipeline.takes).toHaveLength(0);

    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toEqual({ adopt: 'n1' });
    expect(native.reject).not.toHaveBeenCalled();

    // The read's answer starts nothing more.
    await act(async () => answer('granted'));
    await flush();
    await elapse(0);
    expect(pipeline.takes).toHaveLength(1);
    expect(hold.phase).toBe('listening');
  });

  it('does not take the App\'s word for a mouse press, which holds longer than a finger', async () => {
    const el = await mountFollowed({ gesture: (pointerType) => (pointerType === 'mouse' ? MOUSE_HOLD_GESTURE : {}) });
    down(el, 500, 'mouse');
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(pipeline.takes).toHaveLength(0);
    expect(native.reject).toHaveBeenCalledWith('n1');
    expect(hold.phase).toBe('pressing');
  });

  it('refuses the App\'s take when voice may no longer record: consent taken back since the surface was named', async () => {
    const el = await mountFollowed();
    down(el);
    await elapse(150);
    act(() => { noteVoiceStatus({ available: true, consent: 'declined' }); });
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(pipeline.takes).toHaveLength(0);
    expect(native.reject).toHaveBeenCalledWith('n1');
  });

  it('has the App drop a take the surface went away before the page had', async () => {
    pipeline.startMs = 300;
    const el = await mountFollowed();
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    cleanup();
    expect(native.reject).toHaveBeenCalledWith('n1');
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
  });

  it('does not wait for the App on a press it does not follow', async () => {
    const el = await mountFollowed();
    // Above the box the App was given.
    down(el, 100);
    await elapse(200);
    await flush();
    await elapse(0);
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
  });

  it('recognises the hold itself, at its own threshold, when the App cannot open the microphone', async () => {
    const el = await mountFollowed();
    down(el);
    await elapse(60);
    await say({ phase: 'failed', take: 'n1', surface: surfaceId(), step: 'activate:561017449' });
    // Not a hold yet, and maybe a tap: nothing starts on the App's word alone.
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('pressing');
    await elapse(140);
    await flush();
    await elapse(0);
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
    // The trace says why the App did not record it.
    expect(pipeline.takes[0].input.holdMiss).toBe('failed');
  });

  it('refuses a hold the page does not see: no press, a press that already ended, or a take in the way', async () => {
    const el = await mountFollowed();
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(native.reject).toHaveBeenLastCalledWith('n1');

    // A tap the App counted as a hold (the surface was told late).
    down(el);
    await elapse(150);
    up(el);
    await say({ phase: 'recognised', take: 'n2', surface: surfaceId() });
    expect(native.reject).toHaveBeenLastCalledWith('n2');
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('idle');
  });

  it('has the App drop a take the press ended before the page had it', async () => {
    pipeline.startMs = 300;
    const el = await mountFollowed();
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    await elapse(100);
    up(el);
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(native.reject).toHaveBeenCalledWith('n1');
    expect(hold.outcome).toBe('too-short');
    expect(send).not.toHaveBeenCalled();
  });

  it('starts the take itself when the App no longer has the one it recognised, the finger still down', async () => {
    const el = await mountFollowed();
    native.impact.mockClear();
    pipeline.startError = Object.assign(new Error('native voice capture did not start'), { name: NATIVE_HOLD_GONE });
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    pipeline.startError = null;
    await elapse(0);
    await flush();
    expect(pipeline.takes).toHaveLength(2);
    expect(pipeline.takes[0].abort).toHaveBeenCalled();
    expect(pipeline.takes[1].input.nativeCapture).toBe(true);
    await elapse(0);
    await flush();
    expect(hold.phase).toBe('listening');
    // The page's own cue, once its microphone is open.
    expect(native.impact.mock.calls.map(([style]) => style)).toEqual(['medium']);
    await speak('还在');
    await elapse(600);
    up(el);
    expect(pipeline.takes[1].finish).toHaveBeenCalledTimes(1);
  });

  // On a surface that takes the press for itself the App is not told of the finger's moves,
  // so the slides' haptics are the page's for every hold, the App's own too.
  it('gives the slide haptics of a hold the App recognised itself: rigid into cancel, soft back', async () => {
    const el = await mountFollowed();
    native.impact.mockClear();
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    await elapse(0);
    await flush();
    await speak('不要了');
    // The cue itself was the App's.
    expect(native.impact).not.toHaveBeenCalled();
    move(el, 500 - 81);
    expect(hold.phase).toBe('armed');
    move(el, 500 - 59);
    expect(hold.phase).toBe('recording');
    expect(native.impact.mock.calls.map(([style]) => style)).toEqual(['rigid', 'soft']);
    expect(native.selection).not.toHaveBeenCalled();
  });

  it('keeps a press\'s surface to its end, whatever a new press would be told', async () => {
    // As a composer: no hold once its field is focused, which a press may itself bring about.
    let open = true;
    const el = await mountFollowed({ canHold: () => open });
    down(el);
    open = false;
    await elapse(20);
    // Not taken back under the finger: the App would stop following this press.
    expect(namedSurfaces()).toHaveLength(1);
    await elapse(170);
    await say({ phase: 'recognised', take: 'n1', surface: surfaceId() });
    expect(pipeline.takes).toHaveLength(1);
    expect(native.reject).not.toHaveBeenCalled();
  });

  it('takes its surface back while a take is finalized, and names it again after', async () => {
    const el = await mountFollowed();
    const id = surfaceId();
    down(el);
    await elapse(200);
    await say({ phase: 'recognised', take: 'n1', surface: id });
    await elapse(0);
    await flush();
    await speak('你好');
    // Still its own surface while the take records: the App follows no press during a take.
    await elapse(600);
    expect(namedSurfaces()).toHaveLength(1);
    up(el);
    expect(hold.phase).toBe('settling');
    await elapse(20);
    expect(namedSurfaces()).toEqual([]);
    // A press now is the pending take's, not a hold.
    await say({ phase: 'recognised', take: 'n2', surface: id });
    expect(native.reject).toHaveBeenLastCalledWith('n2');

    await act(async () => pipeline.takes[0].resolve('你好'));
    await flush();
    expect(hold.phase).toBe('idle');
    await elapse(20);
    expect(namedSurfaces()).toEqual([{ id, x: 0, y: 470, width: 300, height: 60 }]);
  });

  // The off switch (`audio_asr.app_hold`): with it off the page behaves as it did before the App
  // recognised holds, in the same App build.
  it('names nothing and holds as before while the instance has the App\'s recognition turned off', async () => {
    pipeline.asr.appHold = false;
    const el = await mountFollowed();
    expect(native.surfaces.mock.calls.every(([surfaces]) => surfaces.length === 0)).toBe(true);
    native.impact.mockClear();
    down(el);
    // Its own threshold, with no wait for the App, its own start, and its own cue.
    await elapse(200);
    await flush();
    await elapse(0);
    await flush();
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
    // The trace says why the App did not follow it.
    expect(pipeline.takes[0].input.holdMiss).toBe('off');
    expect(hold.phase).toBe('listening');
    expect(native.impact).toHaveBeenCalledWith('medium');
    up(el);
    cleanup();
    await elapse(20);

    // Turned on again: the next page that reads the status names its surface.
    pipeline.asr.appHold = true;
    native.surfaces.mockClear();
    await mountFollowed();
    expect(namedSurfaces()).toHaveLength(1);
  });

  it('names nothing in an App that does not recognise holds, and holds as before', async () => {
    native.capture.mockReturnValue(true);
    native.permission.mockResolvedValue('granted');
    const el = await mount();
    el.getBoundingClientRect = () => new DOMRect(0, 470, 300, 60);
    await elapse(20);
    expect(native.surfaces).not.toHaveBeenCalled();
    await holdFor(el);
    expect(pipeline.takes).toHaveLength(1);
    expect(pipeline.takes[0].input.nativeCapture).toBe(true);
  });
});

describe('App Lock cancels held voice rather than preserving an interruption', () => {
  let revision = 100;
  const signal = (locked: boolean) => act(() => {
    Object.assign(window, { __AVIBE_VOICE_LOCK__: { revision: ++revision, locked } });
    window.dispatchEvent(new Event('avibe:voice-lock'));
  });
  it('retains late successful retry words on the same interruption until explicit send', async () => {
    const el = await mount();
    try {
      signal(false); await holdFor(el); await elapse(1_000); up(el);
      await act(async () => pipeline.takes[0].reject(new VoiceTranscriptionError('unavailable'))); await flush();
      let resolve!: (text: string) => void;
      pipeline.takes[0].retry.mockReturnValueOnce(new Promise((ok) => { resolve = ok; }));
      await act(async () => hold.retry());
      const original = hold.text;
      signal(true);
      await act(async () => resolve('retained recovered words'));
      expect(hold.phase).toBe('interrupted'); expect(hold.text).toBe(original);
      expect(send).not.toHaveBeenCalled(); expect(pipeline.takes[0].abort).not.toHaveBeenCalled();
      signal(false);
      expect(hold.text).toBe('retained recovered words'); expect(hold.canRetry).toBe(false);
      await act(async () => hold.sendInterrupted());
      expect(send).toHaveBeenCalledExactlyOnceWith('retained recovered words', { input: 'voice' });
      expect(pipeline.takes[0].retry).toHaveBeenCalledOnce();
    } finally { Reflect.deleteProperty(window, '__AVIBE_VOICE_LOCK__'); }
  });
  it.each(['pressing', 'starting', 'listening', 'settling'] as const)('invalidates %s, late pointer release and final words', async (phase) => {
    const el = await mount();
    try {
      signal(false);
      if (phase === 'starting') pipeline.startMs = 5000;
      down(el);
      if (phase !== 'pressing') await act(async () => { await vi.advanceTimersByTimeAsync(phase === 'starting' ? 250 : 1000); });
      const old = pipeline.takes[0];
      if (phase === 'settling') up(el);
      signal(true);
      expect(hold.phase).toBe('idle');
      if (old) { expect(old.abort).toHaveBeenCalled(); expect(old.setAside).not.toHaveBeenCalled(); }
      up(el); down(el);
      signal(false);
      await act(async () => { old?.resolve('stale words'); await vi.advanceTimersByTimeAsync(6000); });
      expect(send).not.toHaveBeenCalled(); expect(onTap).not.toHaveBeenCalled();
      expect(hold.phase).toBe('idle');
      pipeline.startMs = 0;
      down(el); await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      up(el); await act(async () => pipeline.takes.at(-1)!.resolve('fresh words'));
      expect(send).toHaveBeenCalledWith('fresh words', expect.anything());
    } finally { Reflect.deleteProperty(window, '__AVIBE_VOICE_LOCK__'); }
  });
});
