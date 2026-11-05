/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MotionPreferenceContext } from '@/lib/motion';
import { FlipList } from './flip-list';

// The list motion as the browser runs it (docs/plans/web-ui-motion.md M7): jsdom
// has no layout and no Web Animations, so each row's box is its place in the list
// (48 px rows), `animate` records what would play, and `frame()` runs the next
// animation frame, where the list measures and starts its motion. The motion tokens are the
// document's (`:root`), set here to the values `src/index.css` gives them.

const TOKENS: Record<string, string> = {
  '--motion-duration-instant': '100ms',
  '--motion-duration-fast': '160ms',
  '--motion-duration-base': '240ms',
  '--motion-ease-enter': 'cubic-bezier(0.22, 1, 0.36, 1)',
  '--motion-ease-exit': 'cubic-bezier(0.4, 0, 1, 1)',
  '--motion-spring-smooth': 'linear(0, 1)',
  '--motion-spring-smooth-duration': '440ms',
  '--motion-handover-duration': '240ms',
  '--motion-handover-ease': 'cubic-bezier(0.22, 1, 0.36, 1)',
  '--motion-beat': '120ms',
  '--motion-offset': '8px',
  '--motion-scale-enter-sm': '0.98',
};

let frames: FrameRequestCallback[] = [];
/** Run the next animation frame. */
const frame = () => act(() => {
  const due = frames;
  frames = [];
  due.forEach((callback) => callback(performance.now()));
});

type Played = { element: HTMLElement; keyframes: Keyframe[]; options: KeyframeAnimationOptions; animation: Animation };
let played: Played[] = [];

function fakeAnimation(): Animation {
  let settle!: () => void;
  let fail!: (reason: unknown) => void;
  const finished = new Promise<Animation>((resolve, reject) => {
    settle = () => resolve(animation);
    fail = reject;
  });
  finished.catch(() => {});
  const animation = { finished, cancel: vi.fn(() => fail(new Error('cancelled'))), finish: () => settle() } as unknown as Animation;
  return animation;
}

beforeEach(() => {
  played = [];
  frames = [];
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => frames.push(callback));
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
  for (const [name, value] of Object.entries(TOKENS)) document.documentElement.style.setProperty(name, value);
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
  HTMLElement.prototype.animate = function animate(this: HTMLElement, keyframes: Keyframe[] | PropertyIndexedKeyframes | null, options?: number | KeyframeAnimationOptions) {
    const animation = fakeAnimation();
    played.push({ element: this, keyframes: keyframes as Keyframe[], options: options as KeyframeAnimationOptions, animation });
    return animation;
  };
  // A row's box is its place among the rows (48 px each); the list sits at the top.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rect(this: HTMLElement) {
    const rows = [...(this.parentElement?.querySelectorAll(':scope > [data-flip-id]') ?? [])];
    const index = rows.indexOf(this);
    const top = index < 0 ? 0 : index * 48;
    return { top, left: 0, width: 360, height: index < 0 ? 0 : 48, right: 360, bottom: top + 48, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete (HTMLElement.prototype as { animate?: unknown }).animate;
  for (const name of Object.keys(TOKENS)) document.documentElement.style.removeProperty(name);
});

const List = ({ ids, group, reduced = null }: { ids: string[]; group?: string; reduced?: boolean | null }) => (
  <MotionPreferenceContext.Provider value={reduced}>
    <FlipList ids={ids} group={group} className="flex flex-col">
      {ids.map((id) => <article key={id} data-flip-id={id} data-inbox-session-id={id} id={`row-${id}`}>{id}</article>)}
    </FlipList>
  </MotionPreferenceContext.Provider>
);

const on = (id: string) => played.filter((entry) => entry.element.getAttribute('data-flip-id') === id);
const ghosts = () => [...document.querySelectorAll<HTMLElement>('[data-flip-ghost]')];
const moves = (entries: Played[]) => entries.filter((entry) => entry.keyframes.some((frame) => 'transform' in frame));

describe('FlipList', () => {
  it('moves a card a reply took to the top: its copy leaves the old slot, the cards above glide down, it comes in on top', () => {
    const view = render(<List ids={['a', 'b', 'c', 'd']} />);
    expect(played).toEqual([]);
    view.rerender(<List ids={['c', 'a', 'b', 'd']} />);
    frame();

    // The old slot: a copy of the card as it was, drawn where it was, not a row of the list.
    const [ghost] = ghosts();
    expect(ghost.textContent).toBe('c');
    expect(ghost.style.top).toBe('96px');
    expect(ghost.hasAttribute('data-inbox-session-id') || ghost.hasAttribute('data-flip-id') || ghost.id).toBeFalsy();
    expect(ghost.getAttribute('aria-hidden')).toBe('true');
    const leave = played.find((entry) => entry.element === ghost)!;
    expect(leave.keyframes.at(-1)).toEqual({ opacity: 0, transform: 'scale(0.98)' });
    expect(leave.options.duration).toBe(100);

    // The cards it passed glide down from where they were, once the copy has gone.
    for (const id of ['a', 'b']) {
      const [glide] = on(id);
      expect(glide.keyframes[0]).toEqual({ transform: 'translate(0px, -48px)' });
      expect(glide.options).toMatchObject({ duration: 440, delay: 100, fill: 'backwards' });
    }
    expect(on('d')).toEqual([]);

    // It comes in at the top a beat after the room is made: from above and a little smaller, fading.
    const enter = on('c');
    expect(enter.map((entry) => entry.options.delay)).toEqual([100 + 160 + 60, 100 + 160 + 60]);
    expect(enter[0].keyframes[0]).toMatchObject({ transform: 'translateY(-8px) scale(0.98)' });
    expect(enter[1].keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }]);

    // The copy goes when its fade ends.
    act(() => (leave.animation as unknown as { finish: () => void }).finish());
    return Promise.resolve().then(() => expect(ghosts()).toEqual([]));
  });

  it('removes a card: it fades where it was, then the cards below glide up', () => {
    const view = render(<List ids={['a', 'b', 'c']} />);
    view.rerender(<List ids={['a', 'c']} />);
    frame();
    const leave = played.find((entry) => entry.element === ghosts()[0])!;
    expect(leave.options.duration).toBe(160);
    const [glide] = on('c');
    expect(glide.keyframes[0]).toEqual({ transform: 'translate(0px, 48px)' });
    expect(glide.options.delay).toBe(160);
    expect(on('a')).toEqual([]);
  });

  it('under reduced motion nothing travels: what changed fades out and in on `fast`', () => {
    const view = render(<List ids={['a', 'b', 'c', 'd']} reduced />);
    view.rerender(<List ids={['c', 'a', 'b', 'd']} reduced />);
    frame();
    expect(moves(played)).toEqual([]);
    expect(played.every((entry) => entry.options.duration === 160)).toBe(true);
    expect(on('c').map((entry) => entry.keyframes)).toEqual([[{ opacity: 0 }, { opacity: 1 }]]);
    // c's old place is b's now: no copy fades over it.
    expect(ghosts()).toHaveLength(0);
  });

  it('fades instead of moving during a burst of live changes', () => {
    const view = render(<List ids={['a', 'b', 'c', 'd', 'e']} />);
    const orders = [['b', 'a', 'c', 'd', 'e'], ['c', 'b', 'a', 'd', 'e'], ['d', 'c', 'b', 'a', 'e']];
    for (const ids of orders) {
      view.rerender(<List ids={ids} />);
      frame();
    }
    expect(moves(played).length).toBeGreaterThan(0);
    played = [];
    view.rerender(<List ids={['e', 'd', 'c', 'b', 'a']} />);
    frame();
    expect(played.length).toBeGreaterThan(0);
    expect(moves(played)).toEqual([]);
  });

  it('cross-fades a change of more than five rows on the hand-over tokens', () => {
    const view = render(<List ids={['a', 'b']} />);
    view.rerender(<List ids={['n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'a', 'b']} />);
    frame();
    expect(moves(played)).toEqual([]);
    expect(on('n1')[0].options).toMatchObject({ duration: 240, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' });
  });

  it('does not move on the first rows (the skeleton hand-over owns that) or across a change of group', () => {
    const view = render(<List ids={[]} />);
    view.rerender(<List ids={['a', 'b']} />);
    frame();
    expect(played).toEqual([]);
    view.rerender(<List ids={['b', 'a']} group="all" />);
    frame();
    expect(played).toEqual([]);
    expect(ghosts()).toEqual([]);
  });

  it('retargets a row caught mid-glide: the running move is cancelled and the next starts from where the row was seen', () => {
    const view = render(<List ids={['a', 'b', 'c']} />);
    view.rerender(<List ids={['n', 'a', 'b', 'c']} />);
    frame();
    const [first] = on('a');
    view.rerender(<List ids={['m', 'n', 'a', 'b', 'c']} />);
    frame();
    expect(first.animation.cancel).toHaveBeenCalled();
    expect(on('a')).toHaveLength(2);
  });

  it('waits for the frame to measure, and takes changes committed before it as one, from what was on screen', () => {
    const view = render(<List ids={['a', 'b', 'c', 'd']} />);
    view.rerender(<List ids={['c', 'a', 'b', 'd']} />);
    expect(played).toEqual([]);
    view.rerender(<List ids={['d', 'c', 'a', 'b']} />);
    frame();
    // On screen was a, b, c, d: c and d both leave their slots and come in at the top; a and b glide down two rows.
    expect(ghosts().map((ghost) => ghost.textContent).sort()).toEqual(['c', 'd']);
    expect(moves(on('a'))[0].keyframes[0]).toEqual({ transform: 'translate(0px, -96px)' });
  });
});
