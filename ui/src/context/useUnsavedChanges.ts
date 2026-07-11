import { useContext, useId, useLayoutEffect } from 'react';

import { UnsavedChangesContext } from './unsavedChangesContext';

/** Register one dirty surface with the router-wide blocker. Pass null while the surface is clean. */
export function useUnsavedChanges(message: string | null): void {
  const context = useContext(UnsavedChangesContext);
  if (!context) throw new Error('useUnsavedChanges must be used within an UnsavedChangesProvider');

  const registrationId = useId();

  // Layout timing closes the gap between a dirty render and the next user navigation. The identity is
  // stable across message changes, while the separate unmount cleanup removes stale registrations.
  useLayoutEffect(() => {
    context.setRegistration(registrationId, message);
  }, [context, message, registrationId]);

  useLayoutEffect(
    () => () => {
      context.setRegistration(registrationId, null);
    },
    [context, registrationId],
  );
}
