/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mainBotStartsAtRecognition, noteAppVoice } from './appVoice';
import {
  holdFollowed,
  holdMissReason,
  refreshHoldSurfaces,
  registerHoldSurface as registerSurface,
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

const install = (capabilities: Record<string, number> = { voice: 1, voiceCapture: 1, voiceHold: 2 }) => {
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
  live: () => false,
  recognised: vi.fn(() => true),
  failed: vi.fn(),
  ...overrides,
});

/** What a touch at a point reaches: the last of the body's children whose box holds it (a
 *  later child lies over an earlier one), or the page behind them. */
const topAt = (x: number, y: number): Element => (
  [...document.body.children].reverse().find((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  }) ?? document.body
);

/** What the tests registered: taken back after each, as a surface is when its element goes. */
const registered: (() => void)[] = [];
const registerHoldSurface = (id: string, held: HoldSurface): (() => void) => {
  const unregister = registerSurface(id, held);
  registered.push(unregister);
  return unregister;
};
const unregisterAll = async () => {
  for (const unregister of registered.splice(0)) unregister();
  await settle();
};

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
  document.elementFromPoint = topAt;
});
afterEach(async () => {
  noteAppVoice({ app_hold: true, app_hold_main_at_touch: true, app_hold_start_ms: null, app_hold_prepare_at_touch: true });
  // Every surface taken back, and the App told: the next test starts from nothing named.
  await unregisterAll();
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

  // The App acts on a touch before the page sees it: a surface under a sheet would open the
  // microphone for a tap on the sheet.
  it('names no surface that something covers, and names it again when it is uncovered', async () => {
    const composer = box(0, 700, 390, 60);
    registerHoldSurface('composer', surface(composer));
    registerHoldSurface('bot', surface(box(150, 780, 90, 60)));
    await settle();
    expect((named().at(-1) as { surfaces: { id: string }[] }).surfaces.map(({ id }) => id)).toEqual(['composer', 'bot']);

    // A sheet rises over the composer (put on the page as a child of the body).
    const sheet = box(0, 300, 390, 470);
    await settle();
    expect((named().at(-1) as { surfaces: { id: string }[] }).surfaces.map(({ id }) => id)).toEqual(['bot']);
    expect(holdFollowed('composer', 100, 720)).toBe(false);

    sheet.remove();
    await settle();
    expect((named().at(-1) as { surfaces: { id: string }[] }).surfaces.map(({ id }) => id)).toEqual(['composer', 'bot']);

    // Made inert by a modal elsewhere: no touch reaches it.
    const inert = document.createElement('div');
    inert.setAttribute('inert', '');
    document.body.append(inert);
    inert.append(composer);
    inert.getBoundingClientRect = () => new DOMRect(0, 700, 390, 60);
    await settle();
    expect((named().at(-1) as { surfaces: { id: string }[] }).surfaces.map(({ id }) => id)).toEqual(['bot']);
  });

  it('measures once more when what was put on the page has come to rest', async () => {
    const composer = box(0, 700, 390, 60);
    registerHoldSurface('composer', surface(composer));
    await settle();
    // A sheet that starts below the screen and rises: not over the composer when it is added.
    let top = 900;
    const sheet = box(0, 0, 390, 470);
    sheet.getBoundingClientRect = () => new DOMRect(0, top, 390, 470);
    await settle();
    expect(named()).toHaveLength(1);
    top = 300;
    await vi.advanceTimersByTimeAsync(500);
    expect(named().at(-1)).toEqual({ surfaces: [] });
  });

  // A page sliding in is drawn as a picture of itself for a moment: every touch goes to the
  // root, and the composer of the page just opened reads as covered when it is first measured.
  it('measures once more, by itself, a surface that read as covered, and does not go on asking', async () => {
    const composer = box(0, 700, 390, 60);
    const measured = vi.spyOn(composer, 'getBoundingClientRect');
    let sliding = true;
    document.elementFromPoint = (x, y) => (sliding ? document.documentElement : topAt(x, y));
    registerHoldSurface('composer', surface(composer));
    await settle();
    expect(named()).toEqual([]);
    // The slide ends; nothing renders, scrolls or is touched.
    sliding = false;
    await vi.advanceTimersByTimeAsync(500);
    expect(named()).toEqual([{ surfaces: [{ id: 'composer', x: 0, y: 700, width: 390, height: 60 }], holdMs: 200 }]);

    // Under a sheet that stays: measured once more at rest, then left alone.
    sliding = true;
    refreshHoldSurfaces();
    await vi.advanceTimersByTimeAsync(500);
    expect(named().at(-1)).toEqual({ surfaces: [] });
    const measures = measured.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(measured.mock.calls.length).toBe(measures);
  });

  it('measures a sheet where it came to rest though the page behind it scrolls without pause', async () => {
    const composer = box(0, 700, 390, 60);
    registerHoldSurface('composer', surface(composer));
    await settle();
    // Added below the screen, not yet over the composer when it is first measured; then it rises.
    let top = 900;
    const sheet = box(0, 0, 390, 470);
    sheet.getBoundingClientRect = () => new DOMRect(0, top, 390, 470);
    await settle();
    expect(named()).toHaveLength(1);
    top = 300;
    // A reply streaming in: the list scrolls every 100 ms, and never stops.
    for (let step = 0; step < 5; step += 1) {
      window.dispatchEvent(new Event('scroll'));
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(named().at(-1)).toEqual({ surfaces: [] });
  });

  // The main Bot's face is lifted above its button's box (MobileChrome): it is the button's.
  it('names the part of a surface that sticks out of its box, and takes a hold on it', async () => {
    const button = box(150, 780, 90, 60);
    const face = document.createElement('span');
    face.getBoundingClientRect = () => new DOMRect(168, 760, 54, 54);
    const name = document.createElement('span');
    name.getBoundingClientRect = () => new DOMRect(170, 818, 50, 14);
    button.append(face, name);
    const held = surface(button);
    registerHoldSurface('bot', held);
    await settle();
    expect(named()).toEqual([{
      surfaces: [{ id: 'bot', x: 150, y: 780, width: 90, height: 60 }, { id: 'bot_1', x: 168, y: 760, width: 54, height: 54 }],
      holdMs: 200,
    }]);
    // On the face above the box: followed. Beside the face, above the box: not.
    expect(holdFollowed('bot', 195, 765)).toBe(true);
    expect(holdFollowed('bot', 155, 765)).toBe(false);
    emit({ phase: 'recognised', take: 'n1', surface: 'bot_1' });
    expect(held.recognised).toHaveBeenCalledWith('n1');
    expect(call.mock.calls.some(([method]) => method === 'voice.holdReject')).toBe(false);
  });

  // A scrolling input clips its text: a paragraph taller than the box reaches, unseen, over
  // the buttons below it, and a touch there is theirs.
  it('names only the box of a surface that clips what leaves it', async () => {
    const input = box(0, 700, 390, 60);
    input.style.overflowY = 'auto';
    const paragraph = document.createElement('p');
    paragraph.getBoundingClientRect = () => new DOMRect(0, 700, 390, 220);
    input.append(paragraph);
    registerHoldSurface('composer', surface(input));
    await settle();
    expect(named()).toEqual([{ surfaces: [{ id: 'composer', x: 0, y: 700, width: 390, height: 60 }], holdMs: 200 }]);
    expect(holdFollowed('composer', 100, 800)).toBe(false);
  });

  it('keeps a surface as the App has it while a press is live there, whatever lies over it', async () => {
    let live = false;
    const composer = box(0, 700, 390, 60);
    registerHoldSurface('composer', surface(composer, { live: () => live }));
    await settle();
    // The press's own card covers the box, and the box may no longer be one for a new press.
    live = true;
    box(0, 600, 390, 200);
    await vi.advanceTimersByTimeAsync(500);
    expect(named()).toHaveLength(1);
    expect(holdFollowed('composer', 100, 720)).toBe(true);
    live = false;
    refreshHoldSurfaces();
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
  });

  it('measures again after any touch, after a scroll has stopped, and after refusing a hold', async () => {
    let top = 700;
    const composer = box(0, 0, 390, 60);
    composer.getBoundingClientRect = () => new DOMRect(0, top, 390, 60);
    registerHoldSurface('composer', surface(composer, { recognised: vi.fn(() => false) }));
    await settle();
    const tops = () => named().map((params) => (params as { surfaces: { y: number }[] }).surfaces[0]?.y);

    top = 650;
    window.dispatchEvent(new Event('pointerdown'));
    await settle();
    expect(tops()).toEqual([700, 650]);

    // Scrolling with its page: measured where it stops, not at every step.
    for (top = 640; top > 600; top -= 10) {
      window.dispatchEvent(new Event('scroll'));
      await settle();
    }
    expect(tops()).toEqual([700, 650]);
    await vi.advanceTimersByTimeAsync(500);
    expect(tops()).toEqual([700, 650, 600]);

    top = 500;
    emit({ phase: 'recognised', take: 'n1', surface: 'composer' });
    await settle();
    expect(tops()).toEqual([700, 650, 600, 500]);
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
      await unregisterAll();
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

  // A surface may ask that a touch on it open no microphone: the App then starts its input when
  // it recognises the hold. Said for each of the surface's boxes; unsaid, the App's default.
  it('names a surface that starts at recognition with every one of its boxes, and the others without', async () => {
    let atRecognition = true;
    const button = box(150, 780, 90, 60);
    const face = document.createElement('span');
    face.getBoundingClientRect = () => new DOMRect(168, 760, 54, 54);
    button.append(face);
    registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    registerHoldSurface('bot', surface(button, { startsAtRecognition: () => atRecognition }));
    await settle();
    expect(named()).toEqual([{
      surfaces: [
        { id: 'composer', x: 0, y: 700, width: 390, height: 60 },
        { id: 'bot', x: 150, y: 780, width: 90, height: 60, start: 'recognition' },
        { id: 'bot_1', x: 168, y: 760, width: 54, height: 54, start: 'recognition' },
      ],
      holdMs: 200,
    }]);

    // Asked again at each measure: the App is told when the answer changes.
    atRecognition = false;
    refreshHoldSurfaces();
    await settle();
    expect(named().at(-1)).toEqual({
      surfaces: [
        { id: 'composer', x: 0, y: 700, width: 390, height: 60 },
        { id: 'bot', x: 150, y: 780, width: 90, height: 60 },
        { id: 'bot_1', x: 168, y: 760, width: 54, height: 54 },
      ],
      holdMs: 200,
    });
  });

  // The instance's setting for the main Bot's button (`audio_asr.app_hold_main_at_touch`, said
  // by `/api/asr/status`): the list is measured again when it changes, with no reload.
  it('says where the main Bot\'s button starts from the instance\'s setting, and tells the App when it changes', async () => {
    expect(mainBotStartsAtRecognition()).toBe(false);
    registerHoldSurface('bot', surface(box(150, 780, 90, 60), { startsAtRecognition: mainBotStartsAtRecognition }));
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [{ id: 'bot', x: 150, y: 780, width: 90, height: 60 }], holdMs: 200 });

    noteAppVoice({ app_hold: true, app_hold_main_at_touch: false });
    expect(mainBotStartsAtRecognition()).toBe(true);
    await settle();
    expect(named().at(-1)).toEqual({
      surfaces: [{ id: 'bot', x: 150, y: 780, width: 90, height: 60, start: 'recognition' }],
      holdMs: 200,
    });

    // A status body without the field (an instance with no such setting) changes nothing.
    noteAppVoice({ app_hold: true });
    expect(mainBotStartsAtRecognition()).toBe(true);
    // Only `false` turns it; anything else is the default, at touch-down.
    noteAppVoice({ app_hold_main_at_touch: 'no' });
    expect(mainBotStartsAtRecognition()).toBe(false);
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [{ id: 'bot', x: 150, y: 780, width: 90, height: 60 }], holdMs: 200 });
  });

  // The off switch (`audio_asr.app_hold`, said by `/api/asr/status`): no new build needed.
  it('names nothing while the instance has the App\'s hold recognition turned off, and takes back what was named', async () => {
    registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    await settle();
    expect(named()).toHaveLength(1);

    // Turned off while a surface is named: the App is told at once that it follows nothing.
    noteAppVoice({ app_hold: false });
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
    expect(holdFollowed('composer', 100, 720)).toBe(false);
    refreshHoldSurfaces();
    window.dispatchEvent(new Event('pointerdown'));
    await vi.advanceTimersByTimeAsync(500);
    expect(named()).toHaveLength(2);

    // A status body without the field (an instance with no such switch) changes nothing.
    noteAppVoice({ available: true });
    await settle();
    expect(named()).toHaveLength(2);

    noteAppVoice({ app_hold: true });
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [{ id: 'composer', x: 0, y: 700, width: 390, height: 60 }], holdMs: 200 });
    expect(holdFollowed('composer', 100, 720)).toBe(true);
  });

  // An App without it; and the one build that announced version 1 (TestFlight 50.1), which
  // could take a tap for a hold: the page names nothing there, so that App follows nothing.
  it('does nothing in an App that does not recognise holds itself, or whose recognition is the first build\'s', async () => {
    for (const capabilities of [{ voice: 1, voiceCapture: 1 }, { voice: 1, voiceCapture: 1, voiceHold: 1 }]) {
      install(capabilities);
      const unregister = registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
      await settle();
      expect(call, JSON.stringify(capabilities)).not.toHaveBeenCalled();
      expect(listeners).toHaveLength(0);
      expect(holdFollowed('composer', 100, 720)).toBe(false);
      unregister();
    }
  });

  // From `voiceHold` 3 (TestFlight 52.1): when a hold's microphone may start, and whether the App
  // may prepare before, as the instance set them (`audio_asr.app_hold_start_ms`,
  // `audio_asr.app_hold_prepare_at_touch`), on every box; a surface at recognition keeps `start`.
  it('says when the microphone may start and whether to prepare before, to an App that knows them', async () => {
    install({ voice: 1, voiceCapture: 1, voiceHold: 3 });
    noteAppVoice({ app_hold_start_ms: 80, app_hold_prepare_at_touch: false });
    registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    registerHoldSurface('bot', surface(box(150, 780, 90, 60), { startsAtRecognition: () => true }));
    await settle();
    expect(named().at(-1)).toEqual({
      surfaces: [
        { id: 'composer', x: 0, y: 700, width: 390, height: 60, startAfterMs: 80, prepare: 'start' },
        { id: 'bot', x: 150, y: 780, width: 90, height: 60, start: 'recognition', prepare: 'start' },
      ],
      holdMs: 200,
    });

    // Unset again: the App's own default, and preparing at touch-down.
    noteAppVoice({ app_hold_start_ms: null, app_hold_prepare_at_touch: true });
    await settle();
    expect(named().at(-1)).toEqual({
      surfaces: [
        { id: 'composer', x: 0, y: 700, width: 390, height: 60 },
        { id: 'bot', x: 150, y: 780, width: 90, height: 60, start: 'recognition' },
      ],
      holdMs: 200,
    });

    // A time out of 0..500, or not a whole number, is the App's default.
    for (const wrong of [501, -1, 80.5, '80']) {
      noteAppVoice({ app_hold_start_ms: wrong });
      await settle();
      expect(named().at(-1)?.surfaces?.[0], String(wrong)).toEqual({ id: 'composer', x: 0, y: 700, width: 390, height: 60 });
    }
  });

  it('says neither to an App before version 3, which would refuse the list', async () => {
    noteAppVoice({ app_hold_start_ms: 80, app_hold_prepare_at_touch: false });
    registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [{ id: 'composer', x: 0, y: 700, width: 390, height: 60 }], holdMs: 200 });
  });

  // For the capture trace: why the App did not follow a press at a point (the trace's `holdMiss`).
  it('says why the App does not follow a press', async () => {
    let element: Element | null = box(0, 700, 390, 60);
    const held = surface(null, { element: () => element });
    expect(holdMissReason('composer', 100, 720)).toBe('unlisted');
    registerHoldSurface('composer', held);
    await settle();
    expect(holdMissReason('composer', 100, 720)).toBeUndefined();
    // Followed, and the finger outside its box.
    expect(holdMissReason('composer', 100, 300)).toBe('outside');

    // A take being finalized there: no hold may record now.
    const kept = element;
    element = null;
    refreshHoldSurfaces();
    await settle();
    expect(holdMissReason('composer', 100, 720)).toBe('closed');

    // Something over it.
    element = kept;
    const sheet = box(0, 0, 390, 844);
    refreshHoldSurfaces();
    await settle();
    expect(holdMissReason('composer', 100, 720)).toBe('covered');
    sheet.remove();

    // Named, the App's answer still on its way; then refused.
    let answerList!: (error: unknown) => void;
    answers = [new Promise((_, reject) => { answerList = reject; })];
    refreshHoldSurfaces();
    await settle();
    expect(holdMissReason('composer', 100, 720)).toBe('pending');
    answerList({ code: 'failed', message: 'consent' });
    await settle();
    expect(holdMissReason('composer', 100, 720)).toBe('refused');

    // Turned off for the instance, and a document that does not cover the viewport.
    noteAppVoice({ app_hold: false });
    expect(holdMissReason('composer', 100, 720)).toBe('off');
    noteAppVoice({ app_hold: true });
    viewport('width=device-width, initial-scale=1.0');
    expect(holdMissReason('composer', 100, 720)).toBe('viewport');
  });

  it('goes on hearing the App until it has been told the last surface went', async () => {
    const unregister = registerHoldSurface('composer', surface(box(0, 700, 390, 60)));
    await settle();
    unregister();
    // A word already on its way is still refused, not left to the App's own wait.
    emit({ phase: 'recognised', take: 'n1', surface: 'composer' });
    expect(call).toHaveBeenCalledWith('voice.holdReject', { take: 'n1' });
    await settle();
    expect(named().at(-1)).toEqual({ surfaces: [] });
    expect(listeners).toHaveLength(0);
  });
});
