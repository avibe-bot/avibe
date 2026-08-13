import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchSession } from '../../context/ApiContext';
import type { UnsavedChangesActionAuthorization } from '../../lib/unsavedChangesRegistry';
import type { ArchiveSessionDialogProps } from './ArchiveSessionDialog';
import { useSessionActions, type SessionActionsHandle, type SessionActionsOptions } from './useSessionActions';

// The hook only needs four contexts and `t`. Mocking them (rather than mounting the
// real providers) keeps this a jsdom-free unit test of the WRITES — which project id
// reaches the provider, in what order the unsaved-changes guard runs, and which
// session id a resolved write reports.
const mocks = vi.hoisted(() => ({
  forkSession: vi.fn<(projectId: string | null, sessionId: string) => Promise<WorkbenchSession | null>>(),
  setSessionPinned: vi.fn<(projectId: string | null, sessionId: string, pinned: boolean) => Promise<void>>(),
  archiveSession: vi.fn<(projectId: string | null, sessionId: string) => Promise<void>>(),
  setSessionVisibility: vi.fn(),
  showToast: vi.fn(),
  authorizeRouteAction: vi.fn<() => UnsavedChangesActionAuthorization | null>(),
}));

vi.mock('../../context/ApiContext', () => ({
  useApi: () => ({ setSessionVisibility: mocks.setSessionVisibility }),
}));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: mocks.showToast }) }));
vi.mock('../../context/ComposerBridgeContext', () => ({ useComposerInsertTarget: () => null }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsTree: () => ({
    forkSession: mocks.forkSession,
    setSessionPinned: mocks.setSessionPinned,
    archiveSession: mocks.archiveSession,
  }),
}));
vi.mock('../../context/useUnsavedChangesActionGuard', () => ({
  useUnsavedChangesActionGuard: () => mocks.authorizeRouteAction,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const session = (over: Partial<WorkbenchSession> = {}): WorkbenchSession =>
  ({
    id: 'ses_standalone',
    scope_id: 'scope-1',
    project_id: null, // standalone: no project-bound scope, so no project row
    title: 'Standalone',
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    native_session_id: 'native-1',
    ...over,
  }) as WorkbenchSession;

// renderToStaticMarkup runs the hook body once and never re-renders, which is all
// this needs: the returned descriptors close over refs and callbacks, so calling
// them afterwards exercises the real write paths (the pending state updates are
// no-ops on the server, and are covered by the presentation test instead).
const Probe = ({
  options,
  capture,
}: {
  options: SessionActionsOptions;
  capture: (handle: SessionActionsHandle) => void;
}) => {
  capture(useSessionActions(options));
  return null;
};
const mount = (options: SessionActionsOptions): SessionActionsHandle => {
  let handle: SessionActionsHandle | null = null;
  renderToStaticMarkup(
    <Probe
      options={options}
      capture={(captured) => {
        handle = captured;
      }}
    />,
  );
  if (!handle) throw new Error('hook did not run');
  return handle;
};

const select = (h: SessionActionsHandle, id: string) => {
  const action = h.actions.find((candidate) => candidate.id === id);
  if (!action) throw new Error(`no ${id} action`);
  action.onSelect();
};
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const confirmArchive = (h: SessionActionsHandle) =>
  (h.archiveDialog as ReactElement<ArchiveSessionDialogProps>).props.onConfirm();

const options = (over: Partial<SessionActionsOptions> = {}): SessionActionsOptions => ({
  session: session(),
  onRenameStart: vi.fn(),
  onOpenSession: vi.fn(),
  ...over,
});

/** What the real gate returns when there is nothing unsaved to warn about: an
 *  authorization that runs the navigation straight through. */
const grantedAuthorization = (): UnsavedChangesActionAuthorization => ({
  runNavigation: vi.fn((navigation: () => void) => navigation()),
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.forkSession.mockResolvedValue(session({ id: 'ses_forked' }));
  mocks.setSessionPinned.mockResolvedValue(undefined);
  mocks.archiveSession.mockResolvedValue(undefined);
  mocks.authorizeRouteAction.mockImplementation(grantedAuthorization);
});

describe('surface-specific actions', () => {
  it('omits rename when the surface provides no rename editor', () => {
    const h = mount(options({ onRenameStart: undefined }));

    expect(h.actions.map((action) => action.id)).not.toContain('rename');
    expect(h.actions.map((action) => action.id)).toContain('pin');
  });
});

// ── Codex review (useSessionActions.tsx:81) ──────────────────────────────────
// `project_id` is the `proj_*` suffix of the scope id, so it is NULL for every
// session outside a project. The hook used to bail out of pin / fork when it was
// null, and archive CONFIRMED SUCCESSFULLY without archiving anything: the dialog
// closed, the row stayed. The project id is a cache address, not a permission.
describe('a session with no project (project_id: null)', () => {
  it('pins through to the API and reports the write', () => {
    const onSessionPatched = vi.fn();
    const h = mount(options({ onSessionPatched }));

    select(h, 'pin');

    expect(mocks.setSessionPinned).toHaveBeenCalledWith(null, 'ses_standalone', true);
    return flush().then(() => {
      // The id travels with the patch: the write resolves after an await, and the
      // surface may have navigated to another session by then.
      expect(onSessionPatched).toHaveBeenCalledWith({ pinned: true }, 'ses_standalone');
    });
  });

  it('unpins a currently pinned session', () => {
    const h = mount(options({ session: session({ pinned: true }) }));

    select(h, 'pin');

    expect(mocks.setSessionPinned).toHaveBeenCalledWith(null, 'ses_standalone', false);
  });

  it('forks and opens the fork', async () => {
    const onOpenSession = vi.fn();
    const h = mount(options({ onOpenSession }));

    select(h, 'fork');
    await flush();

    expect(mocks.forkSession).toHaveBeenCalledWith(null, 'ses_standalone');
    expect(onOpenSession).toHaveBeenCalledWith('ses_forked');
  });

  it('actually archives when the dialog is confirmed', async () => {
    const onArchived = vi.fn();
    const h = mount(options({ onArchived }));

    // The dialog is closed and bound to nothing until it is asked for — the hook
    // instance is reused across sessions, so an inherited `open` is the bug class
    // `archiveRequestIsLive` exists to prevent.
    const dialog = (h.archiveDialog as ReactElement<ArchiveSessionDialogProps>).props;
    expect(dialog.open).toBe(false);
    expect(dialog.sessionId).toBeNull();

    await confirmArchive(h);

    expect(mocks.archiveSession).toHaveBeenCalledWith(null, 'ses_standalone');
    expect(onArchived).toHaveBeenCalledTimes(1);
  });

  it('still routes a project-owned session through its project', () => {
    const h = mount(options({ session: session({ project_id: 'proj-1' }) }));
    select(h, 'pin');
    expect(mocks.setSessionPinned).toHaveBeenCalledWith('proj-1', 'ses_standalone', true);

    // An explicit projectId (what the sidebar rows pass) wins over the row's own.
    const owned = mount(options({ session: session({ project_id: 'proj-1' }), projectId: 'proj-2' }));
    select(owned, 'pin');
    expect(mocks.setSessionPinned).toHaveBeenLastCalledWith('proj-2', 'ses_standalone', true);
  });
});

// ── Codex review (useSessionActions.tsx:139) ─────────────────────────────────
// The sidebar's unsaved-changes prompt used to run inside `onOpenSession`, i.e.
// AFTER the fork request had already been sent: cancelling the prompt left an orphan
// forked session behind. The guard is a pre-flight now — and the hook asks for it
// ITSELF rather than taking it as an option, so a surface cannot forget to pass it
// (the chat header and the mobile projects row both had).
describe('fork under the unsaved-changes guard', () => {
  it('consults the router guard without being handed one', () => {
    mount(options());
    expect(mocks.authorizeRouteAction).not.toHaveBeenCalled(); // asked per action, not per render
  });

  it('writes nothing when the user cancels the prompt', async () => {
    mocks.authorizeRouteAction.mockReturnValue(null);
    const onOpenSession = vi.fn();
    const h = mount(options({ onOpenSession }));

    select(h, 'fork');
    await flush();

    expect(mocks.authorizeRouteAction).toHaveBeenCalledTimes(1);
    expect(mocks.forkSession).not.toHaveBeenCalled();
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it('carries the granted authorization into the navigation', async () => {
    const granted = grantedAuthorization();
    mocks.authorizeRouteAction.mockReturnValue(granted);
    const onOpenSession = vi.fn();
    const h = mount(options({ onOpenSession }));

    select(h, 'fork');
    await flush();

    expect(mocks.forkSession).toHaveBeenCalledTimes(1);
    // Navigating THROUGH runNavigation is what stops the guard prompting twice for
    // one action; calling navigate() directly would re-block it.
    expect(granted.runNavigation).toHaveBeenCalledTimes(1);
    expect(onOpenSession).toHaveBeenCalledWith('ses_forked');
  });

  it('never forks twice from a double click', async () => {
    const h = mount(options());

    select(h, 'fork');
    select(h, 'fork'); // before any re-render could disable the row

    await flush();
    expect(mocks.forkSession).toHaveBeenCalledTimes(1);
  });
});

describe('a read-only session', () => {
  it('offers nothing to click, no dialog, and an inert shortcut', () => {
    for (const readOnly of [session({ status: 'archived' }), session({ visibility: 'system' }), null]) {
      const h = mount(options({ session: readOnly }));

      expect(h.actions).toEqual([]);
      expect(h.archiveDialog).toBeNull();
      // canArchive is what ChatPage binds ⌘⇧D on, so the chord is not swallowed.
      expect(h.canArchive).toBe(false);
      h.requestArchive();
      expect(mocks.archiveSession).not.toHaveBeenCalled();
    }

    expect(mount(options()).canArchive).toBe(true);
  });
});
