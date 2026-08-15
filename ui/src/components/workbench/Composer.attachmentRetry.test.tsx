/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { createRef, useState, type ReactElement } from 'react';
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
import { WorkbenchUploadError } from '../../lib/workbenchUpload';

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

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
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

  it('withholds retry when the same file cannot succeed', async () => {
    uploadWorkbenchAttachment
      .mockRejectedValueOnce(new WorkbenchUploadError('too_large', 'File is too large', 413))
      .mockResolvedValue({
        token: 'unexpected-retry',
        name: 'large.bin',
        mime: 'application/octet-stream',
        size: 5,
        kind: 'file',
        url: '/api/media/unexpected-retry',
      });
    const ref = createRef<ComposerHandle>();
    render(providers(
      <Composer
        ref={ref}
        sessionId="ses-1"
        onSend={() => undefined}
      />,
    ));

    act(() => ref.current?.addFiles([new File(['hello'], 'large.bin')]));
    await screen.findByText(en.chat.compose.attachmentTooLarge);

    expect(screen.getByText('large.bin')).toBeTruthy();
    expect(screen.queryByLabelText(en.chat.compose.retryAttachment)).toBeNull();
    expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(1);
  });
});

describe('Composer draft retry', () => {
  it('re-caches optimistically cleared text when a send cannot start', async () => {
    const onDraftChange = vi.fn();
    render(providers(
      <Composer
        onSend={async () => false}
        onDraftChange={onDraftChange}
      />,
    ));

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textbox, { target: { value: 'keep this' } });
    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await waitFor(() => expect(textbox.value).toBe('keep this'));
    expect(onDraftChange.mock.calls.map(([text]) => text)).toEqual([
      'keep this',
      '',
      'keep this',
    ]);
  });

  it('persists rejected text after navigation unmounts the old composer', async () => {
    let rejectSend!: () => void;
    const onDraftChange = vi.fn();
    const view = render(providers(
      <Composer
        onSend={() => new Promise<boolean>((resolve) => {
          rejectSend = () => resolve(false);
        })}
        onDraftChange={onDraftChange}
      />,
    ));

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'survive navigation' } });
    fireEvent.click(screen.getByLabelText(en.chat.compose.send));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(''));

    view.unmount();
    await act(async () => rejectSend());

    expect(onDraftChange.mock.calls.map(([text]) => text)).toEqual([
      'survive navigation',
      '',
      'survive navigation',
    ]);
  });
});

describe('Composer Stop activation', () => {
  it('MESSAGE-DELIVERY-025 blocks a send click burst from the new Stop control', async () => {
    vi.useFakeTimers();
    const onStop = vi.fn();

    const Harness = () => {
      const [busy, setBusy] = useState(false);
      return (
        <Composer
          busy={busy}
          onSend={() => {
            setBusy(true);
          }}
          onStop={onStop}
        />
      );
    };

    render(providers(<Harness />));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'start' } });
    fireEvent.click(screen.getByLabelText(en.chat.compose.send));
    const stop = screen.getByLabelText(en.chat.compose.stop) as HTMLButtonElement;

    expect(stop.disabled).toBe(true);
    fireEvent.click(stop);
    expect(onStop).not.toHaveBeenCalled();

    await act(async () => vi.advanceTimersByTime(400));
    expect(stop.disabled).toBe(false);
    fireEvent.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});
