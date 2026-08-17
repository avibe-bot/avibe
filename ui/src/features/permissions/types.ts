export type AccessRole = 'viewer' | 'editor';
export type PrincipalKind = 'email' | 'email_domain' | 'organization_group';
export type SyncStatus = 'none' | 'in_sync' | 'applying' | 'pending' | 'offline' | 'error' | 'deleted';
export type ResourceAccessLevel = 'public' | 'scope' | 'private';

export type AccessEntry = {
  kind: PrincipalKind;
  value: string;
  role: AccessRole;
};

export type DirectoryMember = {
  id: string;
  email: string;
  organization_role: 'owner' | 'admin' | 'member';
  group_ids: string[];
};

export type DirectoryGroup = {
  id: string;
  name: string;
  archived_at: string | null;
};

export type ProjectBinding = {
  principal_kind: PrincipalKind;
  principal_value: string;
  access_role: AccessRole;
};

export type PermissionProject = {
  project_id: string;
  organization_id: string | null;
  display_name: string;
  access: {
    mode: 'inherit' | 'restricted' | 'owner_only';
    revision: number;
    bindings: ProjectBinding[];
  };
  sync: {
    status: SyncStatus;
    desired_access_revision: number;
    applied_access_revision: number;
    last_synced_at: string | null;
    last_sync_error?: string;
  };
};

export type SyncCounts = {
  active: number;
  error: number;
  offline: number;
  applying: number;
  in_sync: number;
};

export type PermissionsProjection = {
  schema_version: 1;
  instance: {
    id: string;
    access_mode: 'allowlist' | 'public';
    permission_authority: 'instance' | 'cloud';
    local_mutation_allowed: boolean;
    authorization_revision: number;
  };
  capabilities: Array<'instance.permissions.read' | 'instance.permissions.mutate'>;
  access: {
    owner: { email: string | null; role: 'owner' };
    entries: AccessEntry[];
  };
  directory: {
    members: DirectoryMember[];
    groups: DirectoryGroup[];
  };
  projects: PermissionProject[];
  policy_sync: {
    status: 'none' | 'error' | 'offline' | 'applying' | 'in_sync';
    projects: SyncCounts;
    resources: SyncCounts;
  };
};

export type PermissionsResponse = {
  ok: true;
  source: 'live' | 'cache';
  offline: boolean;
  cached_at: number | null;
  projection: PermissionsProjection;
};

export type AuthorizedUsersWriteResponse = {
  ok: true;
  entries: AccessEntry[];
  authorization_revision: number;
};

export type ProjectAccessWriteResponse = {
  ok: true;
  project: PermissionProject;
  authorization_revision: number;
};

export type PermissionsErrorPayload = {
  error?: string;
  current_revision?: number;
  offline?: boolean;
};
