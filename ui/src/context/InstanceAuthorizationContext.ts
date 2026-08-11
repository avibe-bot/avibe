import { createContext, useContext } from 'react';

import type { InstanceCapabilities } from './ApiContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

export interface InstanceAuthorizationValue {
  remote: boolean;
  instanceRole: 'owner' | 'editor' | 'viewer' | null;
  /** Temporary rollout policy, derived from signed active-Organization-member claims. */
  hasTemporaryUnrestrictedOrgAppAccess?: boolean;
  capabilities: InstanceCapabilities;
}

export const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceRole: null,
  hasTemporaryUnrestrictedOrgAppAccess: false,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
