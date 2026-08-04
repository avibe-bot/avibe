import type * as React from 'react';
import { useCallback, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, EyeOff, GitFork, Hash, Pencil, Pin, PinOff } from 'lucide-react';

import { useApi } from '../../context/ApiContext';
import type { WorkbenchSession } from '../../context/ApiContext';
import { useComposerInsertTarget } from '../../context/ComposerBridgeContext';
import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useToast } from '../../context/ToastContext';
import { hideSessionToBackground } from '../../lib/sessionVisibilityActions';
import { isSessionReadOnly } from './sessionArchived';
import { ArchiveSessionDialog } from './ArchiveSessionDialog';
import type { SessionActionDescriptor } from './sessionActions';

// ── One session action model, four render sites ───────────────────────────────
// The desktop sidebar row, its right-click menu, the mobile projects row and the
// chat header all offer the SAME six actions. They used to be written out three
// times (and had already drifted: fork was disabled-with-tooltip on desktop but
// hidden on mobile, and "reference" existed only on desktop). This hook owns the
// list, the writes and the pending state; each surface owns only where the
// trigger sits and what "rename" / "open" / "archived" mean locally. The rows
// themselves render through SessionActionMenu (sessionActions.tsx).

export interface SessionActionsOptions {
  /** ``null`` (no session yet, or a read-only one) yields no actions and an inert
   *  archive request, so callers can invoke the hook unconditionally. */
  session: WorkbenchSession | null;
  /** Project that owns the session — the provider's cache patching is per project.
   *  Defaults to ``session.project_id`` (what the chat surface has). */
  projectId?: string;
  /** Start the surface's own rename editor (inline input / chat header title). */
  onRenameStart: () => void;
  /** Navigate to a session (the fork target). The sidebar routes this through its
   *  unsaved-changes guard; other surfaces navigate plainly. */
  onOpenSession: (sessionId: string) => void;
  /** After a successful archive — the chat leaves the dead session, rows just drop. */
  onArchived?: () => void;
  /** Mirror a write into a surface-local copy of the session (the chat page holds
   *  its own ``session`` state, which the provider cache doesn't feed). */
  onSessionPatched?: (changes: Partial<WorkbenchSession>) => void;
  /** Keyboard hint for the archive row (see chatShortcuts). */
  archiveHint?: string;
}

export interface SessionActionsHandle {
  actions: SessionActionDescriptor[];
  /** Render this next to the menu — it carries its own open state. */
  archiveDialog: React.ReactNode;
  /** Open the archive confirm dialog (menu row, or the ⌘⇧D shortcut). */
  requestArchive: () => void;
}

export const useSessionActions = ({
  session,
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
  const insertTarget = useComposerInsertTarget();
  const [archiveOpen, setArchiveOpen] = useState(false);
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
  const target = session && !isSessionReadOnly(session) ? session : null;
  const ownerProjectId = projectId ?? target?.project_id ?? null;

  const requestArchive = useCallback(() => {
    if (!target) return;
    setArchiveOpen(true);
  }, [target]);

  const togglePinned = useCallback(async () => {
    if (!target || !ownerProjectId || pinningRef.current) return;
    pinningRef.current = true;
    setPinning(true);
    const next = !target.pinned;
    try {
      await setSessionPinned(ownerProjectId, target.id, next);
      onSessionPatched?.({ pinned: next });
    } catch {
      // apiFetch already surfaced the error toast.
    } finally {
      pinningRef.current = false;
      setPinning(false);
    }
  }, [target, ownerProjectId, setSessionPinned, onSessionPatched]);

  const fork = useCallback(async () => {
    if (!target || !ownerProjectId || forkingRef.current) return;
    forkingRef.current = true;
    setForking(true);
    try {
      const forked = await forkSession(ownerProjectId, target.id);
      if (forked) onOpenSession(forked.id);
    } finally {
      forkingRef.current = false;
      setForking(false);
    }
  }, [target, ownerProjectId, forkSession, onOpenSession]);

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
      {
        id: 'rename',
        group: 'organize',
        icon: Pencil,
        label: t('workbench.sessionRename'),
        onSelect: onRenameStart,
      },
    ];
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
      sessionId={archiveOpen ? target.id : null}
      sessionTitle={target.title}
      open={archiveOpen}
      onOpenChange={setArchiveOpen}
      onConfirm={async () => {
        if (!ownerProjectId) return;
        await archiveSession(ownerProjectId, target.id);
        onArchived?.();
      }}
    />
  ) : null;

  return { actions, archiveDialog, requestArchive };
};
