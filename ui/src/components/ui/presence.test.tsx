/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MotionPreferenceContext } from '@/lib/motion';
import { Presence } from './presence';

// A count that goes when it is read (docs/plans/web-ui-motion.md M7): it shrinks
// and fades, still showing its last number, and is gone once that ends. jsdom has
// no Web Animations, so `animate` is recorded and finished by hand.

type Played = { element: Element; keyframes: Keyframe[]; options: KeyframeAnimationOptions; finish: () => void };
let played: Played[] = [];

function install() {
  HTMLElement.prototype.animate = function animate(this: HTMLElement, keyframes: Keyframe[] | PropertyIndexedKeyframes | null, options?: number | KeyframeAnimationOptions) {
    let finish!: () => void;
    const animation = { cancel: vi.fn() } as unknown as Animation & { finished: Promise<Animation> };
    animation.finished = new Promise<Animation>((resolve) => { finish = () => resolve(animation); });
    played.push({ element: this, keyframes: keyframes as Keyframe[], options: options as KeyframeAnimationOptions, finish });
    return animation;
  };
}

beforeEach(() => {
  played = [];
  document.documentElement.style.setProperty('--motion-duration-fast', '160ms');
  document.documentElement.style.setProperty('--motion-scale-morph', '0.6');
});
afterEach(() => {
  cleanup();
  delete (HTMLElement.prototype as { animate?: unknown }).animate;
});

const Count = ({ count, reduced = null }: { count: number; reduced?: boolean | null }) => (
  <MotionPreferenceContext.Provider value={reduced}>
    <p>
      <Presence show={count > 0} value={count}>{(value) => <span data-testid="count">{value}</span>}</Presence>
      <span>Open</span>
    </p>
  </MotionPreferenceContext.Provider>
);

describe('Presence', () => {
  it('is simply there on mount: nothing replays', () => {
    install();
    render(<Count count={2} />);
    expect(screen.getByTestId('count').textContent).toBe('2');
    expect(played).toEqual([]);
  });

  it('keeps its last count while it shrinks away, inert, and is gone when that ends', async () => {
    install();
    const view = render(<Count count={2} />);
    view.rerender(<Count count={0} />);
    const count = screen.getByTestId('count');
    expect(count.textContent).toBe('2');
    expect(count.hasAttribute('inert')).toBe(true);
    const [exit] = played;
    expect(exit.keyframes.at(-1)).toEqual({ opacity: 0, transform: 'scale(0.6)' });
    expect(exit.options).toMatchObject({ duration: 160, fill: 'forwards' });
    await act(async () => exit.finish());
    expect(screen.queryByTestId('count')).toBeNull();
  });

  it('fades without shrinking under reduced motion', () => {
    install();
    const view = render(<Count count={1} reduced />);
    view.rerender(<Count count={0} reduced />);
    expect(played[0].keyframes).toEqual([{ opacity: 1 }, { opacity: 0 }]);
  });

  it('comes back growing in when there is something to count again', () => {
    install();
    const view = render(<Count count={0} />);
    expect(screen.queryByTestId('count')).toBeNull();
    view.rerender(<Count count={3} />);
    expect(screen.getByTestId('count').textContent).toBe('3');
    expect(played.map((entry) => entry.keyframes[0])).toEqual([{ transform: 'scale(0.6)' }, { opacity: 0 }]);
  });

  it('goes at once where nothing can animate', () => {
    const view = render(<Count count={2} />);
    view.rerender(<Count count={0} />);
    expect(screen.queryByTestId('count')).toBeNull();
  });
});
