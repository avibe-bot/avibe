import { createContext } from 'react';

import type {
  UnsavedChangesActionAuthorization,
  UnsavedChangesRegistrationId,
} from '../lib/unsavedChangesRegistry';

export interface UnsavedChangesContextValue {
  setRegistration: (id: UnsavedChangesRegistrationId, message: string | null) => void;
  /** Confirm before an action mutates data and later navigates. Null means the user canceled. */
  authorizeRouteAction: () => UnsavedChangesActionAuthorization | null;
}

export const UnsavedChangesContext = createContext<UnsavedChangesContextValue | null>(null);
