/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});
const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/avibeFetch')>(),
  primeCloudToken: vi.fn(),
}));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import {
  deleteVoiceTake,
  readVoiceTakes,
  saveVoiceTake,
  saveVoiceTakeAudio,
  type VoiceTake,
} from '../../lib/voiceTakeStore';
import { Composer } from './Composer';

// The takes kept on the device for a conversation, near its composer (ui/design:
// composer/pending-voice/*): after a reload they are still there, and their words land in the
// draft or as the spoken message they were meant to be.

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const kept = (patch: Partial<VoiceTake>): VoiceTake => ({
  id: 'kept-take',
  target: 'pending-session',
  intent: 'send',
  label: 'To Vibey',
  createdAt: Date.now(),
  updatedAt: Date.now(),
  status: 'transcribed',
  text: 'Ship it on Thursday',
  segments: [{ sequence: 0, final: true, durationMs: 83_000 }],
  ...patch,
});

const renderComposer = (onSend = vi.fn(() => true), sendDisabled = false) => render(
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer sessionId="pending-session" onSend={onSend} sendDisabled={sendDisabled} />
    </ToastProvider>
  </I18nextProvider>,
);

beforeEach(() => {
  setLocalSnapshotPrincipal('local');
  apiFetch.mockReset().mockResolvedValue({
    ok: true,
    json: async () => ({ available: true, max_file_bytes: 10_000_000 }),
  });
});

afterEach(async () => {
  cleanup();
  for (const take of await readVoiceTakes()) await deleteVoiceTake(take.id);
  setLocalSnapshotPrincipal(null);
});

describe('Composer pending voice items', () => {
  it('sends a kept take meant to be sent, as a spoken message, and then forgets it', async () => {
    await saveVoiceTake(kept({}));
    const onSend = vi.fn(() => true);
    renderComposer(onSend);

    const item = (await screen.findByText('Ship it on Thursday')).closest<HTMLElement>('[role="status"]')!;
    expect(within(item).getByText(en.voice.pending.ready)).toBeTruthy();
    expect(within(item).getByText('1:23')).toBeTruthy();
    fireEvent.click(within(item).getByRole('button', { name: en.voice.send }));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith('Ship it on Thursday', [], undefined, { input: 'voice' }));
    await waitFor(() => expect(item.isConnected).toBe(false));
    expect(await readVoiceTakes()).toEqual([]);
  });

  it('holds a kept take back while an earlier send is still unconfirmed', async () => {
    await saveVoiceTake(kept({}));
    const onSend = vi.fn(() => true);
    renderComposer(onSend, true);

    const item = (await screen.findByText('Ship it on Thursday')).closest<HTMLElement>('[role="status"]')!;
    fireEvent.click(within(item).getByRole('button', { name: en.voice.send }));
    await act(async () => undefined);

    expect(onSend).not.toHaveBeenCalled();
    expect(await readVoiceTakes()).toHaveLength(1);
  });

  it('puts a kept dictation in the draft, and then forgets it', async () => {
    await saveVoiceTake(kept({ intent: 'insert', text: 'Run make release' }));
    renderComposer();

    const item = (await screen.findByText('Run make release')).closest<HTMLElement>('[role="status"]')!;
    expect(within(item).queryByRole('button', { name: en.voice.send })).toBeNull();
    fireEvent.click(within(item).getByRole('button', { name: en.voice.pending.insert }));

    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Run make release'));
    await waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
  });

  it('shows a take with no text yet as kept, and discards it only on request', async () => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
    try {
      await saveVoiceTake(kept({ status: 'pending', text: undefined }));
      await saveVoiceTakeAudio('kept-take', 0, new Blob(['audio'], { type: 'audio/wav' }));
      renderComposer();

      await screen.findByText(en.voice.pending.title);
      expect(screen.getByText(en.voice.pending.waiting)).toBeTruthy();
      expect(screen.getByRole('button', { name: en.voice.retry })).toBeTruthy();
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: en.voice.discard }));
      });
      await waitFor(async () => expect(await readVoiceTakes()).toEqual([]));
      await waitFor(() => expect(screen.queryByText(en.voice.pending.title)).toBeNull());
    } finally {
      Reflect.deleteProperty(navigator, 'onLine');
    }
  });
});
