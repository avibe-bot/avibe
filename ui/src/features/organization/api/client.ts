import { apiFetch } from '@/lib/apiFetch';

import type { CloudErrorPayload } from './types';

export class OrganizationApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  currentRevision?: number;
  canSilentReauthorize: boolean;

  constructor(status: number, payload: CloudErrorPayload = {}) {
    const code = payload.error || 'cloud_management_unavailable';
    super(code);
    this.name = 'OrganizationApiError';
    this.status = status;
    this.code = code;
    this.retryable = payload.retryable === true;
    this.currentRevision = payload.current_revision;
    this.canSilentReauthorize = payload.can_silent_reauthorize === true;
  }
}

export async function organizationRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!path.startsWith('/api/cloud-management/')) {
    throw new Error('Organization API path must use the local management proxy');
  }
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await apiFetch(path, {
    ...init,
    headers,
    credentials: 'same-origin',
  });
  let payload: unknown = {};
  try {
    payload = await response.json();
  } catch {
    throw new OrganizationApiError(response.ok ? 502 : response.status);
  }
  if (!response.ok) {
    throw new OrganizationApiError(response.status, payload as CloudErrorPayload);
  }
  return payload as T;
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export function isRevisionConflict(error: unknown): error is OrganizationApiError {
  return error instanceof OrganizationApiError
    && error.status === 409
    && (error.code === 'organization_member_conflict'
      || error.code === 'organization_group_conflict'
      || error.code === 'resource_sync_conflict');
}
