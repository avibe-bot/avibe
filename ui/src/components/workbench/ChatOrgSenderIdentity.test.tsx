// An Organization transcript is shared, so a human bubble has to say who wrote
// it. A personal one has a single human and must keep rendering exactly as it
// did. These render the real row rather than the whole page, so the only
// variable between the two expectations is the instance kind.
import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import {
  InstanceAuthorizationContext,
  type InstanceAuthorizationValue,
} from '../../context/InstanceAuthorizationContext';
import { ToastProvider } from '../../context/ToastProvider';
import { InstanceAuthorizationProvider } from '../../context/InstanceAuthorizationProvider';
import {
  DENIED_INSTANCE_CAPABILITIES,
  normalizeSessionInfo,
  type InstanceKind,
  type SessionInfo,
} from '../../lib/sessionInfo';
import { senderInitial, senderTone } from '../../lib/senderIdentity';
import { MessageRow } from './ChatPage';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

// The hover-revealed stamp under a bubble — the row's timestamp before the
// sender head existed. Matched by class so the assertion does not depend on the
// test machine's timezone.
const HOVER_STAMP = 'group-hover/message:opacity-100';
// The fill a resolved sender's avatar paints, checked in the markup so the
// "resolved gets a tone, unresolved does not" contract is visible in the render
// and not only in the helper's return value.
const TONE_FILL = `bg-${senderTone('remote:sub-amy')}`;

const instance = (instanceKind: InstanceKind | null): InstanceAuthorizationValue => ({
  remote: instanceKind !== null,
  instanceKind,
  instanceRole: 'owner',
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

const render = (ui: ReactElement, instanceKind: InstanceKind | null) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <MemoryRouter>
          <InstanceAuthorizationContext.Provider value={instance(instanceKind)}>
            {ui}
          </InstanceAuthorizationContext.Provider>
        </MemoryRouter>
      </ToastProvider>
    </I18nextProvider>,
  );

const session = {
  id: 'ses_01J8XK5M8T',
  scope_id: 'scope-1',
  status: 'active',
  metadata: {},
} as unknown as WorkbenchSession;

const humanMessage = (over: Partial<WorkbenchMessage> = {}): WorkbenchMessage =>
  ({
    id: 'msg_human',
    type: 'user',
    author: 'user',
    source: 'user',
    author_id: 'remote:sub-amy',
    author_name: null,
    sender_label: 'amy.chen',
    text: 'Can someone take the deploy?',
    content: {},
    metadata: {},
    created_at: '2026-07-27T04:04:00Z',
    ...over,
  }) as unknown as WorkbenchMessage;

const row = (message: WorkbenchMessage, instanceKind: InstanceKind | null) =>
  render(<MessageRow message={message} session={session} messageFontSize={13} />, instanceKind);

// The same row as a real reader sees it: the reader's principal comes from the
// session payload through the production provider, not from a hand-built value.
const rowAs = (message: WorkbenchMessage, reader: SessionInfo) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <MemoryRouter>
          <InstanceAuthorizationProvider session={reader}>
            <MessageRow message={message} session={session} messageFontSize={13} />
          </InstanceAuthorizationProvider>
        </MemoryRouter>
      </ToastProvider>
    </I18nextProvider>,
  );

// Readers exactly as ``normalizeSessionInfo`` hands them over from /api/session.
const LOOPBACK_READER = normalizeSessionInfo({
  remote: false,
  instance_kind: 'organization',
  author_id: 'local',
});
// A LAN setup-host browser: not remote, but not the machine owner either.
const LAN_READER = normalizeSessionInfo({
  remote: false,
  instance_kind: 'organization',
  author_id: null,
});
const cloudReader = (sub: string): SessionInfo =>
  normalizeSessionInfo({
    remote: true,
    authenticated: true,
    email: `${sub}@acme.example`,
    sub,
    author_id: `remote:${sub}`,
    instance_kind: 'organization',
    instance_role: 'member',
    capabilities: DENIED_INSTANCE_CAPABILITIES,
    authorization_state: 'current',
  });
const YOU = `>${en.chat.senderYou}<`;
// The default avatar: a person glyph instead of an initial or a "?".
const DEFAULT_AVATAR = 'lucide-user-round';

describe('organization sender identity', () => {
  it('names the sender on an organization instance', () => {
    const markup = row(humanMessage(), 'organization');

    expect(markup).toContain('amy.chen');
    // Initial over the deterministic tone fill, plus the compact clock.
    expect(markup).toContain('>A<');
    expect(markup).toContain(TONE_FILL);
    expect(markup).toMatch(/>\d{2}:\d{2}</);
    // The head carries the time, so the hover stamp underneath is gone rather
    // than repeating it.
    expect(markup).not.toContain(HOVER_STAMP);
    expect(markup).toContain('Can someone take the deploy?');
  });

  it('falls back to a neutral unknown sender when the server resolved none', () => {
    const markup = row(humanMessage({ sender_label: null }), 'organization');

    expect(markup).toContain(en.chat.senderUnknown);
    expect(markup).toContain('>?<');
    // No tone fill: an unresolved row must not look like a confirmed identity.
    expect(markup).not.toContain(TONE_FILL);
  });

  it('calls the reader\'s own row "You" at the loopback and through Cloud', () => {
    const local = humanMessage({ author_id: 'local', sender_label: null });
    const amy = humanMessage({ author_id: 'remote:sub-amy', sender_label: 'amy.chen' });

    for (const [message, reader] of [
      [local, LOOPBACK_READER],
      [amy, cloudReader('sub-amy')],
    ] as const) {
      const markup = rowAs(message, reader);
      expect(markup).toContain(YOU);
      expect(markup).toContain(DEFAULT_AVATAR);
      expect(markup).not.toContain('amy.chen');
      expect(markup).not.toContain(en.chat.senderUnknown);
      expect(markup).not.toContain('>?<');
      expect(markup).not.toContain(TONE_FILL);
    }
  });

  it('never calls someone else\'s row "You"', () => {
    const local = humanMessage({ author_id: 'local', sender_label: null });
    const amy = humanMessage({ author_id: 'remote:sub-amy', sender_label: 'amy.chen' });

    // A Cloud reader looking at the loopback owner's row, and at a colleague's.
    const bo = cloudReader('sub-bo');
    expect(rowAs(local, bo)).not.toContain(YOU);
    expect(rowAs(local, bo)).toContain(en.chat.senderUnknown);
    expect(rowAs(amy, bo)).not.toContain(YOU);
    expect(rowAs(amy, bo)).toContain('amy.chen');
    // A LAN browser is not remote, but the owner's loopback rows are not its own.
    expect(rowAs(local, LAN_READER)).not.toContain(YOU);
    expect(rowAs(local, LAN_READER)).toContain(en.chat.senderUnknown);
    // The loopback reader looking at a Cloud member's row.
    expect(rowAs(amy, LOOPBACK_READER)).toContain('amy.chen');
    expect(rowAs(amy, LOOPBACK_READER)).not.toContain(YOU);
  });

  it('ignores a label the server should not have sent on a personal instance', () => {
    // Same message object in both renders: the ONLY variable is instance kind.
    const personal = row(humanMessage(), 'personal');
    const unknownKind = row(humanMessage(), null);

    expect(personal).toBe(unknownKind);
    expect(personal).not.toContain('amy.chen');
    expect(personal).not.toContain(en.chat.senderUnknown);
    // The hover stamp is still the row's only timestamp, as before.
    expect(personal).toContain(HOVER_STAMP);
    expect(personal).toContain('Can someone take the deploy?');
  });

  it('leaves the agent row alone on an organization instance', () => {
    const agent = {
      id: 'msg_agent',
      type: 'result',
      author: 'agent',
      source: null,
      author_id: null,
      author_name: 'claude',
      text: 'On it.',
      content: {},
      metadata: {},
      created_at: '2026-07-27T04:05:00Z',
    } as unknown as WorkbenchMessage;

    expect(row(agent, 'organization')).toBe(row(agent, 'personal'));
  });
});

describe('sender avatar derivation', () => {
  it('gives one principal the same tone every time', () => {
    expect(senderTone('remote:sub-amy')).toBe(senderTone('remote:sub-amy'));
    expect(['gold', 'violet']).toContain(senderTone('remote:sub-amy'));
    // A missing id still resolves rather than throwing.
    expect(['gold', 'violet']).toContain(senderTone(null));
  });

  it('takes the first whole character of the label', () => {
    expect(senderInitial('amy.chen')).toBe('A');
    expect(senderInitial('  bo')).toBe('B');
    expect(senderInitial('雨辰')).toBe('雨');
    // A surrogate pair stays one character instead of splitting.
    expect(senderInitial('😀ana')).toBe('😀');
    expect(senderInitial('   ')).toBe('?');
  });
});
