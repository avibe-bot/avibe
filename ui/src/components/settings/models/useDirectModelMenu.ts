import * as React from 'react';

import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { modelsApi, type DirectMenuRow } from './modelsApi';
import type { AgentBackend } from './types';

export type DirectModelMenuState = {
  /** The menu the Session model pickers offer for this backend; null until read. */
  models: DirectMenuRow[] | null;
  /** Nothing to show yet without a live read (OpenCode with no cached options). */
  unread: boolean;
  failed: boolean;
  /** Rows the backend offers now that the menu hides: the restore list. */
  removed: DirectMenuRow[];
  canEdit: boolean;
  pending: ReadonlySet<string>;
  /** The last write that failed, named for its row. */
  writeFailure: { row: DirectMenuRow; removed: boolean } | null;
  setRemoved: (row: DirectMenuRow, removed: boolean) => void;
  /** Read again; `live` lets OpenCode read its options live (it may start OpenCode). */
  read: (live: boolean) => void;
};

const NO_ROWS: DirectMenuRow[] = [];

/**
 * A Direct backend's model menu as Settings · Models shows it: the server's
 * Direct menu, the one its Session pickers, `vibe agent models` and the IM
 * routing form offer. Whoever may add models (`can_manage_instance`) removes a
 * model from it here or brings one back; removing hides the model from every
 * menu, and a Session or Agent already on it keeps it. OpenCode is read from
 * its cached options: opening this page never starts it.
 * ui/design: models/direct-menu/list
 */
export function useDirectModelMenu(backend: AgentBackend, enabled: boolean): DirectModelMenuState {
  const { capabilities } = useInstanceAuthorization();
  const canEdit = capabilities.can_manage_instance;
  const [models, setModels] = React.useState<DirectMenuRow[] | null>(null);
  const [unread, setUnread] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  const [removed, setRemovedRows] = React.useState<DirectMenuRow[]>(NO_ROWS);
  const [pending, setPending] = React.useState<ReadonlySet<string>>(new Set());
  const [writeFailure, setWriteFailure] = React.useState<DirectModelMenuState['writeFailure']>(null);
  const [request, setRequest] = React.useState({ generation: 0, live: false });
  const enabledBackend = enabled ? backend : null;

  React.useEffect(() => {
    if (!enabledBackend) return undefined;
    let cancelled = false;
    void modelsApi.getDirectMenu(enabledBackend, request.live).then(
      (answer) => {
        if (cancelled) return;
        setModels(answer.models);
        setUnread(answer.models === null);
        // A failed read keeps the rows it had; only an answer replaces them.
        setRemovedRows(answer.removed);
        setFailed(false);
      },
      () => { if (!cancelled) setFailed(true); },
    );
    return () => { cancelled = true; };
  }, [enabledBackend, request]);

  // Rows read for one backend never stand for another.
  const [shownBackend, setShownBackend] = React.useState(enabledBackend);
  if (shownBackend !== enabledBackend) {
    setShownBackend(enabledBackend);
    setModels(null);
    setUnread(false);
    setFailed(false);
    setRemovedRows(NO_ROWS);
    setWriteFailure(null);
  }

  const setRemoved = React.useCallback((row: DirectMenuRow, hide: boolean) => {
    setPending((current) => new Set(current).add(row.id));
    setWriteFailure(null);
    void modelsApi.setDirectModelRemoved(backend, row.id, hide).then(
      () => setRequest((current) => ({ generation: current.generation + 1, live: false })),
      () => setWriteFailure({ row, removed: hide }),
    ).finally(() => setPending((current) => {
      const next = new Set(current);
      next.delete(row.id);
      return next;
    }));
  }, [backend]);

  const read = React.useCallback((live: boolean) => setRequest((current) => ({ generation: current.generation + 1, live })), []);
  return { models, unread, failed, removed: canEdit ? removed : NO_ROWS, canEdit, pending, writeFailure, setRemoved, read };
}
