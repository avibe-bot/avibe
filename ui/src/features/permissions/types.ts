export type AccessRole = 'viewer' | 'editor' | 'member';
export type PrincipalKind = 'email' | 'email_domain' | 'organization_group';
export type ProjectSyncStatus = 'in_sync' | 'pending' | 'offline' | 'error' | 'deleted';
export type ResourceAccessLevel = 'public' | 'scope' | 'private';
export type ResourceSyncStatus = 'in_sync' | 'pending' | 'offline' | 'error';

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

export type ProjectAccessRole = 'viewer' | 'editor';

export type ProjectBinding = {
  principal_kind: PrincipalKind;
  principal_value: string;
  access_role: ProjectAccessRole;
};

export type PermissionProject = {
  project_id: string;
  organization_id: string | null;
  display_name: string;
  access: {
    mode: 'inherit' | 'restricted';
    revision: number;
    bindings: ProjectBinding[];
  };
  sync: {
    status: ProjectSyncStatus;
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
    name?: string;
    public_url?: string;
    organization?: { id: string; name: string } | null;
    access_mode: 'allowlist' | 'public';
    permission_authority: 'instance' | 'cloud';
    local_mutation_allowed: boolean;
    authorization_revision: number;
  };
  capabilities: string[];
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
  instance_id: string;
  entries: AccessEntry[];
  authorization_revision: number;
};

export type ProjectAccessWriteResponse = {
  ok: true;
  instance_id: string;
  project: PermissionProject;
  authorization_revision: number;
};

export type PermissionResource = {
  instance_id: string;
  resource_kind: 'agent' | 'vault_secret' | 'skill' | 'show_page';
  resource_id: string;
  display_name: string;
  owner_user_id: string | null;
  access: {
    access_level: ResourceAccessLevel;
    group_ids: string[];
    revision: number;
  };
  sync: {
    status: ResourceSyncStatus;
    desired_acl_revision: number;
    applied_acl_revision: number;
    last_synced_at: string | null;
    last_sync_error?: string;
  };
};

export type ResourceAccessResponse = {
  resource: PermissionResource;
};

export type ResourceAccessWriteResponse = {
  ok: true;
  resource: PermissionResource;
};

export type PermissionsErrorPayload = {
  error?: string;
  current_revision?: number;
  offline?: boolean;
};
