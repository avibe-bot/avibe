/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { createRef, type RefObject } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
});
const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { Composer, type ComposerHandle } from './Composer';
import { DeferredMentionEditor } from './deferredMentionEditor';

// Ask Vibey (desktop shell) appends each share on its own line: typed text is
// kept, nothing is sent, and the change is saved as an ordinary draft edit.
// ui/design: composer/ask-vibey/appended-zh

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

type View = { initialDraft: string | null; mentions: boolean; handle?: RefObject<ComposerHandle | null> };

const composer = ({ initialDraft, mentions, handle }: View, onDraftChange: (text: string) => void) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer
        ref={handle}
        sessionId="ses_a"
        onSend={() => undefined}
        initialDraft={initialDraft}
        onDraftChange={onDraftChange}
        onSearchAgents={mentions ? async () => [] : undefined}
        onSearchSessions={mentions ? async () => [] : undefined}
      />
    </ToastProvider>
  </I18nextProvider>
);

const textOf = (box: HTMLElement) => (box instanceof HTMLTextAreaElement ? box.value : box.textContent ?? '');


beforeAll(async () => {
  await DeferredMentionEditor.preload();
});

beforeEach(() => {
  apiFetch.mockReset().mockResolvedValue({
    ok: true,
    json: async () => ({ available: false, max_file_bytes: null }),
  });
});

afterEach(() => cleanup());

describe.each([
  { path: 'mention editor', mentions: true },
  { path: 'textarea', mentions: false },
])('Composer appendLine ($path)', ({ mentions }) => {
  it('appends on a new line after typed text and never replaces it', async () => {
    const onDraftChange = vi.fn();
    const handle = createRef<ComposerHandle>();
    const onSend = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <Composer
            ref={handle}
            sessionId="ses_main"
            onSend={onSend}
            initialDraft="Is this clause too strict?"
            onDraftChange={onDraftChange}
            onSearchAgents={mentions ? async () => [] : undefined}
            onSearchSessions={mentions ? async () => [] : undefined}
          />
        </ToastProvider>
      </I18nextProvider>,
    );
    const box = await screen.findByRole('textbox');
    await waitFor(() => expect(textOf(box)).toBe('Is this clause too strict?'));
    let accepted = false;
    act(() => { accepted = handle.current!.appendLine('"The Supplier shall deliver a report."'); });
    expect(accepted).toBe(true);
    act(() => { handle.current!.appendLine('https://example.com/terms'); });
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(
      'Is this clause too strict?\n"The Supplier shall deliver a report."\nhttps://example.com/terms',
    ));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('starts the draft without a leading line when the composer is empty', async () => {
    const onDraftChange = vi.fn();
    const handle = createRef<ComposerHandle>();
    render(composer({ initialDraft: null, mentions, handle }, onDraftChange));
    await screen.findByRole('textbox');
    act(() => { handle.current!.appendLine('「引用」'); });
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith('「引用」'));
  });

  it('stages files only when the composer can take them, and says so', async () => {
    const handle = createRef<ComposerHandle>();
    const view = (disabled: boolean) => (
      <I18nextProvider i18n={i18n}>
        <ToastProvider>
          <Composer
            ref={handle}
            sessionId="ses_main"
            onSend={() => undefined}
            disabled={disabled}
            onSearchAgents={mentions ? async () => [] : undefined}
            onSearchSessions={mentions ? async () => [] : undefined}
          />
        </ToastProvider>
      </I18nextProvider>
    );
    const rendered = render(view(true));
    await screen.findByRole('textbox');
    const file = new File(['synthetic'], 'contract.pdf', { type: 'application/pdf' });
    expect(handle.current!.stageFiles([file])).toBe(false);
    rendered.rerender(view(false));
    let accepted = false;
    act(() => { accepted = handle.current!.stageFiles([file]); });
    expect(accepted).toBe(true);
  });
});
