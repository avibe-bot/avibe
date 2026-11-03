/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { VoiceTranscriptionError } from '../../lib/voiceTranscription';
import { useHoldToTalk, type HoldToTalkOptions } from './useHoldToTalk';

// The hold's whole path (ui/design: mobile/main-bot-voice/*) against a stand-in for the
// dictation pipeline: what a press becomes, what is sent, and what is kept when a take
// is cut off. The pipeline itself is ShowPageVoiceDictation's own test.

type Fake = {
  input: { onPreviewParts?: (preview: { text: string; stash: string }) => void };
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
        start: vi.fn(async () => undefined),
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

class PointerEventShim extends MouseEvent {
  pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
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
const down = (el: HTMLElement, y = 500) => fireEvent.pointerDown(el, { button: 0, pointerId: 1, clientX: 100, clientY: y });
const move = (el: HTMLElement, y: number, x = 100) => fireEvent.pointerMove(el, { pointerId: 1, clientX: x, clientY: y });
const up = (el: HTMLElement, y = 500) => fireEvent.pointerUp(el, { pointerId: 1, clientX: 100, clientY: y });
async function holdFor(el: HTMLElement, ms = 300) {
  down(el);
  await act(async () => {
    vi.advanceTimersByTime(ms);
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
  send.mockReset().mockResolvedValue(true);
  onTap.mockReset();
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
    await elapse(200);
    up(el);
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(pipeline.takes).toHaveLength(0);
    expect(hold.phase).toBe('idle');
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

  it('records from 300 ms, shows the live words, and sends the final text as a voice message on release', async () => {
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

  it('lets the permission prompt take the first hold, and says why a refused one does nothing', async () => {
    pipeline.permission = 'prompt';
    const el = await mount();
    await holdFor(el);
    expect(hold.outcome).toBe('asking');
    expect(pipeline.takes).toHaveLength(0);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalled();

    pipeline.permission = 'denied';
    up(el);
    await holdFor(el);
    expect(hold.outcome).toBe('denied');
    expect(pipeline.takes).toHaveLength(0);
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
});
