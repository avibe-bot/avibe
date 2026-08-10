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

import { Composer, type ComposerHandle } from './Composer';

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

afterEach(cleanup);
beforeEach(() => uploadWorkbenchAttachment.mockReset());

describe('Composer attachment retry', () => {
  it('disables a failed upload retry when the composer becomes read-only', async () => {
    uploadWorkbenchAttachment
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue({
        token: 'media-token',
        name: 'note.txt',
        mime: 'text/plain',
        size: 5,
        kind: 'file',
        url: '/api/media/media-token',
      });
    const ref = createRef<ComposerHandle>();
    const composer = (disabled: boolean) => providers(
      <Composer
        ref={ref}
        sessionId="ses-1"
        disabled={disabled}
        onSend={() => undefined}
      />,
    );
    const view = render(composer(false));

    act(() => ref.current?.addFiles([new File(['hello'], 'note.txt', { type: 'text/plain' })]));
    const retry = await screen.findByLabelText(en.chat.compose.retryAttachment);
    expect((retry as HTMLButtonElement).disabled).toBe(false);
    expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(1);

    view.rerender(composer(true));
    await waitFor(() => expect((screen.getByLabelText(
      en.chat.compose.retryAttachment,
    ) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByLabelText(en.chat.compose.retryAttachment));
    expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(1);
  });
});
