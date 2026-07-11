import { createContext } from 'react';

import type { UnsavedChangesRegistrationId } from '../lib/unsavedChangesRegistry';

export interface UnsavedChangesContextValue {
  setRegistration: (id: UnsavedChangesRegistrationId, message: string | null) => void;
}

export const UnsavedChangesContext = createContext<UnsavedChangesContextValue | null>(null);
