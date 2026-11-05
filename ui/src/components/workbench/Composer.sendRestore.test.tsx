/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { createRef, type ReactElement } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';

const uploadWorkbenchAttachment = vi.hoisted(() => vi.fn());

vi.mock('../../lib/workbenchUpload', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/workbenchUpload')>(),
  uploadWorkbenchAttachment,
}));

import { Composer, type ComposerAttachment, type ComposerHandle } from './Composer';

// A send the page withdraws (the server refused it, or the network failed) gives
// the whole submission back: the text, and every attachment chip with the token
// its upload returned. The server keeps uploaded files for the session, so the
// retry sends the same tokens and nothing is uploaded twice.

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

const uploaded = (token: string, name: string) => ({
  token,
  name,
  mime: 'text/plain',
  size: 5,
  kind: 'file' as const,
  url: `/api/media/${token}`,
});

type Sent = { text: string; attachments: ComposerAttachment[] | undefined };

afterEach(() => cleanup());
beforeEach(() => uploadWorkbenchAttachment.mockReset());

describe('Composer withdrawn send', () => {
  it('gives back the attachment chips with their uploaded tokens, and the retry sends the same tokens', async () => {
    uploadWorkbenchAttachment.mockResolvedValue(uploaded('media-1', 'note.txt'));
    const sends: Sent[] = [];
    const ref = createRef<ComposerHandle>();
    render(providers(
      <Composer
        ref={ref}
        sessionId="ses-1"
        onSend={async (text, attachments) => {
          sends.push({ text, attachments });
          return sends.length === 1 ? false : undefined;
        }}
      />,
    ));
    act(() => ref.current?.addFiles([new File(['hello'], 'note.txt', { type: 'text/plain' })]));
    await waitFor(() => expect((screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'see the note' } });

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));
    // Withdrawn: the text and the chip are back, ready to send.
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('see the note'));
    expect(screen.getByText('note.txt')).toBeTruthy();

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));
    await waitFor(() => expect(sends).toHaveLength(2));
    expect(sends.map(({ attachments }) => attachments?.map((attachment) => attachment.token))).toEqual([
      ['media-1'],
      ['media-1'],
    ]);
    expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(1);
  });
});
