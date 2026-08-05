import type * as React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, EyeOff, GitFork, Hash, Pencil, Pin, PinOff } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { WorkbenchSession } from '../../context/ApiContext';
import { useComposerInsertTarget } from '../../context/ComposerBridgeContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useToast } from '../../context/ToastContext';
import { useUnsavedChangesActionGuard } from '../../context/useUnsavedChangesActionGuard';
import { hideSessionToBackground } from '../../lib/sessionVisibilityActions';
import { archiveRequestIsLive, isSessionReadOnly } from './sessionArchived';
import { ArchiveSessionDialog } from './ArchiveSessionDialog';
import type { SessionActionDescriptor } from './sessionActions';

// ── One session action model, four render sites ───────────────────────────────
// The desktop sidebar row, its right-click menu, the mobile projects row and the
// chat header share one action model. They used to be written out three times
// (and had already drifted: fork was disabled-with-tooltip on desktop but hidden
// on mobile, and "reference" existed only on desktop). This hook owns the list,
// the writes and the pending state; each surface owns only where the trigger sits
// and which local capabilities it supplies. The rows themselves render through
// SessionActionMenu (sessionActions.tsx).

export interface SessionActionsOptions {
  /** ``null`` (no session yet, or a read-only one) yields no actions and an inert
   *  archive request, so callers can invoke the hook unconditionally. */
  session: WorkbenchSession | null;
  /** Capability gate for otherwise-live sessions. Remote Organization readers
   *  can inspect a session but must not receive local write actions. */
  writable?: boolean;
  /** Project whose cached list holds the row — a CACHE address, not a permission.
   *  Defaults to ``session.project_id``, which is ``null`` for a standalone session
   *  (no project-bound scope); the writes run either way. */
  projectId?: string | null;
  /** Start the surface's own rename editor. Omit when that surface intentionally
   *  does not expose Rename, such as the compact Chat header menu. */
  onRenameStart?: () => void;
  /** Navigate to a session (the fork target). Always called inside the
   *  unsaved-changes authorization's runner (see below). */
  onOpenSession: (sessionId: string) => void;
  /** After a successful archive — the chat leaves the dead session, rows just drop. */
  onArchived?: () => void;
  /** Mirror a write into a surface-local copy of the session (the chat page holds
   *  its own ``session`` state, which the provider cache doesn't feed). The session
   *  id is passed back so a surface that has since moved on can ignore it. */
  onSessionPatched?: (changes: Partial<WorkbenchSession>, sessionId: string) => void;
  /** Keyboard hint for the archive row (see chatShortcuts). */
  archiveHint?: string;
}

export interface SessionActionsHandle {
  actions: SessionActionDescriptor[];
  /** Render this next to the menu — it carries its own open state. */
  archiveDialog: React.ReactNode;
  /** Open the archive confirm dialog (menu row, or the ⌘⇧D shortcut). */
  requestArchive: () => void;
  /** ``requestArchive`` can actually do something — i.e. there is a writable
   *  session. The keyboard shortcut binds only while this is true, so it never
   *  swallows the browser's own ⌘⇧D on a read-only or still-loading chat. */
  canArchive: boolean;
}

export const useSessionActions = ({
  session,
  writable = true,
  projectId,
  onRenameStart,
  onOpenSession,
  onArchived,
  onSessionPatched,
  archiveHint,
}: SessionActionsOptions): SessionActionsHandle => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const { forkSession, setSessionPinned, archiveSession } = useWorkbenchProjectsTree();
  // Owned HERE, not passed in per surface: the unsaved-changes blocker is mounted on
  // the ROUTER, so any surface that navigates after a write — sidebar, mobile row,
  // chat header — is prompted only once the fork already exists, and cancelling
  // orphans it. Asking inside the hook means a new surface inherits the pre-flight
  // instead of having to remember an option (Codex found the chat header missing it).
  const authorizeNavigation = useUnsavedChangesActionGuard();
  const insertTarget = useComposerInsertTarget();
  // The session an archive request was made FOR, not a bare "open" flag: this hook
  // instance outlives any one session (ChatPage is reused across ids), so a boolean
  // would be inherited by the next session — see archiveRequestIsLive.
  const [archiveRequestId, setArchiveRequestId] = useState<string | null>(null);
  const [pinning, setPinning] = useState(false);
  const [forking, setForking] = useState(false);
  // Refs, not the state above: a second click can land before React re-renders
  // with the disabled row (a double fork spawns two sessions).
  const pinningRef = useRef(false);
  const forkingRef = useRef(false);

  // A read-only session (archived, or a runtime-owned ``system`` row) refuses every
  // one of these server-side — 409 archived / 403 reserved_session — so the whole
  // menu is withdrawn rather than offered as a list of guaranteed failures. Same
  // reasoning showPageControlActions already applies to the Show Page cluster.
  const target = writable && session && !isSessionReadOnly(session) ? session : null;
  const targetId = target?.id ?? null;
  const ownerProjectId = projectId ?? target?.project_id ?? null;

  // Derived, so a target that moved or went read-only closes the dialog in the SAME
  // render rather than a frame later.
  const archiveOpen = archiveRequestIsLive(archiveRequestId, targetId);
  // ...and forget the request once it is stale, so coming back to that session later
  // does not resurrect a dialog the user has long since walked away from.
  useEffect(() => {
    setArchiveRequestId(null);
  }, [targetId]);

  const requestArchive = useCallback(() => {
    if (!targetId) return;
    setArchiveRequestId(targetId);
  }, [targetId]);

  const togglePinned = useCallback(async () => {
    if (!target || pinningRef.current) return;
    pinningRef.current = true;
    setPinning(true);
    const next = !target.pinned;
    const pinnedId = target.id;
    try {
      await setSessionPinned(ownerProjectId, pinnedId, next);
      // Carry the id: this resolves after an await, and the surface may have moved
      // to another session by then (patching it would flip the wrong row).
      onSessionPatched?.({ pinned: next }, pinnedId);
    } catch {
      // apiFetch already surfaced the error toast.
    } finally {
      pinningRef.current = false;
      setPinning(false);
    }
  }, [target, ownerProjectId, setSessionPinned, onSessionPatched]);

  const fork = useCallback(async () => {
    if (!target || forkingRef.current) return;
    // Ask FIRST (the prompt is synchronous), write second: a cancelled
    // unsaved-changes prompt must not leave a forked session nobody navigated to.
    // ``null`` is the user cancelling; with nothing unsaved the gate authorizes
    // straight through.
    const authorization = authorizeNavigation();
    if (!authorization) return;
    forkingRef.current = true;
    setForking(true);
    try {
      const forked = await forkSession(ownerProjectId, target.id);
      if (!forked) return;
      // runNavigation carries the already-granted authorization into the route
      // change, so the router blocker doesn't prompt a second time for one action.
      authorization.runNavigation(() => onOpenSession(forked.id));
    } finally {
      forkingRef.current = false;
      setForking(false);
    }
  }, [target, ownerProjectId, forkSession, onOpenSession, authorizeNavigation]);

  const hide = useCallback(() => {
    if (!target) return;
    void hideSessionToBackground({
      sessionId: target.id,
      setSessionVisibility: api.setSessionVisibility,
      showToast,
      hiddenMessage: t('workbench.sessionHiddenToast'),
      undoLabel: t('common.undo'),
    });
  }, [target, api.setSessionVisibility, showToast, t]);

  const actions = useMemo<SessionActionDescriptor[]>(() => {
    if (!target) return [];
    const canFork = Boolean(target.native_session_id);
    // "Reference this session" needs a chat composer mounted somewhere else — you
    // cannot reference the session you are already typing in, which is also why
    // the chat header never shows this row for its own session.
    const canReference = insertTarget != null && insertTarget.sessionId !== target.id;
    const rows: SessionActionDescriptor[] = [
      {
        id: 'pin',
        group: 'organize',
        icon: target.pinned ? PinOff : Pin,
        label: t(target.pinned ? 'workbench.sessionUnpin' : 'workbench.sessionPin'),
        pending: pinning,
        disabled: pinning,
        onSelect: () => void togglePinned(),
      },
    ];
    if (onRenameStart) {
      rows.push({
        id: 'rename',
        group: 'organize',
        icon: Pencil,
        label: t('workbench.sessionRename'),
        onSelect: onRenameStart,
      });
    }
    if (canReference) {
      rows.push({
        id: 'reference',
        group: 'continue',
        icon: Hash,
        label: t('workbench.sessionReference'),
        onSelect: () => insertTarget?.insertSessionReference(target.id, target.title),
      });
    }
    rows.push(
      {
        id: 'fork',
        group: 'continue',
        icon: GitFork,
        label: t('workbench.sessionFork'),
        // Disabled-with-a-reason on every surface: a session with no native
        // conversation has nothing to fork, and hiding the row (what mobile used
        // to do) teaches the user nothing.
        disabled: !canFork || forking,
        pending: forking,
        title: canFork ? undefined : t('workbench.sessionForkUnavailable'),
        onSelect: () => void fork(),
      },
      {
        id: 'hide',
        group: 'continue',
        icon: EyeOff,
        label: t('workbench.sessionHideToBackground'),
        onSelect: hide,
      },
      {
        id: 'archive',
        group: 'lifecycle',
        icon: Archive,
        label: t('workbench.sessionArchive'),
        hint: archiveHint,
        danger: true,
        onSelect: requestArchive,
      },
    );
    return rows;
  }, [
    target,
    insertTarget,
    t,
    pinning,
    forking,
    togglePinned,
    onRenameStart,
    fork,
    hide,
    requestArchive,
    archiveHint,
  ]);

  const archiveDialog = target ? (
    <ArchiveSessionDialog
      sessionId={archiveOpen ? archiveRequestId : null}
      sessionTitle={target.title}
      open={archiveOpen}
      onOpenChange={(open) => setArchiveRequestId(open ? targetId : null)}
      onConfirm={async () => {
        await archiveSession(ownerProjectId, target.id);
        onArchived?.();
      }}
    />
  ) : null;

  return { actions, archiveDialog, requestArchive, canArchive: target != null };
};
