import { apiFetch } from '@/lib/apiFetch';

import type {
  AccessEntry,
  AuthorizedUsersWriteResponse,
  PermissionProject,
  PermissionsErrorPayload,
  PermissionsResponse,
  ProjectAccessWriteResponse,
  ProjectBinding,
} from './types';

export class PermissionsApiError extends Error {
  status: number;
  code: string;
  currentRevision?: number;
  offline: boolean;

  constructor(status: number, payload: PermissionsErrorPayload = {}) {
    const code = payload.error || 'permissions_unavailable';
    super(code);
    this.name = 'PermissionsApiError';
    this.status = status;
    this.code = code;
    this.currentRevision = payload.current_revision;
    this.offline = payload.offline === true;
  }
}

async function permissionsRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!(path === '/api/permissions' || path.startsWith('/api/permissions/'))) {
    throw new Error('Permissions requests must use the same-origin current-instance API');
  }
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await apiFetch(path, { ...init, headers, credentials: 'same-origin' });
  let payload: unknown = {};
  try {
    payload = await response.json();
  } catch {
    throw new PermissionsApiError(response.ok ? 502 : response.status);
  }
  if (!response.ok) {
    throw new PermissionsApiError(response.status, payload as PermissionsErrorPayload);
  }
  return payload as T;
}

export const getPermissions = (): Promise<PermissionsResponse> => (
  permissionsRequest('/api/permissions')
);

export const replaceAuthorizedUsers = (
  entries: AccessEntry[],
  revision: number,
  instanceId: string,
): Promise<AuthorizedUsersWriteResponse> => permissionsRequest(
  '/api/permissions/authorized-users',
  {
    method: 'PUT',
    body: JSON.stringify({
      entries,
      if_match_revision: revision,
      if_match_instance_id: instanceId,
    }),
  },
);

export const updateProjectAccess = (
  project: Pick<PermissionProject, 'project_id'>,
  mode: PermissionProject['access']['mode'],
  bindings: ProjectBinding[],
  revision: number,
  instanceId: string,
): Promise<ProjectAccessWriteResponse> => permissionsRequest(
  `/api/permissions/projects/${encodeURIComponent(project.project_id)}/access`,
  {
    method: 'PUT',
    body: JSON.stringify({
      mode,
      bindings,
      if_match_revision: revision,
      if_match_instance_id: instanceId,
    }),
  },
);

export const isRevisionConflict = (error: unknown): error is PermissionsApiError => (
  error instanceof PermissionsApiError
  && error.status === 409
  && error.code === 'permission_revision_conflict'
);
