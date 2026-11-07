/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

// The mention editor as a browser runs it: it shows the saved draft it was
// seeded with on mount, and the change event for that seed never comes, because
// Lexical's OnChangePlugin drops the editor's first committed update, which the
// browser commits together with the editor's initial state. (jsdom happens to
// commit them apart, so the real editor does report the seed there.)
vi.mock('./MentionEditor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./MentionEditor')>();
  const { forwardRef, useImperativeHandle } = await import('react');
  const noop = () => undefined;
  return {
    ...actual,
    MentionEditor: forwardRef<unknown, { initialText?: string | null }>(function MentionEditor({ initialText }, ref) {
      useImperativeHandle(ref, () => new Proxy({}, { get: () => noop }));
      return <div contentEditable suppressContentEditableWarning>{initialText}</div>;
    }),
  };
});

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { Composer } from './Composer';
import { DeferredMentionEditor } from './deferredMentionEditor';

// A saved draft restored into the chat composer is a message the reader can
// send as it is, while the agent is idle or into the queue while it is busy,
// without having to edit it first.

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const providers = (ui: ReactElement) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>{ui}</ToastProvider>
  </I18nextProvider>
);

const mentionSearch = {
  onSearchAgents: async () => [],
  onSearchSessions: async () => [],
};

// The chat page loads its composer's editor before it mounts (routePages.ts).
beforeAll(async () => {
  await DeferredMentionEditor.preload();
});

afterEach(() => cleanup());

describe('Composer restored draft', () => {
  it('sends a restored draft as it is', async () => {
    const onSend = vi.fn(async () => undefined);
    render(providers(<Composer sessionId="ses-1" initialDraft="restored draft" onSend={onSend} {...mentionSearch} />));

    const send = screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend.mock.calls[0]).toEqual(['restored draft', [], []]);
  });

  it('queues a restored draft as it is while the agent is busy', async () => {
    const onSend = vi.fn(async () => undefined);
    render(providers(<Composer sessionId="ses-1" busy initialDraft="restored draft" onSend={onSend} {...mentionSearch} />));

    fireEvent.click(screen.getByLabelText(en.chat.compose.queueSend));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend.mock.calls[0][0]).toBe('restored draft');
  });

  it('offers nothing to send for a blank draft', () => {
    render(providers(<Composer sessionId="ses-1" initialDraft={'  \n'} onSend={vi.fn()} {...mentionSearch} />));
    expect((screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement).disabled).toBe(true);
  });
});
