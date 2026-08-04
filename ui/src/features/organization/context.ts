import { createContext, useContext } from 'react';

import type {
  CloudManagementSession,
  Organization,
  OrganizationDetail,
} from './api/types';

export type GateState =
  | 'loading'
  | 'cloud_not_connected'
  | 'authorization_required'
  | 'reauthorizing'
  | 'subject_mismatch'
  | 'unreachable'
  | 'revoked'
  | 'connected'
  | 'error';

export type OrganizationContextValue = {
  gate: GateState;
  session: Extract<CloudManagementSession, { connected: true }> | null;
  organizations: Organization[];
  selectedOrganizationId: string | null;
  detail: OrganizationDetail | null;
  dataVersion: number;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  retry: () => Promise<void>;
  selectOrganization: (organizationId: string) => Promise<void>;
  refreshOrganization: () => Promise<void>;
  invalidate: () => void;
  request: <T>(path: string, init?: RequestInit) => Promise<T>;
};

export const OrganizationContext = createContext<OrganizationContextValue | null>(null);

export function useOrganization(): OrganizationContextValue {
  const value = useContext(OrganizationContext);
  if (!value) throw new Error('useOrganization must be used within OrganizationProvider');
  return value;
}
