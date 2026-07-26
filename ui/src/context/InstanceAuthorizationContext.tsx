/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useMemo, type ReactNode } from 'react';

import type { InstanceCapabilities, SessionInfo } from './ApiContext';

const DENIED_CAPABILITIES: InstanceCapabilities = {
  is_instance_owner: false,
  can_read_instance: false,
  can_chat: false,
  can_manage_projects: false,
  can_manage_agents: false,
  can_manage_instance: false,
  can_use_terminal_files: false,
  can_use_terminal: false,
  can_use_files: false,
  can_use_system: false,
};

const OWNER_CAPABILITIES: InstanceCapabilities = Object.fromEntries(
  Object.keys(DENIED_CAPABILITIES).map((key) => [key, true]),
) as InstanceCapabilities;

interface InstanceAuthorizationValue {
  remote: boolean;
  instanceRole: 'owner' | 'editor' | 'viewer' | null;
  capabilities: InstanceCapabilities;
}

const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceRole: null,
  capabilities: DENIED_CAPABILITIES,
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
        capabilities: session.capabilities ?? OWNER_CAPABILITIES,
      };
    }
    if (!session.authenticated) {
      return { remote: true, instanceRole: null, capabilities: DENIED_CAPABILITIES };
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
