// A message spoken through hold-to-talk says so under its bubble (ui/design:
// mobile/main-bot-voice/i-conversation); a typed one, and the agent's words, do not.
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import { VOICE_MESSAGE_METADATA } from '../../lib/voiceMessage';
import { MessageRow } from './ChatPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const session = { id: 'ses_main', scope_id: null, status: 'active', metadata: {} } as unknown as WorkbenchSession;

const message = (author: 'user' | 'agent', metadata: Record<string, unknown>): WorkbenchMessage =>
  ({
    id: `msg_${author}`,
    type: author === 'user' ? 'user' : 'result',
    author,
    source: author,
    author_id: null,
    author_name: null,
    text: 'Check what the Monday release still needs.',
    content: {},
    metadata,
    created_at: '2026-11-03T04:04:00Z',
  }) as unknown as WorkbenchMessage;

const row = (each: WorkbenchMessage) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <MemoryRouter>
          <MessageRow message={each} session={session} messageFontSize={13} />
        </MemoryRouter>
      </ToastProvider>
    </I18nextProvider>,
  );

const MARK = `>${en.voice.mark}<`;

describe('the voice mark', () => {
  it('marks a spoken message', () => {
    expect(row(message('user', { ...VOICE_MESSAGE_METADATA }))).toContain(MARK);
  });

  it('leaves a typed message and the agent’s reply unmarked', () => {
    expect(row(message('user', {}))).not.toContain(MARK);
    expect(row(message('agent', { ...VOICE_MESSAGE_METADATA }))).not.toContain(MARK);
  });
});
