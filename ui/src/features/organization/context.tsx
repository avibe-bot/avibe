import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useLocation } from 'react-router-dom';

import { OrganizationApiError, organizationRequest } from './api/client';
import type {
  CloudManagementSession,
  Organization,
  OrganizationDetail,
} from './api/types';

type GateState =
  | 'loading'
  | 'cloud_not_connected'
  | 'authorization_required'
  | 'reauthorizing'
  | 'subject_mismatch'
  | 'unreachable'
  | 'revoked'
  | 'connected'
  | 'error';

type OrganizationContextValue = {
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

const OrganizationContext = createContext<OrganizationContextValue | null>(null);
const SELECTED_ORG_KEY = 'avibe.organization.selected';

function callbackError(): string | null {
  const params = new URLSearchParams(window.location.search);
  const error = params.get('cloud_management_error');
  if (error) {
    params.delete('cloud_management_error');
    const query = params.toString();
    window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
  }
  return error;
}

export function OrganizationProvider({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const [gate, setGate] = useState<GateState>('loading');
  const [session, setSession] = useState<Extract<CloudManagementSession, { connected: true }> | null>(null);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<string | null>(null);
  const [detail, setDetail] = useState<OrganizationDetail | null>(null);
  const [dataVersion, setDataVersion] = useState(0);
  const silentAttempted = useRef(false);

  const startAuthorization = useCallback(async (mode: 'interactive' | 'silent') => {
    setGate('reauthorizing');
    try {
      const result = await organizationRequest<{ authorize_url: string }>(
        '/api/cloud-management/session/start',
        {
          method: 'POST',
          body: JSON.stringify({ mode, next: `${location.pathname}${location.search}` }),
        },
      );
      window.location.assign(result.authorize_url);
    } catch (error) {
      if (error instanceof OrganizationApiError) {
        if (error.code === 'cloud_management_subject_mismatch') setGate('subject_mismatch');
        else if (error.status === 409) setGate('cloud_not_connected');
        else if (error.status === 401) setGate('authorization_required');
        else setGate('unreachable');
      } else {
        setGate('unreachable');
      }
    }
  }, [location.pathname, location.search]);

  const loadOrganization = useCallback(async (organizationId: string) => {
    const result = await organizationRequest<OrganizationDetail>(
      `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}`,
    );
    setSelectedOrganizationId(organizationId);
    setDetail(result);
    sessionStorage.setItem(SELECTED_ORG_KEY, organizationId);
  }, []);

  const loadOrganizations = useCallback(async () => {
    const result = await organizationRequest<{ organizations: Organization[] }>(
      '/api/cloud-management/organizations',
    );
    setOrganizations(result.organizations);
    if (result.organizations.length === 0) {
      setSelectedOrganizationId(null);
      setDetail(null);
      setGate('connected');
      return;
    }
    const remembered = sessionStorage.getItem(SELECTED_ORG_KEY);
    const selected = result.organizations.some((item) => item.id === remembered)
      ? remembered!
      : result.organizations[0].id;
    await loadOrganization(selected);
    setGate('connected');
  }, [loadOrganization]);

  const probe = useCallback(async () => {
    const returnedError = callbackError();
    if (returnedError === 'cloud_management_subject_mismatch') {
      setGate('subject_mismatch');
      return;
    }
    setGate('loading');
    try {
      const result = await organizationRequest<CloudManagementSession>(
        '/api/cloud-management/session',
      );
      if (!result.connected) {
        setSession(null);
        setOrganizations([]);
        setDetail(null);
        if (result.state === 'cloud_not_connected') {
          setGate('cloud_not_connected');
        } else if (result.state === 'subject_mismatch') {
          setGate('subject_mismatch');
        } else if (
          result.can_silent_reauthorize
          && !silentAttempted.current
          && returnedError === null
        ) {
          silentAttempted.current = true;
          await startAuthorization('silent');
        } else {
          setGate('authorization_required');
        }
        return;
      }
      setSession(result);
      silentAttempted.current = false;
      await loadOrganizations();
    } catch (error) {
      if (error instanceof OrganizationApiError) {
        if (error.code === 'cloud_management_subject_mismatch') setGate('subject_mismatch');
        else if (error.status === 401) setGate(returnedError ? 'authorization_required' : 'revoked');
        else if (error.status === 409) setGate('cloud_not_connected');
        else setGate('unreachable');
      } else {
        setGate('unreachable');
      }
    }
  }, [loadOrganizations, startAuthorization]);

  useEffect(() => {
    void probe();
  }, [probe]);

  const request = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    try {
      return await organizationRequest<T>(path, init);
    } catch (error) {
      if (error instanceof OrganizationApiError) {
        if (error.code === 'cloud_management_subject_mismatch') {
          setGate('subject_mismatch');
        } else if (error.status === 401) {
          if (error.canSilentReauthorize && !silentAttempted.current) {
            silentAttempted.current = true;
            await startAuthorization('silent');
          } else {
            setGate('revoked');
          }
        } else if (error.status >= 500 && error.retryable) {
          setGate('unreachable');
        }
      }
      throw error;
    }
  }, [startAuthorization]);

  const signOut = useCallback(async () => {
    try {
      await organizationRequest('/api/cloud-management/session', { method: 'DELETE' });
    } finally {
      setSession(null);
      setOrganizations([]);
      setDetail(null);
      setGate('authorization_required');
      silentAttempted.current = true;
    }
  }, []);

  const refreshOrganization = useCallback(async () => {
    if (!selectedOrganizationId) return;
    await loadOrganization(selectedOrganizationId);
  }, [loadOrganization, selectedOrganizationId]);

  const value = useMemo<OrganizationContextValue>(() => ({
    gate,
    session,
    organizations,
    selectedOrganizationId,
    detail,
    dataVersion,
    signIn: () => startAuthorization('interactive'),
    signOut,
    retry: probe,
    selectOrganization: loadOrganization,
    refreshOrganization,
    invalidate: () => setDataVersion((version) => version + 1),
    request,
  }), [
    dataVersion,
    detail,
    gate,
    loadOrganization,
    organizations,
    probe,
    refreshOrganization,
    request,
    selectedOrganizationId,
    session,
    signOut,
    startAuthorization,
  ]);

  return <OrganizationContext.Provider value={value}>{children}</OrganizationContext.Provider>;
}

export function useOrganization(): OrganizationContextValue {
  const value = useContext(OrganizationContext);
  if (!value) throw new Error('useOrganization must be used within OrganizationProvider');
  return value;
}
