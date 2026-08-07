export type OrganizationRole = 'owner' | 'admin' | 'member';
export type MemberStatus = 'active' | 'invited' | 'removed';
export type AccessRole = 'viewer' | 'editor';
export type SyncStatus = 'none' | 'in_sync' | 'applying' | 'pending' | 'offline' | 'error' | 'deleted';
export type GroupColor = 'mint' | 'cyan' | 'blue' | 'violet' | 'rose' | 'gold';

export type Organization = {
  id: string;
  name: string;
  slug: string;
  plan: string;
  created_at: string;
  updated_at: string;
  role: OrganizationRole;
  member_id: string;
};

export type GroupSummary = {
  id: string;
  name: string;
  archived_at: string | null;
};

export type OrganizationDetail = {
  organization: Omit<Organization, 'role' | 'member_id'>;
  membership: {
    id: string;
    role: OrganizationRole;
    status: MemberStatus;
    member_revision: number;
    groups: GroupSummary[];
  };
  capabilities: { can_manage_organization: boolean };
  counts: {
    members: number;
    groups: number;
    instances: number;
    domains?: number;
    invitedMembers?: number;
    removedMembers?: number;
    archivedGroups?: number;
  };
};

export type OrganizationMember = {
  id: string;
  user_id: string | null;
  email: string;
  role: OrganizationRole;
  status: MemberStatus;
  member_revision: number;
  invited_by_user_id: string | null;
  created_at: string;
  updated_at: string;
  groups: GroupSummary[];
};

export type GroupUsage = { instances: number; projects: number; resources: number };
export type GroupReferences = {
  instances: Array<{ id: string; slug: string }>;
  projects: Array<{ instanceId: string; projectId: string; displayName: string }>;
  resources: Array<{
    instanceId: string;
    resourceKind: string;
    resourceId: string;
    displayName: string;
  }>;
};

export type OrganizationGroup = {
  id: string;
  name: string;
  normalized_name: string;
  description: string | null;
  color: GroupColor | null;
  group_revision: number;
  member_count?: number;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  is_member: boolean;
  can_manage: boolean;
  usage?: GroupUsage;
  members?: OrganizationMember[];
  references?: GroupReferences;
};

export type SyncCounts = {
  active: number;
  error: number;
  offline: number;
  applying: number;
  in_sync: number;
};

export type OrganizationInstance = {
  id: string;
  slug: string;
  organization_id: string;
  owner_user_id: string | null;
  owner_email: string | null;
  owner_is_current_user: boolean;
  can_manage_access: boolean;
  public_hostname: string;
  primary_url: string;
  managed_url: string;
  status: 'pending' | 'active' | 'disabled' | 'deleted';
  paired: boolean;
  access_entries_count: number;
  policy_sync: {
    status: Exclude<SyncStatus, 'pending' | 'deleted'>;
    projects: SyncCounts;
    resources: SyncCounts;
  };
  hostnames: Array<{
    id: string;
    hostname: string;
    kind?: string;
    status?: string;
    primary?: boolean;
  }>;
};

export type InstanceAccessEntry = {
  id?: string;
  instanceId?: string;
  kind: 'email' | 'email_domain' | 'organization_group';
  value: string;
  role: AccessRole;
  createdAt?: string;
};

export type ProjectBinding = {
  principal_kind: 'email' | 'email_domain' | 'organization_group';
  principal_value: string;
  access_role: AccessRole;
};

export type OrganizationProject = {
  project_id: string;
  organization_id: string | null;
  display_name: string;
  access: {
    mode: 'inherit' | 'restricted';
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

export type ResourceKind = 'agent' | 'vault_secret' | 'skill' | 'show_page';
export type ResourceAccessLevel = 'public' | 'scope' | 'private';

export type OrganizationResource = {
  instance_id: string;
  resource_kind: ResourceKind;
  resource_id: string;
  display_name: string;
  owner_user_id: string | null;
  access: {
    access_level: ResourceAccessLevel;
    group_ids: string[];
    revision: number;
  } | null;
  sync: {
    status: SyncStatus;
    desired_acl_revision: number;
    applied_acl_revision: number;
    last_synced_at: string | null;
    last_sync_error?: string;
  };
};

export type CloudManagementSession =
  | { connected: false; state: 'cloud_not_connected' }
  | {
      connected: false;
      state: 'authorization_required';
      can_silent_reauthorize: boolean;
    }
  | {
      connected: false;
      state: 'subject_mismatch';
      error: 'cloud_management_subject_mismatch';
    }
  | {
      connected: true;
      state: 'connected';
      user: { subject: string; email: string };
      expires_in: number;
    };

export type CloudErrorPayload = {
  error?: string;
  retryable?: boolean;
  current_revision?: number;
  can_silent_reauthorize?: boolean;
};
