import { Suspense, useEffect, useState, type ReactNode } from 'react';

import type { AgentBackend } from '../components/settings/models/types';
import { hubCatalogModels, useAddModelAvailable, type BackendModels } from './backendModels';
import { lazyPage } from './lazyPage';

// The add dialog's code (with the model editor and picker it opens) loads when a picker first
// opens it: the composer's model picker that offers this exit is on the first screen.
const AddModelsDialog = lazyPage(() => import('../components/settings/models/AddModelsDialog').then((module) => module.AddModelsDialog));

/**
 * A model picker's "Add model" exit, opened in place: the add dialog over the
 * picker's own surface, saving at once, and handing back the first model it
 * added — with the list the write left, so the picker judges the new model's
 * efforts by what was just saved — so the picker can select it. `open` is null when no exit is shown
 * (`useAddModelAvailable`); the dialog, once opened, outlives the menu that
 * opened it — closing the menu is how a picker makes room for it.
 *
 * The host renders `dialog` wherever it renders anything; it is a portal.
 */
export function useAddModelDialog(
  backend: string,
  enabled: boolean,
  onAdded: (modelId: string, catalog: BackendModels | null) => void,
): { open: (() => void) | null; dialog: ReactNode } {
  const available = useAddModelAvailable(backend, enabled);
  // Fetched as soon as the exit is offered, so a click finds the dialog already here.
  useEffect(() => {
    if (available) void AddModelsDialog.preload().catch(() => undefined);
  }, [available]);
  const [adding, setAdding] = useState<AgentBackend | null>(null);
  return {
    open: available ? () => setAdding(backend as AgentBackend) : null,
    dialog: adding && (
      <Suspense fallback={null}>
      <AddModelsDialog
        open
        backend={adding}
        onClose={() => setAdding(null)}
        onAdded={(ids, agent) => {
          setAdding(null);
          if (ids[0]) onAdded(ids[0], hubCatalogModels(agent, adding));
        }}
      />
      </Suspense>
    ),
  };
}
