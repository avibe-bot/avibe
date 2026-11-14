/* @vitest-environment jsdom */

// A blur commits a typed chip at once; while the pointer press that moved focus is still going on,
// the chip waits to appear, so its row cannot wrap (and move the pressed button) mid-press.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { TagInput } from './tag-input';

const changes: string[][] = [];
const pending: boolean[] = [];
function Harness({ initial = [] as string[] }) {
  const [values, setValues] = useState(initial);
  return (
    <TagInput
      values={values}
      onChange={(next) => {
        changes.push(next);
        setValues(next);
      }}
      onPendingChange={(next) => pending.push(next)}
      normalize={(raw) => (/\s/.test(raw.trim()) || !raw.trim() ? null : raw.trim())}
      ariaLabel="Tags"
    />
  );
}
const input = () => screen.getByLabelText('Tags') as HTMLInputElement;
const chip = (value: string) => screen.queryByRole('button', { name: `Remove ${value}` });
const type = (text: string) => {
  fireEvent.focus(input());
  fireEvent.change(input(), { target: { value: text } });
};

beforeEach(() => {
  vi.useFakeTimers();
  changes.length = 0;
  pending.length = 0;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('commits and shows the chip at once on a blur with no press (Tab)', () => {
  render(<Harness />);
  type('prod');
  fireEvent.blur(input());
  expect(changes.at(-1)).toEqual(['prod']);
  expect(chip('prod')).toBeTruthy();
  expect(input().value).toBe('');
  expect(pending.at(-1)).toBe(false);
});

it('commits on a blur during a press, and shows the chip only after that press’s click', () => {
  render(<Harness initial={['ai']} />);
  type('release-tag');
  fireEvent.pointerDown(window);
  fireEvent.blur(input());
  // Committed: the form already holds it, and nothing is pending.
  expect(changes.at(-1)).toEqual(['ai', 'release-tag']);
  expect(pending.at(-1)).toBe(false);
  // Drawn as before until the press is over: no new chip, the typed text in place.
  expect(chip('release-tag')).toBeNull();
  expect(chip('ai')).toBeTruthy();
  expect(input().value).toBe('release-tag');
  fireEvent.pointerUp(window);
  fireEvent.click(window);
  expect(chip('release-tag')).toBeNull();
  act(() => { vi.advanceTimersByTime(0); });
  expect(chip('release-tag')).toBeTruthy();
  expect(input().value).toBe('');
  expect(changes).toHaveLength(1);
});

it.each([
  ['a cancelled press', () => fireEvent.pointerCancel(window), 0],
  ['a release with no click', () => fireEvent.pointerUp(window), 600],
])('shows the held chip after %s', (_, end, wait) => {
  render(<Harness />);
  type('prod');
  fireEvent.pointerDown(window);
  fireEvent.blur(input());
  end();
  act(() => { vi.advanceTimersByTime(Math.max(0, wait - 1)); });
  if (wait) expect(chip('prod')).toBeNull();
  act(() => { vi.advanceTimersByTime(1); });
  expect(chip('prod')).toBeTruthy();
});

it('keeps an invalid draft as a pending draft on a blur during a press, as on any blur', () => {
  render(<Harness />);
  type('two words');
  fireEvent.pointerDown(window);
  fireEvent.blur(input());
  expect(changes).toEqual([]);
  expect(pending.at(-1)).toBe(true);
  expect(input().value).toBe('two words');
});

it('stops listening when it unmounts, even with a chip held', () => {
  const remove = vi.spyOn(window, 'removeEventListener');
  const { unmount } = render(<Harness />);
  type('prod');
  fireEvent.pointerDown(window);
  fireEvent.blur(input());
  unmount();
  expect(remove.mock.calls.map(([event]) => event).sort()).toEqual(['click', 'pointercancel', 'pointerdown', 'pointerup']);
  // The held chip's release has nothing left to update.
  fireEvent.click(window);
  act(() => { vi.advanceTimersByTime(1000); });
  remove.mockRestore();
});
