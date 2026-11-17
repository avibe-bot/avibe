/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ToastContext } from '../../context/ToastContext';
import {
  answerVoiceConsent,
  askVoiceConsent,
  beginVoiceConsentRead,
  closeVoiceConsent,
  gateVoicePress,
  knownVoiceConsent,
  noteVoiceStatus,
  voiceConsentAsking,
} from '../../lib/voiceConsent';
import { VoiceConsentHost, VoiceInputSetting } from './VoiceConsent';

// The question voice input asks before speech goes to a third-party AI service
// (ui/design: composer/voice-consent/*): only Allow records a yes, and what was answered is
// what the instance is told.

const api = vi.hoisted(() => ({
  fetch: vi.fn(),
}));
vi.mock('../../lib/apiFetch', () => ({ apiFetch: api.fetch }));
vi.mock('../../lib/avibeFetch', () => ({ primeCloudToken: vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const showToast = vi.fn();
const mount = () => render(
  <ToastContext.Provider value={{ showToast }}>
    <VoiceConsentHost />
  </ToastContext.Provider>,
);

const saved = (choice: string) => api.fetch.mock.calls.filter(([path, init]) => (
  path === '/api/asr/consent' && init?.method === 'PUT' && JSON.parse(init.body as string).choice === choice
));

beforeEach(() => {
  api.fetch.mockReset().mockImplementation(async (path: string) => ({
    ok: true,
    json: async () => (path === '/api/asr/status' ? { available: true, consent: null } : { ok: true }),
  }));
  showToast.mockReset();
  noteVoiceStatus({ available: true, consent: null });
  closeVoiceConsent();
});

afterEach(cleanup);

describe('voice consent', () => {
  it('a press before any answer asks and does not go on', () => {
    expect(gateVoicePress()).toBe('ask');
    expect(voiceConsentAsking()).toBe(true);
  });

  it('reads a status without the field as an instance that asks nothing', () => {
    noteVoiceStatus({ available: true });
    expect(gateVoicePress()).toBe('granted');
    expect(voiceConsentAsking()).toBe(false);
  });

  it('Allow saves a yes and lets the next press record', async () => {
    mount();
    act(() => askVoiceConsent());
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'voice.consent.allow' }));
    });
    expect(saved('granted')).toHaveLength(1);
    expect(knownVoiceConsent()).toBe('granted');
    expect(voiceConsentAsking()).toBe(false);
    expect(gateVoicePress()).toBe('granted');
  });

  it("Don't allow saves a no; closing saves nothing and asks again next time", async () => {
    mount();
    act(() => askVoiceConsent());
    fireEvent.click(screen.getByRole('button', { name: 'voice.dismiss' }));
    expect(voiceConsentAsking()).toBe(false);
    expect(api.fetch.mock.calls.some(([path]) => path === '/api/asr/consent')).toBe(false);
    expect(gateVoicePress()).toBe('ask');

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'voice.consent.deny' }));
    });
    expect(saved('declined')).toHaveLength(1);
    expect(voiceConsentAsking()).toBe(false);
    // A no is recoverable: the next press records nothing and asks again.
    expect(gateVoicePress()).toBe('declined');
    expect(voiceConsentAsking()).toBe(true);
  });

  it('Settings › General turns voice off at once, and turning it on asks', async () => {
    api.fetch.mockImplementation(async (path: string) => ({
      ok: true,
      json: async () => (path === '/api/asr/status' ? { available: true, consent: 'granted' } : { ok: true }),
    }));
    noteVoiceStatus({ available: true, consent: 'granted' });
    render(
      <ToastContext.Provider value={{ showToast }}>
        <VoiceInputSetting />
      </ToastContext.Provider>,
    );
    const toggle = await screen.findByRole('switch', { name: 'voice.consent.settingTitle' });
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    await act(async () => { fireEvent.click(toggle); });
    expect(saved('declined')).toHaveLength(1);
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(voiceConsentAsking()).toBe(false);
    fireEvent.click(toggle);
    expect(voiceConsentAsking()).toBe(true);
    expect(saved('granted')).toHaveLength(0);
  });

  it('keeps the question up and says so when the answer could not be saved', async () => {
    api.fetch.mockImplementation(async (path: string) => ({ ok: path !== '/api/asr/consent', json: async () => ({}) }));
    mount();
    act(() => askVoiceConsent());
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'voice.consent.allow' }));
    });
    expect(showToast).toHaveBeenCalledWith('voice.consent.saveFailed', 'error');
    expect(voiceConsentAsking()).toBe(true);
    expect(gateVoicePress()).toBe('ask');
  });

  it('a read that started before a saved answer cannot bring the old answer back', async () => {
    noteVoiceStatus({ available: true, consent: 'granted' });
    const slowRead = beginVoiceConsentRead();
    expect(await answerVoiceConsent('declined')).toBe(true);
    noteVoiceStatus({ available: true, consent: 'granted' }, slowRead);
    expect(knownVoiceConsent()).toBe('declined');
    // Nor can an older read overtake a newer one.
    const older = beginVoiceConsentRead();
    const newer = beginVoiceConsentRead();
    noteVoiceStatus({ available: true, consent: 'declined' }, newer);
    noteVoiceStatus({ available: true, consent: 'granted' }, older);
    expect(knownVoiceConsent()).toBe('declined');
  });

  it('shows the small print, with where speech goes, only on Learn more', () => {
    mount();
    act(() => askVoiceConsent());
    expect(screen.queryByText('voice.consent.details', { exact: false })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'voice.consent.learnMore' }));
    expect(screen.getByText('voice.consent.details', { exact: false })).toBeTruthy();
  });
});
