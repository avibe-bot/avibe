import { createContext, useContext } from 'react';

import type { InstanceCapabilities } from './ApiContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

export interface InstanceAuthorizationValue {
  remote: boolean;
  instanceRole: 'owner' | 'editor' | 'viewer' | null;
  capabilities: InstanceCapabilities;
}

export const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceRole: null,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
