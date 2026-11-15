/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { createRef, type ReactElement } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
import {
  sessionDraftAttachmentsCache,
  type SessionDraftAttachment,
} from '../../lib/sessionDraftAttachmentsCache';

const uploadWorkbenchAttachment = vi.hoisted(() => vi.fn());

vi.mock('../../lib/workbenchUpload', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/workbenchUpload')>(),
  uploadWorkbenchAttachment,
}));

import { Composer, type ComposerAttachment, type ComposerHandle } from './Composer';

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

const readyAttachment: SessionDraftAttachment = {
  token: 'media-token',
  name: 'image.png',
  mime: 'image/png',
  size: 42,
  kind: 'image',
  url: '/api/media/media-token',
};

beforeEach(() => {
  uploadWorkbenchAttachment.mockReset();
  sessionDraftAttachmentsCache.clear('session-a');
  sessionDraftAttachmentsCache.clear('session-b');
});

afterEach(() => cleanup());

describe('Composer draft attachments', () => {
  it('restores ready chips with the text and sends the durable token without re-uploading', async () => {
    sessionDraftAttachmentsCache.write('session-a', [readyAttachment]);
    const onSend = vi.fn(async () => undefined);
    const view = render(providers(
      <Composer
        sessionId="session-a"
        initialDraft="keep this text"
        onSend={onSend}
      />,
    ));

    await waitFor(() => {
      expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('keep this text');
      expect(screen.getByText('image.png')).toBeTruthy();
    });
    expect((screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByLabelText(en.chat.compose.send));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const [, sent] = onSend.mock.calls[0] as [string, ComposerAttachment[]];
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ token: 'media-token', status: 'ready' });
    expect(sent[0].file).toBeUndefined();
    expect(uploadWorkbenchAttachment).not.toHaveBeenCalled();

    view.unmount();
    render(providers(
      <Composer sessionId="session-a" onSend={vi.fn()} />,
    ));
    await waitFor(() => expect(screen.queryByText('image.png')).toBeNull());
  });

  it('keeps attachments scoped to the session and supports attachment-only drafts', async () => {
    sessionDraftAttachmentsCache.write('session-a', [readyAttachment]);
    const onSend = vi.fn(async () => undefined);
    const view = render(providers(
      <Composer sessionId="session-a" onSend={onSend} />,
    ));

    await waitFor(() => expect(screen.getByText('image.png')).toBeTruthy());
    expect((screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement).disabled).toBe(false);

    view.rerender(providers(
      <Composer sessionId="session-b" onSend={onSend} />,
    ));
    await waitFor(() => expect(screen.queryByText('image.png')).toBeNull());
    expect((screen.getByLabelText(en.chat.compose.send) as HTMLButtonElement).disabled).toBe(true);
  });

  it('persists removing a ready chip before the composer unmounts', async () => {
    sessionDraftAttachmentsCache.write('session-a', [readyAttachment]);
    const view = render(providers(
      <Composer sessionId="session-a" onSend={vi.fn()} />,
    ));

    await waitFor(() => expect(screen.getByText('image.png')).toBeTruthy());
    fireEvent.click(screen.getByLabelText(en.chat.compose.removeAttachment));
    await waitFor(() => expect(screen.queryByText('image.png')).toBeNull());

    view.unmount();
    render(providers(
      <Composer sessionId="session-a" onSend={vi.fn()} />,
    ));
    await waitFor(() => expect(screen.queryByText('image.png')).toBeNull());
  });

  it('does not restore an upload that was still in flight when the composer unmounted', async () => {
    uploadWorkbenchAttachment.mockImplementation(() => new Promise(() => {}));
    const ref = createRef<ComposerHandle>();
    const view = render(providers(
      <Composer ref={ref} sessionId="session-a" onSend={vi.fn()} />,
    ));

    const file = new File(['image'], 'pending.png', { type: 'image/png' });
    ref.current?.addFiles([file]);
    await waitFor(() => expect(screen.getByText('pending.png')).toBeTruthy());

    view.unmount();
    render(providers(
      <Composer sessionId="session-a" onSend={vi.fn()} />,
    ));
    await waitFor(() => expect(screen.queryByText('pending.png')).toBeNull());
  });
});
