// @vitest-environment jsdom
// Dragging a phone sheet (docs/plans/web-ui-motion.md M6) as its listeners see a finger: what a release
// does depends on how far and how fast the finger was going as it let go, and a drag survives its owner
// re-rendering. jsdom has no touch input, so the events carry their touches as plain properties.
import { useState } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useSheetDrag } from './sheetDrag';

function Sheet({ onDismiss }: { onDismiss: () => void }) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  useSheetDrag(node, { enabled: true, onDismiss });
  return <div ref={setNode} data-state="open"><h2>Every Bot</h2></div>;
}
let sheet: HTMLElement;

/** A touch event at `y`, `t` ms into the gesture. */
function touch(type: string, y: number, t: number) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  const point = { identifier: 1, clientX: 100, clientY: y };
  Object.defineProperties(event, {
    touches: { value: type === 'touchend' ? [] : [point] },
    changedTouches: { value: [point] },
    timeStamp: { value: t },
  });
  act(() => {
    screen.getByRole('heading').dispatchEvent(event);
  });
}

function mount(onDismiss: () => void) {
  const view = render(<Sheet onDismiss={onDismiss} />);
  sheet = screen.getByRole('heading').parentElement!;
  vi.spyOn(sheet, 'getBoundingClientRect').mockReturnValue({ height: 600 } as DOMRect);
  return view;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('dragging a phone sheet', () => {
  it('springs back when the finger stopped before letting go, however fast it moved first', () => {
    const onDismiss = vi.fn();
    mount(onDismiss);
    touch('touchstart', 100, 0);
    touch('touchmove', 110, 8);
    touch('touchmove', 150, 16);
    touch('touchmove', 180, 24);
    // Held still for half a second, 70 px down (under 30% of 600), then let go.
    touch('touchend', 180, 524);
    expect(onDismiss).not.toHaveBeenCalled();
    expect(sheet.style.transform).toBe('');
  });

  it('dismisses a flick that is still moving as it lets go', () => {
    const onDismiss = vi.fn();
    mount(onDismiss);
    touch('touchstart', 100, 0);
    touch('touchmove', 110, 8);
    touch('touchmove', 150, 16);
    touch('touchmove', 180, 24);
    touch('touchend', 180, 30);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('keeps a drag going when its owner re-renders with a new close, and closes with the newest', () => {
    const first = vi.fn();
    const latest = vi.fn();
    const view = mount(first);
    touch('touchstart', 100, 0);
    touch('touchmove', 110, 16);
    touch('touchmove', 200, 200);
    view.rerender(<Sheet onDismiss={latest} />);
    touch('touchmove', 400, 400);
    expect(sheet.style.transform).toBe('translateY(290px)');
    touch('touchend', 400, 410);
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);
  });
});
