import { useContext, useId, useLayoutEffect } from 'react';

import { UnsavedChangesContext } from './unsavedChangesContext';
import { useRouteSurfaceActive } from '../lib/routeSurfaceActivity';

/** Register one dirty surface with the router-wide blocker. Pass null while the surface is clean. */
export function useUnsavedChanges(message: string | null): void {
  const context = useContext(UnsavedChangesContext);
  if (!context) throw new Error('useUnsavedChanges must be used within an UnsavedChangesProvider');

  const registrationId = useId();
  const routeSurfaceActive = useRouteSurfaceActive();

  // Layout timing closes the gap between a dirty render and the next user navigation. The identity is
  // stable across message changes, while the separate unmount cleanup removes stale registrations.
  useLayoutEffect(() => {
    context.setRegistration(registrationId, routeSurfaceActive ? message : null);
  }, [context, message, registrationId, routeSurfaceActive]);

  useLayoutEffect(
    () => () => {
      context.setRegistration(registrationId, null);
    },
    [context, registrationId],
  );
}
