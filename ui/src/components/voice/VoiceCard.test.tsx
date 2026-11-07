/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import { VoiceCard } from './VoiceCard';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const card = (text: string, reason: 'dropped' | 'failed' | 'system') => render(
  <I18nextProvider i18n={i18n}>
    <VoiceCard to={{ name: 'Vibey' }} phase="interrupted" text={text} seconds={142} reason={reason} />
  </I18nextProvider>,
);

afterEach(cleanup);

describe('VoiceCard', () => {
  it.each(['dropped', 'failed', 'system'] as const)(
    'never points at words above when a %s take has none',
    (reason) => {
      card('', reason);
      expect(screen.getByText(en.voice.noText)).toBeTruthy();
      expect(screen.queryByText(en.pet.voice.listening)).toBeNull();
      expect(screen.queryByText(/above/)).toBeNull();
      expect(screen.getByText(en.voice.interrupted[`${reason}Empty`])).toBeTruthy();
    },
  );

  it('points at the words it shows', () => {
    card('帮我看一下发布', 'dropped');
    expect(screen.getByText('帮我看一下发布')).toBeTruthy();
    expect(screen.getByText(en.voice.interrupted.dropped)).toBeTruthy();
  });
});

describe('VoiceCard recording without Web Audio', () => {
  const recording = (captureState: 'live' | 'recorder' | 'no_sound', liveUnavailable: boolean) => render(
    <I18nextProvider i18n={i18n}>
      <VoiceCard to={{ name: 'Vibey' }} phase="listening" text="" seconds={3} liveUnavailable={liveUnavailable} captureState={captureState} />
    </I18nextProvider>,
  );
  const meterBars = () => Array.from(document.querySelectorAll('[data-voice-meter="live"] span')).map((bar) => (bar as HTMLElement).style.transform);
  /** The live meter animates its bars; a still one does not. */
  const animated = () => document.querySelectorAll('[data-voice-meter="live"] span.will-change-transform').length;

  it('says the take is transcribed on release, and draws no meter it has no level for', () => {
    recording('recorder', true);
    expect(screen.getByRole('status').textContent).toBe(en.voice.liveUnavailable);
    expect(document.querySelector('[data-voice-meter]')).toBeNull();
    // A dot that breathes only where motion is allowed.
    const mark = document.querySelector('[data-voice-recording]');
    expect(mark?.querySelector('.motion-safe\\:animate-pulse')).toBeTruthy();
  });

  it('says when the microphone gives no sound, its meter flat, and the take goes on', () => {
    recording('no_sound', true);
    expect(screen.getByRole('status').textContent).toBe(en.voice.noSoundNow);
    expect(screen.queryByText(en.voice.liveUnavailable)).toBeNull();
    expect(animated()).toBe(0);
    expect(new Set(meterBars()).size).toBe(1);
  });

  it('draws the live level otherwise', () => {
    recording('live', false);
    expect(screen.queryByText(en.voice.liveUnavailable)).toBeNull();
    expect(screen.queryByText(en.voice.noSoundNow)).toBeNull();
    expect(animated()).toBeGreaterThan(0);
  });
});
