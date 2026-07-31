/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useMemo, type ReactNode } from 'react';

import type { InstanceCapabilities, SessionInfo } from './ApiContext';
import {
  DENIED_INSTANCE_CAPABILITIES,
  OWNER_INSTANCE_CAPABILITIES,
} from '../lib/sessionInfo';

interface InstanceAuthorizationValue {
  remote: boolean;
  instanceRole: 'owner' | 'editor' | 'viewer' | null;
  capabilities: InstanceCapabilities;
}

const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceRole: null,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const InstanceAuthorizationProvider = ({
  session,
  children,
}: {
  session: SessionInfo;
  children: ReactNode;
}) => {
  const value = useMemo<InstanceAuthorizationValue>(() => {
    if (!session.remote) {
      return {
        remote: false,
        instanceRole: session.instance_role ?? 'owner',
        capabilities: session.capabilities ?? OWNER_INSTANCE_CAPABILITIES,
      };
    }
    if (!session.authenticated) {
      return { remote: true, instanceRole: null, capabilities: DENIED_INSTANCE_CAPABILITIES };
    }
    return {
      remote: true,
      instanceRole: session.instance_role,
      capabilities: session.capabilities,
    };
  }, [session]);

  return (
    <InstanceAuthorizationContext.Provider value={value}>
      {children}
    </InstanceAuthorizationContext.Provider>
  );
};

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
