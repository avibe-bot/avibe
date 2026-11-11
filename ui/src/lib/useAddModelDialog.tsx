import { useState, type ReactNode } from 'react';

import { AddModelsDialog } from '../components/settings/models/AddModelsDialog';
import type { AgentBackend } from '../components/settings/models/types';
import { useAddModelAvailable } from './backendModels';

/**
 * A model picker's "Add model" exit, opened in place: the add dialog over the
 * picker's own surface, saving at once, and handing back the first model it
 * added so the picker can select it. `open` is null when no exit is shown
 * (`useAddModelAvailable`); the dialog, once opened, outlives the menu that
 * opened it — closing the menu is how a picker makes room for it.
 *
 * The host renders `dialog` wherever it renders anything; it is a portal.
 */
export function useAddModelDialog(
  backend: string,
  enabled: boolean,
  onAdded: (modelId: string) => void,
): { open: (() => void) | null; dialog: ReactNode } {
  const available = useAddModelAvailable(backend, enabled);
  const [adding, setAdding] = useState<AgentBackend | null>(null);
  return {
    open: available ? () => setAdding(backend as AgentBackend) : null,
    dialog: adding && (
      <AddModelsDialog
        open
        backend={adding}
        onClose={() => setAdding(null)}
        onAdded={(ids) => {
          setAdding(null);
          if (ids[0]) onAdded(ids[0]);
        }}
      />
    ),
  };
}
