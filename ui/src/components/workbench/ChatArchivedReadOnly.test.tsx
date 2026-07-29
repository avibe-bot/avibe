import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type { WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { ChatHeaderBar, MessageRow } from './ChatPage';
import { QuickReplies } from './QuickReplies';
import {
  isSessionArchivedConflict,
  isSessionReadOnly,
  isShowPageActive,
  markSessionArchived,
  showPageControlActions,
  transcriptSelectionActions,
} from './sessionArchived';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const render = (ui: ReactElement) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{ui}</MemoryRouter>
    </I18nextProvider>,
  );

const session = (over: Partial<WorkbenchSession> = {}): WorkbenchSession =>
  ({
    id: 'ses_01J8XK5M8T',
    scope_id: 'scope-1',
    project_id: 'proj-1',
    title: 'Model Hub',
    agent_id: 'agt-1',
    agent_name: 'claude',
    agent_backend: 'claude_code',
    agent_variant: null,
    model: null,
    reasoning_effort: null,
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    workdir: null,
    native_session_id: 'native-1',
    created_at: '2026-07-27T04:00:00Z',
    updated_at: '2026-07-27T04:00:00Z',
    last_active_at: '2026-07-27T04:00:00Z',
    metadata: {},
    ...over,
  }) as WorkbenchSession;

// An agent reply carrying a quick-reply group, which is the transcript control
// that POSTs a new message when clicked.
const agentWithQuickReplies = (over: Record<string, unknown> = {}): WorkbenchMessage =>
  ({
    id: 'msg_01J8XK5M8T',
    type: 'result',
    author: 'agent',
    source: null,
    author_name: 'claude',
    text: 'Ship it now, or wait for review?',
    content: { quick_replies: ['Ship it', 'Wait'], ...over },
    metadata: {},
    created_at: '2026-07-27T04:04:00Z',
  }) as unknown as WorkbenchMessage;

// Count rendered <button …> elements that carry the disabled attribute. SSR emits
// ``disabled=""``, so a locked group has one per option and a live one has none.
const countButtons = (markup: string) => (markup.match(/<button/g) ?? []).length;
const countDisabledButtons = (markup: string) => (markup.match(/<button[^>]*disabled=""/g) ?? []).length;

// ── Codex review #1 (ChatPage.tsx:1242) ───────────────────────────────────────
// A backgrounded / offline tab can miss the archive SSE for a session that
// already has a native_session_id, and refreshSessionRowUntilNativeBound then
// early-returns on every reconnect/focus — so the archived 409 on the first send
// is the ONLY point at which that tab learns the truth. It has to converge there,
// not just print a message, or the composer stays live and the user retries a
// permanently rejected send forever.
describe('archived 409 convergence', () => {
  it('recognizes the archived conflict and nothing else', () => {
    expect(isSessionArchivedConflict(409, { code: 'session_archived' })).toBe(true);
    // Same status, different reason (e.g. a backend lock) must stay a normal error.
    expect(isSessionArchivedConflict(409, { code: 'session_backend_locked' })).toBe(false);
    expect(isSessionArchivedConflict(409, null)).toBe(false);
    expect(isSessionArchivedConflict(409, undefined)).toBe(false);
    // The code alone is not enough — only a 409 states the row is archived.
    expect(isSessionArchivedConflict(200, { code: 'session_archived' })).toBe(false);
    expect(isSessionArchivedConflict(500, { code: 'session_archived' })).toBe(false);
  });

  it('turns the stale row read-only, which is what disables the composer', () => {
    const stale = session({ status: 'active' });
    expect(isSessionReadOnly(stale)).toBe(false);

    const converged = markSessionArchived(stale, stale.id);

    expect(converged?.status).toBe('archived');
    expect(isSessionReadOnly(converged)).toBe(true);
    // Only the status moves — the rest of the loaded row is left alone (the
    // best-effort authoritative refresh that follows picks up the frozen fields).
    expect({ ...converged, status: 'active' }).toEqual(stale);
  });

  it('never stamps one chat’s archive onto the chat the user moved to', () => {
    const other = session({ id: 'ses_other' });
    expect(markSessionArchived(other, 'ses_01J8XK5M8T')).toBe(other);
    expect(markSessionArchived(null, 'ses_01J8XK5M8T')).toBeNull();
  });

  it('is identity-stable once archived, so it cannot spin a re-render', () => {
    const archived = session({ status: 'archived' });
    expect(markSessionArchived(archived, archived.id)).toBe(archived);
  });
});

// ── Codex review #2 (ChatPage.tsx:1971) ───────────────────────────────────────
// readOnly reached the composer but not the transcript, so an archived chat still
// offered controls that write to it: an old quick reply POSTed a message (409),
// and Quote called the composer's imperative appendText, planting an unsendable
// draft in the disabled editor.
describe('read-only transcript withdraws session writes', () => {
  it('offers neither Quote nor Ask-in-a-new-session on an archived session', () => {
    // Live session with a bound native: both actions available (unchanged).
    expect(transcriptSelectionActions(session(), false)).toEqual({ quote: true, askInNew: true });
    // Pre-existing gate: forking needs a bound native.
    expect(transcriptSelectionActions(session({ native_session_id: null }), false)).toEqual({
      quote: true,
      askInNew: false,
    });
    // Archived: Quote would land in a disabled composer, and the fork endpoint
    // refuses an archived source outright (archive is terminal — no fork out).
    expect(transcriptSelectionActions(session({ status: 'archived' }), true)).toEqual({
      quote: false,
      askInNew: false,
    });
    expect(transcriptSelectionActions(session({ status: 'archived', native_session_id: null }), true)).toEqual({
      quote: false,
      askInNew: false,
    });
  });

  it('locks the quick-reply group instead of leaving it clickable', () => {
    const live = render(<QuickReplies options={['Ship it', 'Wait']} onChoose={() => undefined} />);
    expect(countButtons(live)).toBe(2);
    expect(countDisabledButtons(live)).toBe(0);

    const archived = render(<QuickReplies options={['Ship it', 'Wait']} readOnly onChoose={() => undefined} />);
    // The group is still THERE — which options the agent offered is part of the
    // transcript — but no click can start a send.
    expect(archived).toContain('Ship it');
    expect(archived).toContain('Wait');
    expect(countDisabledButtons(archived)).toBe(2);
  });

  it('keeps the recorded answer visible while read-only', () => {
    const archived = render(
      <QuickReplies options={['Ship it', 'Wait']} chosen="Wait" readOnly onChoose={() => undefined} />,
    );
    expect(countDisabledButtons(archived)).toBe(2);
    // The chosen option keeps its aria-pressed marker (and its ✓).
    expect(archived).toContain('aria-pressed="true"');
  });

  it('threads readOnly from the transcript row into the group', () => {
    const live = render(
      <MessageRow
        message={agentWithQuickReplies()}
        session={session()}
        messageFontSize={13}
        onQuickReply={() => undefined}
      />,
    );
    expect(live).toContain('Ship it');
    expect(countDisabledButtons(live)).toBe(0);

    const archived = render(
      <MessageRow
        message={agentWithQuickReplies()}
        session={session({ status: 'archived' })}
        messageFontSize={13}
        onQuickReply={() => undefined}
        readOnly
      />,
    );
    // Same row, same options, nothing left to click.
    expect(archived).toContain('Ship it');
    expect(archived).toContain('Ship it now, or wait for review?');
    expect(countDisabledButtons(archived)).toBe(2);
  });
});

// ── Codex review #3 (ChatPage.tsx:2540) ───────────────────────────────────────
// The previous round kept the Show Page toggle and the Share control on the
// theory that the store already refuses archived mutations. That is the
// clickable-but-erroring pattern, not a fix: archive forces every existing page
// to visibility="offline" (storage/workbench_sessions_service.py) and
// ensure_active refuses to create a missing one (core/show_pages.py), so
// Visualize either 409s or frames a dead page, and every Share mutation can only
// 409. All of it is withdrawn now.
describe('read-only header withdraws the Show Page controls', () => {
  it('offers no Show Page control at all on an archived session', () => {
    // Live, in chat view: Visualize only (Share + annotate are Show-Page-mode).
    expect(showPageControlActions(false, false)).toEqual({
      visualize: true,
      share: false,
      annotate: false,
    });
    // Live, framing the page: back-to-chat + Share + the annotation control.
    expect(showPageControlActions(false, true)).toEqual({
      visualize: true,
      share: true,
      annotate: true,
    });
    // Archived: nothing, in either mode. Not "disabled" — absent.
    expect(showPageControlActions(true, false)).toEqual({
      visualize: false,
      share: false,
      annotate: false,
    });
    expect(showPageControlActions(true, true)).toEqual({
      visualize: false,
      share: false,
      annotate: false,
    });
  });

  it('falls back out of Show Page mode when the session turns read-only', () => {
    // The stale-tab shape: this tab was already framing the page when the
    // archived 409 converged. Back-to-chat IS the withdrawn Visualize button, so
    // staying framed would strand the reader on an offline page — and leave the
    // chat surface hidden with no iframe, i.e. blank.
    expect(isShowPageActive(false, true)).toBe(true);
    expect(isShowPageActive(true, true)).toBe(false);
    // Unchanged in chat view.
    expect(isShowPageActive(false, false)).toBe(false);
    expect(isShowPageActive(true, false)).toBe(false);
  });

  it('renders an archived header with the title and badge but no Show Page button', () => {
    // Only the read-only header is reachable here: the live one renders
    // AgentRoutePicker, which calls useApi() and throws without an ApiProvider.
    // The pure matrix above covers the live side.
    const markup = render(
      <ChatHeaderBar
        session={session({ status: 'archived' })}
        agents={[]}
        defaultAgentName={null}
        onPatch={async () => undefined}
        onBack={() => undefined}
        working={false}
        showPageMode={false}
        showPageBusy={false}
        onToggleShowPage={() => undefined}
        annotation={{
          state: null,
          iframeRef: { current: null },
          handleIframeLoad: () => undefined,
          enable: () => undefined,
          disable: () => undefined,
          setMode: () => undefined,
        }}
        readOnly
      />,
    );
    // The header did render (so the absences below are not a blank component).
    expect(markup).toContain('Model Hub');
    expect(markup).toContain('Archived');
    expect(markup).toContain('Back');
    // …and offers no Show Page affordance, disabled or otherwise.
    expect(markup).not.toContain('Visualize');
    expect(markup).not.toContain('Back to chat');
    expect(markup).not.toContain('Share');
    // The title is static text, not a click-to-edit button (round-two behaviour,
    // re-asserted here so the header render is checked as a whole).
    expect(countButtons(markup)).toBe(1); // just Back
  });
});
