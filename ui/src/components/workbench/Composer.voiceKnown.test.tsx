/* @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { createInstance } from 'i18next';
import { act, cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
vi.mock('../../lib/avibeFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/avibeFetch')>(),
  primeCloudToken: vi.fn(),
}));

import { ToastProvider } from '../../context/ToastProvider';
import en from '../../i18n/en.json';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import { beginVoiceStatusRead } from '../../lib/voiceAvailability';
import { Composer } from './Composer';

// The composer's voice control renders from the last known transcription status
// for this reader, so it is in place from the first frame; the live read then
// decides, and once shown the control keeps its place (disabled) instead of
// leaving a gap.

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const composer = () => (
  <I18nextProvider i18n={i18n}>
    <ToastProvider>
      <Composer sessionId="ses_a" onSend={() => undefined} onSearchAgents={async () => []} onSearchSessions={async () => []} />
    </ToastProvider>
  </I18nextProvider>
);

const voiceButton = () => screen.queryByRole('button', { name: en.chat.compose.voice }) as HTMLButtonElement | null;

// Every status read still out (the composer's own, and hold-to-talk's).
type Status = { available: boolean; max_file_bytes: number | null } | null;
let pending: Array<(status: Status) => void> = [];
const answer = (status: Status) => {
  const reads = pending;
  pending = [];
  for (const resolve of reads) resolve(status);
};

beforeEach(() => {
  setLocalSnapshotPrincipal('local');
  pending = [];
  apiFetch.mockReset().mockImplementation((path: string) => {
    if (path !== '/api/asr/status') return new Promise(() => {});
    return new Promise((resolve) => {
      pending.push((status) => resolve(status ? { ok: true, json: async () => status } : { ok: false, json: async () => null }));
    });
  });
});

afterEach(async () => {
  cleanup();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

const know = (available: boolean) => beginVoiceStatusRead().remember({ available, maxFileBytes: null });

describe('Composer voice control from the last known status', () => {
  it('is in place at the first frame when voice was last known available', () => {
    know(true);
    render(composer());
    // No await: this is the first render, before the live status read answers.
    expect(voiceButton()).not.toBeNull();
    expect(voiceButton()!.disabled).toBe(false);
  });

  it('keeps its place, disabled, when the live read says voice is now unavailable', async () => {
    know(true);
    render(composer());
    const before = voiceButton();
    expect(before).not.toBeNull();

    await act(async () => answer({ available: false, max_file_bytes: null }));
    expect(voiceButton()).not.toBeNull();
    expect(voiceButton()!.disabled).toBe(true);
  });

  it('keeps the known state when the live read fails', async () => {
    know(true);
    render(composer());
    await act(async () => answer(null));
    expect(voiceButton()!.disabled).toBe(false);
  });

  it('stays absent when voice was last known unavailable and the live read agrees', async () => {
    know(false);
    render(composer());
    expect(voiceButton()).toBeNull();
    await act(async () => answer({ available: false, max_file_bytes: null }));
    expect(voiceButton()).toBeNull();
  });

  it('remembers the live answer for the next composer', async () => {
    render(composer());
    expect(voiceButton()).toBeNull();
    await act(async () => answer({ available: true, max_file_bytes: 1000 }));
    expect(voiceButton()).not.toBeNull();
    cleanup();

    render(composer());
    expect(voiceButton()).not.toBeNull();
  });
});
