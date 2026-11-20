import { lazyPage } from '@/lib/lazyPage';
import { LazyDialog } from '../ui/lazy-dialog';
import type { NewProjectDialogProps } from './NewProjectDialog';

// The new-project dialog and the folder browser it opens with load when the dialog is first
// asked for (docs/plans/web-ui-first-load.md): nothing on the first screen draws them. The idle
// prefetch of the Projects page, which imports the dialog itself, brings the same code in
// ahead of the first press.
const Loaded = lazyPage(() => import('./NewProjectDialog').then((module) => module.NewProjectDialog));

function Dialog(props: NewProjectDialogProps) {
  return (
    <LazyDialog onFailed={props.onClose}>
      <Loaded {...props} />
    </LazyDialog>
  );
}

/** `NewProjectDialog` for a caller on the first screen; a failed load closes it and says to reload. */
export const LazyNewProjectDialog = Object.assign(Dialog, { preload: Loaded.preload });
