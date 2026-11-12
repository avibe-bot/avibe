// The add dialog where adding is the whole job: the routes page's 「Add model」 and
// every model picker's 「Add model」 exit. The picker (or, from its footer, the
// custom-model editor) chooses; this saves the choice at once and hands back
// the IDs it added, so the caller can show or select them. Manage models keeps
// its own picker, whose picks go into that dialog's draft and its Save.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { addModelsToBackend } from './addModels';
import type { ChosenCandidate } from './backendCatalog';
import { BackendModelEditorDialog } from './BackendModelEditorDialog';
import { BackendModelPickerDialog } from './BackendModelPickerDialog';
import { modelsApi } from './modelsApi';
import type { AgentBackend, AgentSupply, BackendModel } from './types';

const NOTHING: ReadonlySet<string> = new Set();
const SUPPLIERS_CHANGED = 'settings.models.gateway.catalog.saveSuppliersChanged';
const NO_MODELS: readonly BackendModel[] = [];

/** A row written in the editor, as a pick: no supplier was shown, so none is promised. */
const editedPick = (row: BackendModel): ChosenCandidate => ({
  candidate: {
    id: row.id,
    display_name: row.display_name,
    reasoning_efforts: [...row.reasoning_efforts],
    suppliers: [],
    origin: row.origin,
    ...(row.native_protocol ? { native_protocol: row.native_protocol } : {}),
  },
  row,
});

export const AddModelsDialog: React.FC<{
  open: boolean;
  backend: AgentBackend;
  onClose: () => void;
  /** The IDs now in the list, in pick order, with the list the write left. */
  onAdded: (ids: string[], agent: AgentSupply) => void | Promise<void>;
  /** Queues the write behind any other on this backend, where a host keeps one. */
  track?: (work: () => Promise<void>) => Promise<void>;
}> = ({ open, backend, onClose, onAdded, track = (work) => work() }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [read, setRead] = React.useState<{ backend: AgentBackend; models: readonly BackendModel[] } | null>(null);
  /** The editor, over the picker, with the rows the picker had ticked: they are
   *  saved with the written model, and still ticked if the editor is cancelled. */
  const [editing, setEditing] = React.useState<{ seedId: string; picks: ChosenCandidate[] } | null>(null);
  /** A written model whose save was refused because a ticked row's suppliers
   *  changed: the editor closes so the picker can show today's suppliers, and
   *  reopens on this row. */
  const [written, setWritten] = React.useState<BackendModel | undefined>(undefined);
  const [busy, setBusy] = React.useState(false);
  /** A refused agreement about suppliers is asked again: the picker re-reads its
   *  candidates and opens with the same models picked, showing today's suppliers. */
  const [reask, setReask] = React.useState<ReadonlySet<string> | undefined>(undefined);

  // What the list holds, for the picker's 「already in the list」 and the
  // editor's duplicate check. The write reads the list again for its baseline.
  React.useEffect(() => {
    if (!open) return undefined;
    let current = true;
    void modelsApi.getAgentSources(backend).then(
      (agent) => { if (current) setRead({ backend, models: agent.catalog_models ?? [] }); },
      () => undefined,
    );
    return () => { current = false; };
  }, [backend, open]);
  const held = read?.backend === backend ? read.models : NO_MODELS;

  const listed = React.useMemo(() => (held.length ? new Set(held.map((model) => model.id)) : NOTHING), [held]);
  const effortSuggestions = React.useMemo(() => [...new Set(held.flatMap((model) => model.reasoning_efforts))], [held]);

  const add = (picks: ChosenCandidate[], row?: BackendModel) => {
    if (busy || picks.length === 0) return;
    setBusy(true);
    void track(async () => {
      const result = await addModelsToBackend(modelsApi, backend, picks);
      if (!result.ok) {
        // The picks stay on screen; the sentence says what to do about them.
        showToast(t(result.reason) as string, 'error');
        if (result.reason === SUPPLIERS_CHANGED) {
          // The refused projection must not be sent again from the editor, and the
          // new suppliers are for the user to see: back to the picker, re-asked.
          setReask(new Set(picks.filter((pick) => pick.candidate.id !== row?.id).map((pick) => pick.candidate.id)));
          if (row) {
            setWritten(row);
            setEditing(null);
          }
        }
        return;
      }
      setEditing(null);
      setWritten(undefined);
      await Promise.resolve(onAdded(result.ids, result.agent)).catch(() => undefined);
    }).finally(() => setBusy(false));
  };

  return (
    <>
      <BackendModelPickerDialog
        open={open}
        backend={backend}
        listedIds={listed}
        busy={busy}
        seedPicked={reask}
        onCancel={() => {
          // A write in flight belongs to this dialog: it cannot be walked away from.
          if (busy) return;
          setEditing(null);
          setWritten(undefined);
          onClose();
        }}
        onAdd={add}
        onCustom={(seedId, picks) => setEditing({ seedId, picks })}
      />
      {open && editing !== null && (
        <BackendModelEditorDialog
          open
          backend={backend}
          model={null}
          seedId={editing.seedId}
          draftSeed={written}
          takenIds={listed}
          effortSuggestions={effortSuggestions}
          busy={busy}
          onCancel={() => { setEditing(null); setWritten(undefined); }}
          onCommit={(row) => add([...editing.picks.filter((pick) => pick.candidate.id !== row.id), editedPick(row)], row)}
        />
      )}
    </>
  );
};
