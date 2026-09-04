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

/** Named by its label AND by whatever it currently holds, in that order. */
const trigger = () => screen.getByRole('combobox', { name: /^Vendor(\s|$)/ });

/** A labelled field, wired the way a call site has to wire one: a visible
 *  `<label for>` for the pointer, and the same text handed to the primitive
 *  because that association does not reach a button. */
const labelled = (options: ComboboxOption[], props: Record<string, unknown> = {}) => (
  <>
    <label htmlFor="vendor-field">Vendor</label>
    <Combobox
      options={options}
      value="a"
      onValueChange={vi.fn()}
      id="vendor-field"
      ariaLabel="Vendor"
      {...props}
    />
  </>
);

/** The open panel is modal, so it hides the rest of the document from the
 *  accessibility tree — including the trigger. It is taken by hand first, which
 *  is also the only way to compare the field before and after a choice. */
const open = async (options: ComboboxOption[], props: Record<string, unknown> = {}) => {
  const user = userEvent.setup();
  render(labelled(options, props));
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

  it('names the trigger with the label it cannot be reached by, and with its value', async () => {
    // A `<label for>` does not name a button, so the name is written here — and
    // written as label PLUS selection, because a name is a replacement, not an
    // addition: the label on its own would announce the field while withholding
    // the vendor it is holding, which is the half a screen reader most needs.
    render(labelled(marked, { value: 'b' }));

    const field = screen.getByRole('combobox', { name: 'Vendor Beta' });
    expect(field.id).toBe('vendor-field');
  });

  it('names a field given no label with nothing at all', async () => {
    // Every other call site passes no label, and for those the trigger's own
    // contents are already the whole name. Composing one anyway would prepend an
    // empty string, or worse, repeat the value.
    render(<Combobox options={marked} value="b" onValueChange={vi.fn()} />);

    expect(screen.getByRole('combobox').getAttribute('aria-label')).toBeNull();
  });

  it('does not open while the field is disabled', async () => {
    await open(marked, { disabled: true });
    expect(screen.queryAllByRole('option')).toEqual([]);
  });
});
