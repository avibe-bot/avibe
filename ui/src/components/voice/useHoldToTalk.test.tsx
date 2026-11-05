/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { VoiceTranscriptionError } from '../../lib/voiceTranscription';
import { MobileBotCenter } from '../MobileChrome';
import { useHoldToTalk, type HoldToTalkOptions } from './useHoldToTalk';

// The hold's whole path (ui/design: mobile/main-bot-voice/*) against a stand-in for the
// dictation pipeline: what a press becomes, what is sent, and what is kept when a take
// is cut off. The pipeline itself is ShowPageVoiceDictation's own test.

type Fake = {
  input: { onPreviewParts?: (preview: { text: string; stash: string }) => void; context?: unknown; onLevel?: (rms: number) => void };
  done: Promise<string>;
  resolve: (text: string) => void;
  reject: (error: unknown) => void;
  start: ReturnType<typeof vi.fn>;
  finish: ReturnType<typeof vi.fn>;
  abort: ReturnType<typeof vi.fn>;
  canRetry: ReturnType<typeof vi.fn>;
  retry: ReturnType<typeof vi.fn>;
};

const pipeline = vi.hoisted(() => ({
  takes: [] as Fake[],
  asr: { available: true },
  permission: 'granted' as PermissionState,
  captureActive: false,
  /** How long the microphone and audio graph take to open. */
  startMs: 0,
}));
const native = vi.hoisted(() => ({
  impact: vi.fn(),
  notify: vi.fn(),
  selection: vi.fn(),
  permission: vi.fn(),
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
        start: vi.fn(() => new Promise<void>((ready) => setTimeout(ready, pipeline.startMs))),
        finish: vi.fn(),
        abort: vi.fn(() => reject(new VoiceTranscriptionError('cancelled'))),
        canRetry: vi.fn(() => true),
        retry: vi.fn(async () => 'heard again'),
      };
      pipeline.takes.push(take);
      return take;
    }
  },
}));
vi.mock('../../lib/apiFetch', () => ({
  apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({ available: pipeline.asr.available }) })),
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
vi.mock('../../lib/native', () => ({
  nativeHapticImpact: native.impact,
  nativeHapticNotify: native.notify,
  nativeHapticSelection: native.selection,
  nativeVoicePermission: native.permission,
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
  return <button type="button" data-testid="held" {...value.bind} />;
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
  pipeline.permission = 'granted';
  pipeline.captureActive = false;
  pipeline.startMs = 0;
  send.mockReset().mockResolvedValue(true);
  onTap.mockReset();
  native.impact.mockReset().mockResolvedValue(true);
  native.notify.mockReset().mockResolvedValue(true);
  native.selection.mockReset().mockResolvedValue(true);
  native.permission.mockReset().mockResolvedValue(null);
  Object.defineProperty(navigator, 'permissions', {
    configurable: true,
    value: { query: vi.fn(async () => ({ state: pipeline.permission })) },
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [] })) },
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useHoldToTalk', () => {
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
    expect(native.selection).toHaveBeenCalledTimes(2);
    expect(native.notify).toHaveBeenCalledWith('warning');
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
    expect(hold.phase).toBe('listening');
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

