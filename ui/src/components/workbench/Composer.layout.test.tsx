/* @vitest-environment jsdom */

// The chat composer's two-row box: the text takes the whole first row, and the
// action row keeps Attach on the left and the caller's route picker, Voice and
// Send on the right. ChatPage tests mock the Composer, so this is where the
// arrangement itself is pinned.

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});
const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { Composer, type ComposerProps } from './Composer';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const renderComposer = (props: Partial<ComposerProps>) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer sessionId="ses-layout" onSend={vi.fn()} {...props} />
    </ToastProvider>
  </I18nextProvider>,
);

const before = (a: Element, b: Element) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

/** The child of `a` and `b`'s nearest common ancestor that holds `a`. */
const branchOf = (a: Element, b: Element): Element => {
  let branch = a;
  while (branch.parentElement && !branch.parentElement.contains(b)) branch = branch.parentElement;
  return branch;
};

beforeEach(() => {
  apiFetch.mockReset().mockResolvedValue({
    ok: true,
    json: async () => ({ available: true, max_file_bytes: 10_000_000 }),
  });
});

afterEach(cleanup);

describe('Composer stacked layout', () => {
  it('puts the input on its own row above Attach on the left and the route, Voice and Send on the right', async () => {
    renderComposer({ layout: 'stacked', actions: <button type="button">route</button> });

    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    const attach = screen.getByRole('button', { name: en.chat.compose.attach });
    const route = screen.getByRole('button', { name: 'route' });
    const send = screen.getByRole('button', { name: en.chat.compose.send });

    // Row one is the input alone; every control sits in the row after it.
    const inputRow = branchOf(textbox, send);
    for (const control of [attach, route, mic, send]) {
      expect(inputRow.contains(control)).toBe(false);
      expect(before(textbox, control)).toBe(true);
    }
    // Attach holds the left edge alone; the route picker, Voice and Send share the right.
    const right = branchOf(send, attach);
    expect(right.contains(route)).toBe(true);
    expect(right.contains(mic)).toBe(true);
    expect(branchOf(attach, send).contains(route)).toBe(false);
    expect(before(attach, route) && before(route, mic) && before(mic, send)).toBe(true);
  });

  it('keeps the inline box for callers that ask for nothing else', async () => {
    renderComposer({ actions: <button type="button">route</button> });

    const mic = await screen.findByRole('button', { name: en.chat.compose.voice });
    const textbox = screen.getByRole('textbox');
    // Attach and Voice lead the single row; the stacked-only actions are not drawn.
    expect(before(mic, textbox)).toBe(true);
    expect(screen.queryByRole('button', { name: 'route' })).toBeNull();
  });
});
