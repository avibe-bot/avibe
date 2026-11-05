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
