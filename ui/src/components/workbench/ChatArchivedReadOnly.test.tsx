import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { Archive } from 'lucide-react';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { selectApiErrorFields } from '../../context/apiErrorParse';
import type { VaultRequest, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { ToastProvider } from '../../context/ToastProvider';
import { isVoiceControlDisabled } from '../../lib/voiceRecording';
import { ChatHeaderBar, MessageRow, ThinkingBubble } from './ChatPage';
import { sessionAgentDisplayName } from './sessionAgentName';
import { Composer } from './Composer';
import { QuickReplies } from './QuickReplies';
import {
  archiveRequestIsLive,
  isSessionArchivedConflict,
  isSessionArchivedError,
  isSessionReadOnly,
  isShowPageActive,
  markSessionArchived,
  sessionReadOnlyReason,
  showPageControlActions,
  transcriptSelectionActions,
  WORKSPACE_NOTICE_SESSION_ID,
} from './sessionArchived';
import { SecretRequestCard } from '../ui/secret-request-card';

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
      <ToastProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </ToastProvider>
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

const archivedPm = {
  id: 'agt-pm',
  name: '_pm-8dd7',
  display_name: 'pm',
  description: null,
  backend: 'codex',
  model: null,
  reasoning_effort: null,
  enabled: false,
  archived: true,
  archived_at: '2026-07-31T14:00:00Z',
  source: 'user',
  updated_at: '2026-07-31T14:00:00Z',
} satisfies VibeAgentBrief;

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

describe('archived Agent display names', () => {
  it('keeps the display name when a mounted catalog still has the pre-archive name', () => {
    const staleCatalogAgent = { ...archivedPm, name: 'pm' };
    const archivedSession = session({ agent_id: archivedPm.id, agent_name: archivedPm.name });

    expect(sessionAgentDisplayName(archivedSession, [staleCatalogAgent])).toBe('pm');
  });

  it('uses the catalog display name in thinking and transcript bubbles', () => {
    const archivedSession = session({ agent_name: archivedPm.name });
    const displayName = archivedPm.display_name;

    const thinking = render(
      <ThinkingBubble session={archivedSession} agentDisplayName={displayName} />,
    );
    const message = render(
      <MessageRow
        message={agentWithQuickReplies()}
        session={archivedSession}
        agentDisplayName={displayName}
        messageFontSize={13}
      />,
    );

    expect(thinking).toContain('>pm</span>');
    expect(message).toContain('>pm</span>');
    expect(thinking).not.toContain(archivedPm.name);
    expect(message).not.toContain(archivedPm.name);
  });
});

describe('nonterminal Agent output', () => {
  it('keeps Agent identity but uses the muted boundary presentation', () => {
    for (const message of [
      { ...agentWithQuickReplies(), type: 'output', content: {} },
      { ...agentWithQuickReplies(), type: 'result', content: {}, metadata: { detached: true } },
    ] as WorkbenchMessage[]) {
      const markup = render(
        <MessageRow message={message} session={session()} messageFontSize={13} />,
      );

      expect(markup).toContain('lucide-bot');
      expect(markup).toContain('bg-foreground/[0.03]');
      expect(markup).not.toContain('bg-mint/[0.09]');
    }
  });

  it('keeps a detached backend failure in the status presentation', () => {
    const message = {
      ...agentWithQuickReplies(),
      type: 'notify',
      content: {},
      metadata: { detached: true, event: 'backend_failure' },
    } as WorkbenchMessage;
    const markup = render(
      <MessageRow message={message} session={session()} messageFontSize={13} />,
    );

    expect(markup).toContain('lucide-bell');
    expect(markup).toContain('bg-gold/[0.08]');
    expect(markup).not.toContain('lucide-bot');
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

  it('withdraws an active Show Page frame on a definitive access denial', () => {
    expect(isShowPageActive(false, true, false)).toBe(true);
    expect(isShowPageActive(false, true, true)).toBe(false);
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
        onPrepareShowPageLaunch={async () => false}
        annotation={{
          state: null,
          iframeRef: { current: null },
          handleIframeLoad: () => undefined,
          handleShortcutKeyDown: () => undefined,
          enable: () => undefined,
          disable: () => undefined,
          setMode: () => undefined,
        }}
        readOnlyReason="archived"
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

  it('withdraws the session ⋯ menu on a read-only header even if actions are passed', () => {
    // useSessionActions already returns an empty list for a read-only session, so
    // this pins the second half: pin / rename / fork / hide / archive are all
    // refused server-side (409 archived / 403 reserved_session), so the header must
    // not mount a trigger for them. The trigger is the only new button in the
    // cluster, hence the same Back-only count as above.
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
        onPrepareShowPageLaunch={async () => false}
        annotation={{
          state: null,
          iframeRef: { current: null },
          handleIframeLoad: () => undefined,
          handleShortcutKeyDown: () => undefined,
          enable: () => undefined,
          disable: () => undefined,
          setMode: () => undefined,
        }}
        readOnlyReason="archived"
        sessionActions={[
          {
            id: 'archive',
            group: 'lifecycle',
            icon: Archive,
            label: en.workbench.sessionArchive,
            danger: true,
            onSelect: () => undefined,
          },
        ]}
      />,
    );
    expect(markup).toContain('Model Hub');
    expect(markup).not.toContain(en.workbench.sessionActions);
    expect(markup).not.toContain(en.workbench.sessionArchive);
    expect(countButtons(markup)).toBe(1); // still just Back
  });
});

// ── Codex review #4 (Composer.tsx / ChatPage.tsx:2426) ────────────────────────
// Round 3 classified the Stop button as "safe — readOnly && busy is unreachable".
// That was wrong. archive_session cannot cancel an in-flight chat turn inside its
// transaction (the turn lives in the controller process, reachable only over the
// internal socket), so the DELETE route commits the archive FIRST and cancels
// best-effort afterwards. ChatPage bootstraps ``busy`` from the controller's
// ``turn_state.foreground`` — not the row's ``agent_status``, which archive does
// reset — so an archived chat opened inside that cancellation window loads with
// readOnly AND busy true. In that state the busy branch rendered an ENABLED Stop
// button and its "working" placeholder overrode placeholderArchived.
//
// ``busy && disabled`` is incoherent for every Composer caller, not just this
// one, so it is resolved in the shared component (``busyControls``) rather than
// by pre-clearing ``busy`` at the call site.
describe('a disabled composer has no live-turn controls', () => {
  const composer = (props: { busy?: boolean; disabled?: boolean }) =>
    render(
      <Composer
        onSend={() => undefined}
        onStop={() => undefined}
        placeholder={en.chat.compose.placeholderArchived}
        {...props}
      />,
    );

  it('keeps Stop and the busy placeholder while busy and writable', () => {
    const markup = composer({ busy: true });
    // Unchanged live behaviour: Stop is offered, and "working" wins over the
    // caller's idle placeholder override.
    expect(markup).toContain(`aria-label="${en.chat.compose.stop}"`);
    expect(markup).toContain(en.chat.compose.placeholderBusy);
    expect(markup).not.toContain(en.chat.compose.placeholderArchived);
  });

  it('withdraws Stop when the same turn is showing in a read-only chat', () => {
    const markup = composer({ busy: true, disabled: true });
    expect(markup).not.toContain(`aria-label="${en.chat.compose.stop}"`);
    // Not merely hidden-Stop: the row falls back to the ordinary Send button,
    // which is inert because ``disabled`` already clears ``canSubmit``.
    expect(markup).toContain(`aria-label="${en.chat.compose.send}"`);
    expect(countDisabledButtons(markup)).toBe(countButtons(markup));
  });

  it('lets the archived placeholder win over the busy one, so the reason is readable', () => {
    const markup = composer({ busy: true, disabled: true });
    // This is the i18n-visible half of the same defect: placeholderBusy is not the
    // truth for a session that can never accept a queued message.
    expect(markup).toContain(en.chat.compose.placeholderArchived);
    expect(markup).not.toContain(en.chat.compose.placeholderBusy);
    // And the textarea is inert (the plain path honours ``disabled`` since r2).
    expect(markup).toContain('<textarea');
    expect(markup).toContain('disabled=""');
  });

  it('keeps only an active recording Stop control available after archiving', () => {
    expect(isVoiceControlDisabled(true, true, false)).toBe(false);
    expect(isVoiceControlDisabled(true, false, false)).toBe(true);
    expect(isVoiceControlDisabled(true, true, true)).toBe(true);
  });

  // The busy branch's sibling "send to queue" button needs no case of its own: it
  // is gated on ``canSubmit``, which ``disabled`` already clears, so it never
  // rendered here even before this fix.
});

// ── Codex review #5a (ChatPage.tsx:1280) ──────────────────────────────────────
// Round 2 wired convergence into ``sendMessage`` only. The next verb the reviewer
// tried — a rename / agent re-route — got its 409 stored as error text while the
// title editor and route picker stayed live, so it could re-issue a permanently
// rejected PATCH forever. Rather than add a second per-verb branch (which is what
// produced this finding three rounds running), convergence moved UP to the API
// layer: ``handleApiError`` announces every ``session_archived`` body via
// ``onSessionArchived`` and ChatPage subscribes once. ``isSessionArchivedError`` is
// the classifier for that layer's already-parsed ``ApiError``; the path→session-id
// half is pinned in ui/src/context/ApiErrorParse.test.ts.
describe('archived conflicts converge whatever the verb', () => {
  it('recognizes the archived conflict on an ApiError, not just a raw body', () => {
    // What ``updateSession``/``forkSession``/``ensureShowPage`` reject with, once
    // handleApiError has parsed the structured body.
    expect(isSessionArchivedError({ name: 'ApiError', status: 409, code: 'session_archived' })).toBe(true);
    // Every other rejection stays an ordinary, reportable error.
    expect(isSessionArchivedError({ name: 'ApiError', status: 409, code: 'backend_locked' })).toBe(false);
    expect(isSessionArchivedError({ name: 'ApiError', status: 404, code: null })).toBe(false);
    // A network failure is a plain Error with no code — must not be mistaken for a
    // terminal archive, or an offline tab would freeze itself read-only.
    expect(isSessionArchivedError(new Error('Failed to fetch'))).toBe(false);
    expect(isSessionArchivedError(null)).toBe(false);
    expect(isSessionArchivedError(undefined)).toBe(false);
  });

  it('converges the same way for every verb, because they share one reducer', () => {
    // Whichever write reported it, the applied fact is identical: the loaded row
    // turns archived, which is what flips readOnly and withdraws the controls that
    // issued the doomed request in the first place.
    const stale = session({ status: 'active' });
    const converged = markSessionArchived(stale, stale.id);
    expect(isSessionReadOnly(converged)).toBe(true);
    // ...and with readOnly true, the title editor and the route picker are both
    // gone, so a rejected PATCH cannot be re-issued from this render.
    const markup = render(
      <ChatHeaderBar
        session={converged!}
        agents={[]}
        defaultAgentName={null}
        onPatch={async () => undefined}
        onBack={() => undefined}
        working={false}
        showPageMode={false}
        showPageBusy={false}
        onToggleShowPage={() => undefined}
        onPrepareShowPageLaunch={async () => false}
        annotation={{
          state: null,
          iframeRef: { current: null },
          handleIframeLoad: () => undefined,
          handleShortcutKeyDown: () => undefined,
          enable: () => undefined,
          disable: () => undefined,
          setMode: () => undefined,
        }}
        readOnlyReason="archived"
      />,
    );
    expect(markup).toContain('Model Hub');
    expect(countButtons(markup)).toBe(1); // Back only: no pencil, no route picker
  });

  it('has distinct copy for a blocked edit and a blocked send', () => {
    // ``errors.session_archived`` (what handleApiError resolves) is Show-Page-worded
    // and wrong for a rename, so the PATCH path says so in its own words. Both keys
    // must exist and differ, or one of the two verbs reports the other's reason.
    expect(en.chat.archived.editBlocked).toBeTruthy();
    expect(en.chat.archived.sendBlocked).toBeTruthy();
    expect(en.chat.archived.editBlocked).not.toBe(en.chat.archived.sendBlocked);
  });
});

// ── Codex review round 15 (storage/messages_service.py:1207) ──────────────────
// The reserved workspace-notifications session is ``visibility === 'system'``, which
// admits it to the Inbox on purpose — so its card is a clickable chat. Archive was the
// only read-only reason the chat surface knew, so that card opened a fully writable
// composer into a row the runtime owns ("no backend and no turns"): typing into it
// dispatched a real agent turn and mixed conversation into the failure-notice
// transcript. The server now answers ``403 reserved_session`` there; this pins the
// client half — the affordances go, and the copy does NOT claim the row is archived.
describe('a runtime-owned system session is read-only for its own reason', () => {
  const systemSession = () => session({ visibility: 'system', agent_name: null, agent_backend: '' });

  it('reads read-only from the visibility projection, not from the status', () => {
    expect(sessionReadOnlyReason(session())).toBeNull();
    expect(isSessionReadOnly(session())).toBe(false);

    const owned = systemSession();
    expect(owned.status).toBe('active'); // NOT archived — the point of the reason
    expect(sessionReadOnlyReason(owned)).toBe('system');
    expect(isSessionReadOnly(owned)).toBe(true);

    // The two assignable visibilities stay ordinary chats. ``background`` is hidden
    // from lists but is still the USER's session and still accepts turns.
    expect(sessionReadOnlyReason(session({ visibility: 'background' }))).toBeNull();
    expect(sessionReadOnlyReason(session({ visibility: 'foreground' }))).toBeNull();
    // A payload from an older client that predates the field is not read-only.
    expect(sessionReadOnlyReason(session({ visibility: undefined }))).toBeNull();
    expect(sessionReadOnlyReason(null)).toBeNull();
    // Terminal lifecycle outranks ownership when a system row is somehow archived.
    expect(sessionReadOnlyReason(session({ visibility: 'system', status: 'archived' }))).toBe('archived');
  });

  it('recognizes the reserved session by IDENTITY when its visibility has drifted', () => {
    // The server's ``session_is_runtime_owned`` is two tests OR'd: the visibility
    // projection AND the reserved identity, because the reserved row heals its
    // visibility only lazily — on the next notice. Between an out-of-band update
    // and that heal, the Inbox (which admits foreground) still reaches the row as
    // ``foreground``; a visibility-only client predicate would render the
    // composer, route picker and fork controls just to collect
    // ``403 reserved_session`` on every one. Identity must lock it alone.
    const drifted = session({
      id: WORKSPACE_NOTICE_SESSION_ID,
      visibility: 'foreground',
      agent_name: null,
      agent_backend: '',
    });
    expect(drifted.status).toBe('active');
    expect(sessionReadOnlyReason(drifted)).toBe('system');
    expect(isSessionReadOnly(drifted)).toBe(true);
    // And the identity constant mirrors the server's, character for character —
    // a drifted copy of THIS string is the whole point of the check.
    expect(WORKSPACE_NOTICE_SESSION_ID).toBe('ses-workspace-notices');
    // An ordinary foreground session with an ordinary id stays writable.
    expect(sessionReadOnlyReason(session({ id: 'sesordinary01', visibility: 'foreground' }))).toBeNull();
  });

  it('withdraws every session write the same way archive does', () => {
    // One reason feeds all of them, so the transcript/Show Page/header affordances
    // cannot diverge per reason — this is what makes the new reason safe by
    // construction rather than by auditing each site.
    const readOnly = isSessionReadOnly(systemSession());
    expect(readOnly).toBe(true);
    expect(transcriptSelectionActions(systemSession(), readOnly)).toEqual({
      quote: false,
      askInNew: false,
    });
    expect(showPageControlActions(readOnly, true)).toEqual({
      visualize: false,
      share: false,
      annotate: false,
    });
    expect(isShowPageActive(readOnly, true)).toBe(false);
  });

  it('renders a System badge and no invented agent route', () => {
    const markup = render(
      <ChatHeaderBar
        session={systemSession()}
        agents={[]}
        defaultAgentName="claude"
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
          handleShortcutKeyDown: () => undefined,
          enable: () => undefined,
          disable: () => undefined,
          setMode: () => undefined,
        }}
        readOnlyReason="system"
      />,
    );
    expect(markup).toContain('Model Hub'); // the header did render
    expect(markup).toContain(en.common.systemSession);
    // It is not archived, so it must not say so...
    expect(markup).not.toContain(en.common.archived);
    // ...and it has no backend, so the default agent's name must not be borrowed as
    // this session's route (the archived header's fallback would have printed it).
    expect(markup).not.toContain('claude');
    expect(markup).not.toContain(en.newSession.defaultAgent);
    // Same withdrawn cluster as the archived header: Back only.
    expect(countButtons(markup)).toBe(1);
    expect(markup).not.toContain('Visualize');
    expect(markup).not.toContain('Share');
  });

  it('tells the composer what this session receives instead of calling it archived', () => {
    const markup = render(
      <Composer
        onSend={() => undefined}
        onStop={() => undefined}
        disabled
        placeholder={en.chat.compose.placeholderSystem}
      />,
    );
    expect(markup).toContain(en.chat.compose.placeholderSystem);
    expect(markup).not.toContain(en.chat.compose.placeholderArchived);
    // Inert, exactly like the archived composer.
    expect(countDisabledButtons(markup)).toBe(countButtons(markup));
  });

  it('reports the coded 403 as a sentence, not as "[object Object]"', () => {
    // The composer is inert, so this body is only reachable from a client whose loaded
    // payload predates ``visibility`` (the field is optional for exactly that reason) —
    // which is precisely the case that must not render an object cast to a string. The
    // send path uses a RAW apiFetch, so ``handleApiError`` never runs and the branch has
    // to apply the shared selector itself.
    const coded = {
      ok: false,
      code: 'reserved_session',
      message: 'This session only receives Avibe’s workspace failure notifications.',
      error: {
        code: 'reserved_session',
        message: 'This session only receives Avibe’s workspace failure notifications.',
      },
    };
    const parsed = selectApiErrorFields(coded, 'HTTP 403');
    expect(parsed?.code).toBe('reserved_session');
    expect(parsed?.fallback).toBe(coded.message);
    expect(String(parsed?.fallback)).not.toBe('[object Object]');
    // The pre-fix expression, kept as the refutation.
    expect(String(coded.error)).toBe('[object Object]');
    // A flat legacy body still resolves to its sentence, so the branch is unchanged
    // for every route that has not adopted the coded shape.
    expect(selectApiErrorFields({ error: 'text or content is required' }, 'HTTP 400')?.fallback).toBe(
      'text or content is required',
    );
  });

  it('has its own copy in both bundles, distinct from the archived wording', () => {
    // A read-only reason with borrowed copy is the defect this reason exists to avoid:
    // "This session is archived" is false on a row that was never archived.
    for (const bundle of [en, zh]) {
      expect(bundle.chat.compose.placeholderSystem).toBeTruthy();
      expect(bundle.chat.compose.placeholderSystem).not.toBe(bundle.chat.compose.placeholderArchived);
      expect(bundle.common.systemSession).toBeTruthy();
      expect(bundle.common.systemSession).not.toBe(bundle.common.archived);
      // The machine code the server's 403 carries, for a client that surfaces it
      // through the shared ``errors.<code>`` resolution.
      expect(bundle.errors.reserved_session).toBeTruthy();
    }
    // en/zh parity: the Chinese bundle must not be the English string.
    expect(zh.chat.compose.placeholderSystem).not.toBe(en.chat.compose.placeholderSystem);
    expect(zh.common.systemSession).not.toBe(en.common.systemSession);
  });
});

// ── Codex review #5b (ChatPage.tsx:3257) ──────────────────────────────────────
// Round 3 classified this "safe" because the write is a MACHINE-scoped vault secret
// the server accepts. That missed the point: archiving expired the session's
// provision requests, so an enabled Provide button tells the reader an agent is
// waiting for a secret when none is. The defect is the affordance asserting a live
// request, not the write. Locked (not hidden) for the same reason the quick-reply
// group is: the card is the transcript record of what was asked.
describe('read-only transcript locks the secret-request cards', () => {
  it('withdraws a stale pending provision card attached to an Agent reply', () => {
    const pending = {
      id: 'vrq_stale',
      request_type: 'provision',
      secret_name: 'STALE_API_KEY',
      requester: { session_id: 'ses_01J8XK5M8T' },
      delivery: {},
      status: 'pending',
      message_id: null,
      created_at: '2026-07-27T04:04:00.500Z',
      decided_at: null,
      expires_at: null,
    } satisfies VaultRequest;

    const archived = render(
      <MessageRow
        message={agentWithQuickReplies()}
        session={session({ status: 'archived' })}
        messageFontSize={13}
        onQuickReply={() => undefined}
        vaultRequests={[pending]}
        onVaultRequestResolved={() => undefined}
        readOnly
      />,
    );

    expect(archived).not.toContain('STALE_API_KEY');
  });

  it('renders the recorded ask, disabled, with the reason', () => {
    const locked = render(<SecretRequestCard name="OPENAI_API_KEY" readOnly />);
    // Still legible as a transcript entry...
    expect(locked).toContain('OPENAI_API_KEY');
    expect(locked).toContain(en.vaults.request.provide);
    // ...and inert, with the expiry stated rather than implied.
    expect(countDisabledButtons(locked)).toBe(countButtons(locked));
    expect(countButtons(locked)).toBe(1);
    expect(locked).toContain(en.vaults.request.expired);
    // No dialog is mounted at all, so there is no path to the vault write. (The
    // live card can't be rendered here for contrast: it calls useApi().)
    expect(locked).not.toContain(en.vaults.request.title);
  });

  it('threads readOnly from the transcript row through Markdown into the card', () => {
    // ``$<NAME>`` in an AGENT reply is what mints the card (linkifySecretRequests).
    const ask = agentWithQuickReplies();
    const message = { ...ask, text: 'I need $<OPENAI_API_KEY> to continue.', content: {} } as WorkbenchMessage;

    const archived = render(
      <MessageRow
        message={message}
        session={session({ status: 'archived' })}
        messageFontSize={13}
        onQuickReply={() => undefined}
        readOnly
      />,
    );
    expect(archived).toContain('OPENAI_API_KEY');
    // The row's only button is the locked card — nothing clickable survives.
    expect(countButtons(archived)).toBe(1);
    expect(countDisabledButtons(archived)).toBe(1);
    expect(archived).toContain(en.vaults.request.expired);
  });

  it('leaves the marker plain text on a NON-agent bubble, read-only or not', () => {
    // Pre-existing security gate (``secretRequests`` is keyed to authorship), pinned
    // here so the new readOnly prop can't be read as the thing that governs it.
    const userMessage = {
      id: 'msg_user',
      type: 'user',
      author: 'user',
      source: 'user',
      author_name: null,
      text: 'my key is $<OPENAI_API_KEY>',
      content: {},
      metadata: {},
      created_at: '2026-07-27T04:05:00Z',
    } as unknown as WorkbenchMessage;

    for (const readOnly of [false, true]) {
      const markup = render(
        <MessageRow
          message={userMessage}
          session={session({ status: readOnly ? 'archived' : 'active' })}
          messageFontSize={13}
          onQuickReply={() => undefined}
          readOnly={readOnly}
        />,
      );
      expect(markup).toContain('OPENAI_API_KEY');
      expect(markup).not.toContain(en.vaults.request.provide);
      expect(countButtons(markup)).toBe(0);
    }
  });
});

describe('Agent result metrics tail', () => {
  const footer = '✅ ⏱️ 5s · 🪙 1.2k tok';
  const displayedFooter = '⏱️ 5s · 🪙 1.2k tok';

  it('renders structured duration and token usage once, after the timestamp', () => {
    const markup = render(
      <MessageRow
        message={agentWithQuickReplies({ result_footer: footer })}
        session={session()}
        messageFontSize={13}
      />,
    );

    expect(markup).not.toContain(footer);
    expect(markup.split(displayedFooter)).toHaveLength(2);
    expect(markup.indexOf('2026-07-27')).toBeLessThan(markup.indexOf(displayedFooter));
    expect(markup).toContain(
      'opacity-0 group-hover/message:opacity-100 group-focus-within/message:opacity-100 pointer-coarse:opacity-100',
    );
    expect(markup).not.toContain('transition-opacity');
    expect(markup).toContain('flex-wrap');
  });

  it('moves a legacy folded footer out of the Markdown body', () => {
    const legacy = {
      ...agentWithQuickReplies(),
      text: `Answer body\n\n${footer}`,
      content: {},
    } as WorkbenchMessage;
    const markup = render(
      <MessageRow message={legacy} session={session()} messageFontSize={13} />,
    );

    expect(markup).toContain('Answer body');
    expect(markup).not.toContain(footer);
    expect(markup.split(displayedFooter)).toHaveLength(2);
    expect(markup.indexOf('2026-07-27')).toBeLessThan(markup.indexOf(displayedFooter));
  });

  it('renders a footer-only completion in metadata without an empty bubble', () => {
    const footerOnly = {
      ...agentWithQuickReplies(),
      text: footer,
      content: {},
    } as WorkbenchMessage;
    const markup = render(
      <MessageRow message={footerOnly} session={session()} messageFontSize={13} />,
    );

    expect(markup).not.toContain(footer);
    expect(markup.split(displayedFooter)).toHaveLength(2);
    expect(markup.indexOf('2026-07-27')).toBeLessThan(markup.indexOf(displayedFooter));
    expect(markup).not.toContain('vr-markdown--inherit-size');
  });

  it('removes a legacy folded footer from an error status pill', () => {
    const legacyError = {
      ...agentWithQuickReplies(),
      type: 'error',
      text: `Failed to finish\n\n${footer}`,
      content: {},
    } as WorkbenchMessage;
    const markup = render(
      <MessageRow message={legacyError} session={session()} messageFontSize={13} />,
    );

    expect(markup).toContain('Failed to finish');
    expect(markup).not.toContain(footer);
    expect(markup.split(displayedFooter)).toHaveLength(2);
    expect(markup.indexOf('2026-07-27')).toBeLessThan(markup.indexOf(displayedFooter));
  });
});

describe('agent-authored local file links', () => {
  const linkedMessage = (author: 'agent' | 'user'): WorkbenchMessage => ({
    ...agentWithQuickReplies(),
    author,
    type: author === 'agent' ? 'result' : 'user',
    text: '[open report](./reports/result.md)',
    content: {},
  }) as WorkbenchMessage;

  it('opts only Agent replies into the Editor link handler', () => {
    const props = {
      session: session({ workdir: '/workspace/project' }),
      messageFontSize: 13,
      onOpenLocalFile: () => undefined,
    };

    const agent = render(<MessageRow {...props} message={linkedMessage('agent')} />);
    expect(agent).toContain('data-local-file-link="true"');
    expect(agent).not.toContain('target="_blank"');

    const user = render(<MessageRow {...props} message={linkedMessage('user')} />);
    expect(user).not.toContain('data-local-file-link');
    expect(user).toContain('target="_blank"');
  });

  it('preserves absolute Windows destinations through Markdown URL sanitization', () => {
    const props = {
      session: session({ workdir: 'C:\\workspace' }),
      messageFontSize: 13,
      onOpenLocalFile: () => undefined,
    };
    const windowsMessage = {
      ...linkedMessage('agent'),
      text: '[open source](C:/workspace/app.py:42)',
    } as WorkbenchMessage;

    const markup = render(<MessageRow {...props} message={windowsMessage} />);
    expect(markup).toContain('data-local-file-link="true"');
    expect(markup).toContain('href="C:/workspace/app.py:42"');

    const userMarkup = render(<MessageRow {...props} message={{ ...windowsMessage, author: 'user', type: 'user' }} />);
    expect(userMarkup).not.toContain('data-local-file-link');
    expect(userMarkup).not.toContain('href="C:/workspace/app.py:42"');
  });

  it('opens UNC files locally while leaving Workbench routes as browser links', () => {
    const props = {
      session: session({ workdir: '\\\\server\\share' }),
      messageFontSize: 13,
      onOpenLocalFile: () => undefined,
    };
    const uncMessage = {
      ...linkedMessage('agent'),
      text: '[open source](%5C%5Cserver%5Cshare%5Capp.py:42)',
    } as WorkbenchMessage;
    const routeMessage = {
      ...linkedMessage('agent'),
      text: '[open files](/apps/files)',
    } as WorkbenchMessage;

    const uncMarkup = render(<MessageRow {...props} message={uncMessage} />);
    expect(uncMarkup).toContain('data-local-file-link="true"');
    expect(uncMarkup).toContain('href="%5C%5Cserver%5Cshare%5Capp.py:42"');

    const routeMarkup = render(<MessageRow {...props} message={routeMessage} />);
    expect(routeMarkup).not.toContain('data-local-file-link');
    expect(routeMarkup).toContain('href="/apps/files"');
    expect(routeMarkup).toContain('target="_blank"');
  });

  it('opens Workbench chat routes in the current document', () => {
    const props = {
      session: session({ workdir: '/workspace/project' }),
      messageFontSize: 13,
      onOpenLocalFile: () => undefined,
    };
    const chatMessage = {
      ...linkedMessage('agent'),
      text: '[open chat](/chat/session-456)',
    } as WorkbenchMessage;
    const markup = render(<MessageRow {...props} message={chatMessage} />);
    expect(markup).toContain('href="/chat/session-456"');
    expect(markup).not.toContain('target="_blank"');
    expect(markup).not.toContain('data-local-file-link');
  });

  it('lets the outer local link own clicks for a linked local image', () => {
    const linkedImageMessage = {
      ...linkedMessage('agent'),
      text: '[![preview](/tmp/preview.png)](/tmp/report.md)',
    } as WorkbenchMessage;
    const markup = render(
      <MessageRow
        message={linkedImageMessage}
        session={session({ workdir: '/workspace/project' })}
        messageFontSize={13}
        onOpenLocalFile={() => undefined}
      />,
    );

    expect(markup.match(/data-local-file-link/g)).toHaveLength(1);
    expect(markup).toContain('href="/tmp/report.md"');
    expect(markup).not.toContain('href="/tmp/preview.png"');
  });
});

// ── Codex review round 2 (useSessionActions.tsx:216) ─────────────────────────
// The archive confirm dialog is owned by a hook instance that OUTLIVES the session
// it was opened for: ChatPage is reused across session ids. Stored as a bare `open`
// boolean, a request for A was inherited by B — the dialog re-appeared already open,
// re-pointed, one Enter from archiving the wrong session.
describe('a pending archive request belongs to one session', () => {
  it('is live only while the target is still the session it was requested for', () => {
    expect(archiveRequestIsLive('ses_a', 'ses_a')).toBe(true);
    // Navigated on, or the row moved: B must not inherit A's request.
    expect(archiveRequestIsLive('ses_a', 'ses_b')).toBe(false);
    // The target went read-only mid-flight (another tab archived it) or unloaded.
    expect(archiveRequestIsLive('ses_a', null)).toBe(false);
    // Nothing requested: an existing target never opens the dialog by itself.
    expect(archiveRequestIsLive(null, 'ses_a')).toBe(false);
    expect(archiveRequestIsLive(null, null)).toBe(false);
  });
});
