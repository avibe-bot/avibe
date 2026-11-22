import { lazyPage } from '@/lib/lazyPage';
import { LazyDialog } from '../ui/lazy-dialog';
import type { NewProjectDialogProps } from './NewProjectDialog';

// The new-project dialog and the folder browser it opens with are off the first screen
// (docs/plans/web-ui-first-load.md): nothing there draws them. The idle prefetch fetches them
// first of all (src/routePages.ts), and a trigger that is hovered or focused asks for them too,
// so the press finds them loaded and the dialog opens at once. Only a press in the first moment
// after the page loads, before either, waits for the fetch.
const Loaded = lazyPage(() => import('./NewProjectDialog').then((module) => module.NewProjectDialog));

function Dialog(props: NewProjectDialogProps) {
  return (
    <LazyDialog onFailed={props.onClose}>
      <Loaded {...props} />
    </LazyDialog>
  );
}

/**
 * `NewProjectDialog` for a caller on the first screen; a failed load closes it and says to reload.
 * `preload` starts the fetch ahead of the press (a trigger's pointer-enter or focus, the idle prefetch).
 */
export const LazyNewProjectDialog = Object.assign(Dialog, { preload: Loaded.preload });
