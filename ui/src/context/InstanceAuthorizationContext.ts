import { createContext, useContext } from 'react';

import type { InstanceCapabilities } from './ApiContext';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

export interface InstanceAuthorizationValue {
  remote: boolean;
  instanceRole: 'owner' | 'editor' | 'viewer' | null;
  /** Temporary unrestricted runtime policy, derived from signed active-Organization claims. */
  hasTemporaryUnrestrictedOrgAccess?: boolean;
  /** Compatibility with the previous Apps-only field. */
  hasTemporaryUnrestrictedOrgAppAccess?: boolean;
  capabilities: InstanceCapabilities;
}

/**
 * Temporary runtime rollout gate. This is deliberately separate from the
 * capability projection: active Organization members receive the explicitly
 * opened runtime surfaces without being presented as local owners.
 */
export const canUseTemporaryOrgRuntime = (
  remote: boolean,
  hasTemporaryUnrestrictedOrgAccess: boolean | undefined,
): boolean => !remote || hasTemporaryUnrestrictedOrgAccess === true;

/** Shared name for non-control-plane runtime surfaces. */
export const canUseRuntimeSurfaces = canUseTemporaryOrgRuntime;

export const canUseAppsSurface = canUseTemporaryOrgRuntime;

export const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceRole: null,
  hasTemporaryUnrestrictedOrgAccess: false,
  hasTemporaryUnrestrictedOrgAppAccess: false,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
