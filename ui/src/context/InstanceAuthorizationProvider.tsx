import { useEffect, useMemo, type ReactNode } from 'react';

import type { SessionInfo } from './ApiContext';
import {
  InstanceAuthorizationContext,
  type InstanceAuthorizationValue,
} from './InstanceAuthorizationContext';
import {
  DENIED_INSTANCE_CAPABILITIES,
  OWNER_INSTANCE_CAPABILITIES,
} from '../lib/sessionInfo';
import { setLocalSnapshotPrincipal } from '../lib/localSnapshots';

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
        instanceKind: session.instance_kind,
        instanceRole: session.instance_role ?? 'owner',
        capabilities: session.capabilities ?? OWNER_INSTANCE_CAPABILITIES,
        readerPrincipal: session.author_id ?? null,
      };
    }
    if (!session.authenticated || session.authorization_state !== 'current') {
      return {
        remote: true,
        instanceKind: null,
        instanceRole: null,
        capabilities: DENIED_INSTANCE_CAPABILITIES,
      };
    }
    return {
      remote: true,
      instanceKind: session.instance_kind,
      instanceRole: session.instance_role,
      capabilities: session.capabilities,
      readerPrincipal: session.author_id ?? null,
    };
  }, [session]);

  // Local snapshots are stored per reader, so they follow the same answer: known
  // while this boundary is mounted with a principal, unknown otherwise.
  const principal = value.readerPrincipal ?? null;
  useEffect(() => {
    setLocalSnapshotPrincipal(principal);
    return () => setLocalSnapshotPrincipal(null);
  }, [principal]);

  return (
    <InstanceAuthorizationContext.Provider value={value}>
      {children}
    </InstanceAuthorizationContext.Provider>
  );
};
