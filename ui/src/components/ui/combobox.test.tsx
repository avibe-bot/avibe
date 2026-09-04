// @vitest-environment jsdom
//
// The picker every field in this app selects with. What is asserted here is what
// a call site is allowed to rely on: a row may carry a mark, the trigger shows
// the chosen row's mark as well as its label, a row that carries none is
// rendered exactly as it always was, and a disabled field does not open.
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Combobox, type ComboboxOption } from './combobox';

beforeEach(() => {
  // Radix measures the trigger to size the panel and cmdk scrolls the highlighted
  // row into view; jsdom implements neither.
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const MARK = <svg data-testid="mark" aria-hidden="true" viewBox="0 0 24 24" />;

const marked: ComboboxOption[] = [
  { value: 'a', label: 'Alpha', icon: MARK },
  { value: 'b', label: 'Beta', icon: MARK },
];

const plain: ComboboxOption[] = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta' },
];

const trigger = () => screen.getByRole('combobox', { name: 'Vendor' });

/** The open panel is modal, so it hides the rest of the document from the
 *  accessibility tree — including the trigger. It is taken by hand first, which
 *  is also the only way to compare the field before and after a choice. */
const open = async (options: ComboboxOption[], props: Record<string, unknown> = {}) => {
  const user = userEvent.setup();
  render(<Combobox options={options} value="a" onValueChange={vi.fn()} ariaLabel="Vendor" {...props} />);
  const field = trigger();
  await user.click(field);
  return { user, field };
};

describe('Combobox', () => {
  it('marks each row and keeps the chosen row’s mark on the closed field', async () => {
    // The second half is the reason the mark is a property of the option rather
    // than of the list: a picker whose closed state drops it shows less than the
    // choice already contains, which is what a `<select>` is stuck with.
    const { field } = await open(marked);

    for (const row of screen.getAllByRole('option')) {
      expect(row.querySelector('[data-testid="mark"]'), row.textContent ?? '').toBeTruthy();
    }
    expect(field.querySelector('[data-testid="mark"]')).toBeTruthy();
    expect(field.textContent).toContain('Alpha');
  });

  it('renders a row that carries no mark with nothing added to it', async () => {
    // The model pickers pass `{value, label}` and must be untouched by the mark
    // arriving: the selected row is its check plus its text, and no wrapper.
    const { field } = await open(plain);

    const [selected] = screen.getAllByRole('option');
    expect(selected.textContent).toBe('Alpha');
    expect(selected.querySelectorAll('span')).toHaveLength(0);
    expect(selected.querySelectorAll('svg')).toHaveLength(1);
    // The trigger keeps only its chevron.
    expect(field.querySelectorAll('svg')).toHaveLength(1);
  });

  it('names the trigger for a field label that cannot reach it', async () => {
    // A `<label for>` does not name a button, so a labelled field passes both the
    // id it minted and the label text; the value stays the trigger's contents.
    render(
      <Combobox options={marked} value="b" onValueChange={vi.fn()} id="vendor-field" ariaLabel="Vendor" />,
    );
    expect(trigger().id).toBe('vendor-field');
    expect(trigger().textContent).toContain('Beta');
  });

  it('does not open while the field is disabled', async () => {
    await open(marked, { disabled: true });
    expect(screen.queryAllByRole('option')).toEqual([]);
  });
});
