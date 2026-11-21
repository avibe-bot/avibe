/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  holdFollowed,
  refreshHoldSurfaces,
  registerHoldSurface,
  resetHoldSurfacesForTest,
  type HoldSurface,
} from './nativeVoiceHold';

// The list of hold surfaces the iOS App has (ios/Avibe/NativeHoldCapture.swift), against a
// stand-in bridge: what the page names, when it names it again, and what it does with the
// App's word about a press.

type HoldDetail = { phase: string; take: string; surface?: string; step?: string };

let call: ReturnType<typeof vi.fn>;
let listeners: ((detail: HoldDetail) => void)[] = [];
/** The App's answers to `voice.holdSurfaces`, in order; `ok` once they run out. */
let answers: (string | Promise<unknown>)[] = [];

const install = (capabilities: Record<string, number> = { voice: 1, voiceCapture: 1, voiceHold: 1 }) => {
  listeners = [];
  call = vi.fn(async (method: string) => {
    if (method !== 'voice.holdSurfaces') return null;
    const answer = answers.shift() ?? 'ok';
    if (typeof answer !== 'string') return answer;
    if (answer !== 'ok') throw { code: 'failed', message: answer };
    return null;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities,
      call,
      on: (event: string, handler: (detail: HoldDetail) => void) => {
        expect(event).toBe('voice.hold');
        listeners.push(handler);
        return () => {
          listeners = listeners.filter((listener) => listener !== handler);
        };
      },
    },
  });
};

const viewport = (content: string) => {
  let meta = document.querySelector('meta[name="viewport"]');
  if (!meta) {
    meta = document.createElement('meta');
    meta.setAttribute('name', 'viewport');
    document.head.append(meta);
  }
  meta.setAttribute('content', content);
};

const box = (left: number, top: number, width: number, height: number): HTMLElement => {
  const element = document.createElement('div');
  document.body.append(element);
  element.getBoundingClientRect = () => new DOMRect(left, top, width, height);
  return element;
};

const surface = (element: Element | null, overrides: Partial<HoldSurface> = {}): HoldSurface => ({
  holdMs: 200,
  element: () => element,
  recognised: vi.fn(() => true),
  failed: vi.fn(),
  ...overrides,
});

/** One frame, and the App's answer. */
const settle = async () => {
  await vi.advanceTimersByTimeAsync(20);
  await Promise.resolve();
};
const named = () => call.mock.calls.filter(([method]) => method === 'voice.holdSurfaces').map(([, params]) => params);
const emit = (detail: HoldDetail) => {
  for (const listener of [...listeners]) listener(detail);
};

beforeEach(() => {
  vi.useFakeTimers();
  answers = [];
  install();
  viewport('width=device-width, initial-scale=1.0, viewport-fit=cover');
  resetHoldSurfacesForTest();
});
afterEach(() => {
  resetHoldSurfacesForTest();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  delete (window as { avibeNative?: unknown }).avibeNative;
});

describe('the hold surfaces the iOS App follows', () => {
  it('names the box of a surface where a hold may record now, with the threshold', async () => {
    const unregister = registerHoldSurface('composer', surface(box(12, 700.4, 366, 60)));
    await settle();
    expect(named()).toEqual([{ surfaces: [{ id: 'composer', x: 12, y: 700.5, width: 366, height: 60 }], holdMs: 200 }]);
    // The App said it follows presses there: inside the box, and nowhere else.
    expect(holdFollowed('composer', 100, 720)).toBe(true);
    expect(holdFollowed('composer', 100, 600)).toBe(false);
    expect(holdFollowed('other', 100, 720)).toBe(false);

    // Nothing moved: nothing is sent again.
    refreshHoldSurfaces();
    await settle();
    expect(named()).toHaveLength(1);

    // Taken back: the App follows no press.
    unregister();
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
    expect(holdFollowed('composer', 100, 720)).toBe(false);
  });

  it('gives the boxes in the WebView\'s own points: without the visual viewport\'s offset, at its scale', async () => {
    vi.stubGlobal('visualViewport', { scale: 2, offsetLeft: 10, offsetTop: 100, addEventListener: vi.fn(), removeEventListener: vi.fn() });
    registerHoldSurface('composer', surface(box(20, 400, 100, 50)));
    await settle();
    expect(named()).toEqual([{ surfaces: [{ id: 'composer', x: 20, y: 600, width: 200, height: 100 }], holdMs: 200 }]);
    expect(holdFollowed('composer', 30, 410)).toBe(true);
    expect(holdFollowed('composer', 30, 390)).toBe(false);
  });

  it('names a surface again when it moved, and none while no hold may record there', async () => {
    let top = 700;
    let element: HTMLElement | null = box(0, 0, 390, 60);
    element.getBoundingClientRect = () => new DOMRect(0, top, 390, 60);
    registerHoldSurface('composer', surface(null, { element: () => element }));
    await settle();
    top = 400; // the keyboard came up
    window.dispatchEvent(new Event('resize'));
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [{ id: 'composer', x: 0, y: 400, width: 390, height: 60 }], holdMs: 200 });
    element = null; // text in the box: a press there is no hold
    refreshHoldSurfaces();
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
    expect(named()).toHaveLength(3);
    expect(holdFollowed('composer', 100, 420)).toBe(false);
  });

  it('names nothing in a document that does not cover the viewport: its inset is unknown', async () => {
    viewport('width=device-width, initial-scale=1.0');
    registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    await settle();
    expect(named()).toEqual([]);
    // The App's shell made it cover (its outer scroll lock): named now.
    viewport('width=device-width, initial-scale=1.0, viewport-fit=cover');
    await settle();
    expect(named()).toHaveLength(1);
    viewport('width=device-width, initial-scale=1.0');
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
  });

  it('does not count on a press being followed unless the App said so', async () => {
    for (const refusal of ['consent', 'unwatched', 'invalid']) {
      resetHoldSurfacesForTest();
      answers = [refusal];
      registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
      await settle();
      expect(holdFollowed('composer', 100, 720), refusal).toBe(false);
    }
  });

  it('takes the last list\'s answer when lists follow each other faster than the App answers', async () => {
    let top = 700;
    const element = box(0, 0, 390, 60);
    element.getBoundingClientRect = () => new DOMRect(0, top, 390, 60);
    let answerFirst!: (error: unknown) => void;
    answers = [new Promise((_, reject) => { answerFirst = reject; }), 'ok'];
    registerHoldSurface('composer', surface(element));
    await settle();
    top = 400;
    refreshHoldSurfaces();
    await settle();
    // The second list is in force; the first one's late `superseded` changes nothing.
    answerFirst({ code: 'failed', message: 'superseded' });
    await settle();
    expect(holdFollowed('composer', 100, 420)).toBe(true);
    expect(holdFollowed('composer', 100, 720)).toBe(false);
  });

  it('sends the App one threshold, and at most eight surfaces', async () => {
    registerHoldSurface('first', surface(box(0, 0, 10, 10)));
    registerHoldSurface('slow', surface(box(0, 20, 10, 10), { holdMs: 350 }));
    for (let index = 0; index < 9; index += 1) registerHoldSurface(`more-${index}`, surface(box(0, 40 + index * 20, 10, 10)));
    registerHoldSurface('empty', surface(box(0, 400, 0, 10)));
    await settle();
    const [{ surfaces, holdMs }] = named() as [{ surfaces: { id: string }[]; holdMs: number }];
    expect(holdMs).toBe(200);
    expect(surfaces.map(({ id }) => id)).toEqual(['first', ...Array.from({ length: 7 }, (_, index) => `more-${index}`)]);
  });

  it('hands a hold the App recognised to its surface, and refuses one no surface takes', async () => {
    const taking = surface(box(0, 700, 390, 60));
    const refusing = surface(box(0, 600, 390, 60), { recognised: vi.fn(() => false) });
    registerHoldSurface('composer', taking);
    registerHoldSurface('bot', refusing);
    await settle();
    const rejected = () => call.mock.calls.filter(([method]) => method === 'voice.holdReject').map(([, params]) => params);

    emit({ phase: 'recognised', take: 'n1', surface: 'composer' });
    expect(taking.recognised).toHaveBeenCalledWith('n1');
    expect(rejected()).toEqual([]);

    emit({ phase: 'recognised', take: 'n2', surface: 'bot' });
    emit({ phase: 'recognised', take: 'n3', surface: 'gone' });
    await settle();
    expect(rejected()).toEqual([{ take: 'n2' }, { take: 'n3' }]);

    emit({ phase: 'failed', take: 'n4', surface: 'composer', step: 'engine:1' });
    expect(taking.failed).toHaveBeenCalledTimes(1);
    expect(refusing.failed).not.toHaveBeenCalled();
    // A take's timings at its end are its capture's to keep (lib/nativeVoiceCapture).
    emit({ phase: 'timings', take: 'n1' });
    expect(taking.recognised).toHaveBeenCalledTimes(1);
  });

  it('does nothing in an App that does not recognise holds itself', async () => {
    install({ voice: 1, voiceCapture: 1 });
    const unregister = registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    await settle();
    expect(call).not.toHaveBeenCalled();
    expect(listeners).toHaveLength(0);
    expect(holdFollowed('composer', 100, 720)).toBe(false);
    unregister();
  });
});
