import { createContext, useContext } from 'react';

import type { InstanceCapabilities } from './ApiContext';
import type { InstanceKind, InstanceRole } from '../lib/sessionInfo';
import { DENIED_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

export interface InstanceAuthorizationValue {
  remote: boolean;
  instanceKind: InstanceKind | null;
  instanceRole: InstanceRole | null;
  capabilities: InstanceCapabilities;
  // The principal this reader's Chat rows are written as, straight from
  // ``/api/session`` -- ``local`` for a direct loopback browser, ``remote:<sub>``
  // for a Cloud session, absent for anything else (a LAN setup host, say).
  // Compared against a row's ``author_id`` to recognise the reader's own rows.
  readerPrincipal?: string | null;
}

export const InstanceAuthorizationContext = createContext<InstanceAuthorizationValue>({
  remote: false,
  instanceKind: null,
  instanceRole: null,
  capabilities: DENIED_INSTANCE_CAPABILITIES,
});

export const useInstanceAuthorization = () => useContext(InstanceAuthorizationContext);
