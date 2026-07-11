import { useContext } from 'react';

import { UnsavedChangesContext } from './unsavedChangesContext';
import type { UnsavedChangesActionAuthorization } from '../lib/unsavedChangesRegistry';

/** Confirm before a side-effecting action whose successful result navigates to another route. */
export function useUnsavedChangesActionGuard(): () => UnsavedChangesActionAuthorization | null {
  const context = useContext(UnsavedChangesContext);
  if (!context) throw new Error('useUnsavedChangesActionGuard must be used within an UnsavedChangesProvider');
  return context.authorizeRouteAction;
}
