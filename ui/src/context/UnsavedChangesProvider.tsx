import { useBlocker } from 'react-router-dom';
import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';

import {
  UnsavedChangesContext,
  type UnsavedChangesContextValue,
} from './unsavedChangesContext';
import {
  createUnsavedChangesNavigationGate,
  getUnsavedChangesMessage,
  setUnsavedChangesRegistration,
  type UnsavedChangesActionAuthorization,
  type UnsavedChangesRegistrationId,
  type UnsavedChangesRegistry,
} from '../lib/unsavedChangesRegistry';

export const UnsavedChangesProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const registryRef = useRef<UnsavedChangesRegistry>(new Map());
  const handledLocationRef = useRef<object | null>(null);
  const navigationGateRef = useRef(createUnsavedChangesNavigationGate());

  const setRegistration = useCallback((id: UnsavedChangesRegistrationId, message: string | null) => {
    setUnsavedChangesRegistration(registryRef.current, id, message);
  }, []);

  const authorizeRouteAction = useCallback((): UnsavedChangesActionAuthorization | null => {
    const message = getUnsavedChangesMessage(registryRef.current);
    return navigationGateRef.current.authorize(message, (confirmation) => window.confirm(confirmation));
  }, []);

  // React Router 7.17 supports one blocker per router. Keep that blocker mounted here and let the
  // registry decide synchronously whether the current transition needs confirmation.
  const shouldBlock = useCallback(() => {
    const hasUnsavedChanges = getUnsavedChangesMessage(registryRef.current) !== null;
    return navigationGateRef.current.shouldBlock(hasUnsavedChanges);
  }, []);
  const blocker = useBlocker(shouldBlock);

  useEffect(() => {
    if (blocker.state === 'unblocked') {
      handledLocationRef.current = null;
      return;
    }
    if (blocker.state !== 'blocked' || handledLocationRef.current === blocker.location) return;

    handledLocationRef.current = blocker.location;
    const message = getUnsavedChangesMessage(registryRef.current);
    if (message === null || window.confirm(message)) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  const value = useMemo<UnsavedChangesContextValue>(
    () => ({ setRegistration, authorizeRouteAction }),
    [setRegistration, authorizeRouteAction],
  );
  return <UnsavedChangesContext.Provider value={value}>{children}</UnsavedChangesContext.Provider>;
};
