import { createContext, useContext } from 'react';

import type { InstanceCapabilities } from './ApiContext';
import type { InstanceKind, InstanceRole } from '../lib/sessionInfo';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

export interface InstanceAuthorizationValue {
  remote: boolean;
  instanceKind: InstanceKind | null;
  instanceRole: InstanceRole | null;
  capabilities: InstanceCapabilities;
}

export const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceKind: null,
  instanceRole: null,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
