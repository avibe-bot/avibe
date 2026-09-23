/* @vitest-environment jsdom */

// Inspecting a screenshot BEFORE it is sent (#2100). The composer's chips are
// the only place a staged image exists, so the thumbnail is the trigger — and
// what it opens is the staged set alone: the transcript's gallery sits in the
// same provider and must never page into a pre-send preview.

import { createInstance } from 'i18next';
import { createRef, type ReactElement } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
import { ImageViewerProvider } from '../ui/image-viewer';

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

// An image already in the transcript. The chat page feeds these to the provider,
// so every assertion about "only the staged ones" has something to exclude.
const TRANSCRIPT = ['/api/media/transcript_1'];

const previewLabel = (name: string) => en.chat.compose.previewAttachment.replace('{{name}}', name);
const mediaUrl = (name: string) => `/api/media/${name}`;
const imageFile = (name: string) => new File(['png-bytes'], name, { type: 'image/png' });

const dialogImage = () => screen.getByRole('dialog').querySelector('img')?.getAttribute('src');

const onSend = vi.fn();
const ref = createRef<ComposerHandle>();

const providers = (ui: ReactElement) => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <ImageViewerProvider images={TRANSCRIPT}>{ui}</ImageViewerProvider>
    </ToastProvider>
  </I18nextProvider>
);

const renderComposer = () => render(providers(
  <Composer ref={ref} sessionId="ses-1" onSend={onSend} />,
));

// Stage files through the same entry point the picker, paste and drop all use,
// and wait for the uploads the test's mock resolves.
const stage = async (...names: string[]) => {
  act(() => ref.current?.addFiles(names.map((name) => (
    name.endsWith('.png') ? imageFile(name) : new File(['text'], name, { type: 'text/plain' })
  ))));
  await waitFor(() => expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(names.length));
};

// Uploads run concurrently, so a test that reasons about the whole staged set
// waits for every image to reach the state that puts it in that set.
const stageImages = async (...names: string[]) => {
  await stage(...names);
  for (const name of names.filter((n) => n.endsWith('.png'))) {
    await screen.findByLabelText(previewLabel(name));
  }
};

beforeEach(() => {
  onSend.mockReset();
  uploadWorkbenchAttachment.mockReset();
  uploadWorkbenchAttachment.mockImplementation(async (_sid: string, file: File) => ({
    token: `tok-${file.name}`,
    name: file.name,
    mime: file.type,
    size: file.size,
    kind: file.type.startsWith('image/') ? 'image' : 'file',
    url: mediaUrl(file.name),
  }));
  // The lightbox's zoom stage observes its own box; jsdom reports no layout.
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Composer attachment preview', () => {
  it('opens an uploaded screenshot at full size and closes again, leaving the chip staged', async () => {
    renderComposer();
    await stage('shot-a.png');

    fireEvent.click(await screen.findByLabelText(previewLabel('shot-a.png')));
    expect(dialogImage()).toBe(mediaUrl('shot-a.png'));
    // The viewer's own affordances, inherited rather than re-implemented here.
    expect(screen.getByLabelText(en.chat.viewer.zoomIn)).toBeTruthy();
    expect(screen.getByLabelText(en.chat.media.download)).toBeTruthy();

    fireEvent.click(screen.getByLabelText(en.chat.viewer.close));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('shot-a.png')).toBeTruthy();
    expect(screen.getByLabelText(previewLabel('shot-a.png'))).toBeTruthy();
    expect(uploadWorkbenchAttachment).toHaveBeenCalledTimes(1);
  });

  it('pages through the staged images in attachment order and never the transcript', async () => {
    renderComposer();
    await stageImages('shot-a.png', 'notes.txt', 'shot-b.png');

    fireEvent.click(screen.getByLabelText(previewLabel('shot-a.png')));
    // Two staged images, not three attachments and not the transcript's image.
    expect(screen.getByText('1 / 2')).toBeTruthy();

    fireEvent.click(screen.getByLabelText(en.chat.viewer.next));
    expect(dialogImage()).toBe(mediaUrl('shot-b.png'));

    // Wrapping stays inside the staged set rather than falling into the gallery.
    fireEvent.click(screen.getByLabelText(en.chat.viewer.next));
    expect(dialogImage()).toBe(mediaUrl('shot-a.png'));
    expect(screen.getByRole('dialog').innerHTML).not.toContain(TRANSCRIPT[0]);
  });

  it('opens the image the thumbnail belongs to, whichever chip is clicked', async () => {
    renderComposer();
    await stageImages('shot-a.png', 'shot-b.png');

    fireEvent.click(screen.getByLabelText(previewLabel('shot-b.png')));

    expect(dialogImage()).toBe(mediaUrl('shot-b.png'));
    expect(screen.getByText('2 / 2')).toBeTruthy();
  });

  it('offers no preview while an upload is in flight', async () => {
    uploadWorkbenchAttachment.mockImplementation(() => new Promise(() => {}));
    renderComposer();
    await stage('pending.png');

    expect(screen.getByText('pending.png')).toBeTruthy();
    expect(screen.queryByLabelText(previewLabel('pending.png'))).toBeNull();
    expect(screen.getByLabelText(en.chat.compose.removeAttachment)).toBeTruthy();
  });

  it('offers no preview for a failed upload and keeps retry and remove', async () => {
    uploadWorkbenchAttachment.mockRejectedValue(new TypeError('Failed to fetch'));
    renderComposer();
    await stage('broken.png');

    await screen.findByLabelText(en.chat.compose.retryAttachment);
    expect(screen.queryByLabelText(previewLabel('broken.png'))).toBeNull();
    expect(screen.getByLabelText(en.chat.compose.removeAttachment)).toBeTruthy();
  });

  it('leaves a non-image attachment with no preview action', async () => {
    renderComposer();
    await stage('notes.txt');

    await waitFor(() => expect(screen.getByText('notes.txt')).toBeTruthy());
    expect(screen.queryByLabelText(previewLabel('notes.txt'))).toBeNull();
    expect(screen.getByLabelText(en.chat.compose.removeAttachment)).toBeTruthy();
  });

  it('opens and closes from the keyboard without sending or losing the draft', async () => {
    const user = userEvent.setup();
    renderComposer();
    await stage('shot-a.png');

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textbox, { target: { value: 'ship this crop' } });

    const trigger = await screen.findByLabelText(previewLabel('shot-a.png'));
    (trigger as HTMLButtonElement).focus();
    await user.keyboard('{Enter}');
    expect(dialogImage()).toBe(mediaUrl('shot-a.png'));

    // Escape belongs to the open viewer alone — it must not reach the composer.
    fireEvent.keyDown(window, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(textbox.value).toBe('ship this crop');
    expect(screen.getByText('shot-a.png')).toBeTruthy();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('sends exactly the attachments it staged after a preview', async () => {
    renderComposer();
    await stageImages('shot-a.png', 'shot-b.png');

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'look' } });
    fireEvent.click(screen.getByLabelText(previewLabel('shot-a.png')));
    fireEvent.click(screen.getByLabelText(en.chat.viewer.next));
    fireEvent.click(screen.getByLabelText(en.chat.viewer.close));
    fireEvent.click(screen.getByLabelText(en.chat.compose.send));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const [text, attachments] = onSend.mock.calls[0] as [string, ComposerAttachment[]];
    expect(text).toBe('look');
    expect(attachments.map((a) => a.url)).toEqual([mediaUrl('shot-a.png'), mediaUrl('shot-b.png')]);
    expect(attachments.map((a) => a.token)).toEqual(['tok-shot-a.png', 'tok-shot-b.png']);
    expect(attachments.every((a) => a.status === 'ready')).toBe(true);
  });
});
