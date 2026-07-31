import type {
  OrganizationRole,
  ProjectBinding,
  ResourceAccessLevel,
  SyncCounts,
} from './api/types';

export type OrganizationPrincipalKind = ProjectBinding['principal_kind'];
export type ProjectAccessMode = 'inherit' | 'owner_only' | 'restricted';
export type AggregateSyncStatus = 'none' | 'in_sync' | 'applying' | 'offline' | 'error';

type OrganizationPrincipal = {
  kind: OrganizationPrincipalKind;
  value: string;
};

const ORGANIZATION_GROUP_DETAIL_PATH = /^\/admin\/organization\/groups\/[^/]+\/?$/;
const ORGANIZATION_INSTANCE_DETAIL_PATH = /^\/admin\/organization\/instances\/[^/]+\/(?:access|projects)\/?$/;

export function normalizeOrganizationPrincipal(
  kind: OrganizationPrincipalKind,
  value: string,
): string {
  const normalized = value.trim();
  if (kind === 'organization_group') return normalized;
  return (kind === 'email_domain' ? normalized.replace(/^@/, '') : normalized).toLowerCase();
}

export function hasDuplicateOrganizationPrincipals(principals: OrganizationPrincipal[]): boolean {
  const keys = principals.map((principal) => (
    `${principal.kind}:${normalizeOrganizationPrincipal(principal.kind, principal.value)}`
  ));
  return new Set(keys).size !== keys.length;
}

export function hasDuplicateProjectPrincipals(bindings: ProjectBinding[]): boolean {
  return hasDuplicateOrganizationPrincipals(bindings.map((binding) => ({
    kind: binding.principal_kind,
    value: binding.principal_value,
  })));
}

function projectBindingKey(binding: ProjectBinding): string {
  return `${binding.principal_kind}:${normalizeOrganizationPrincipal(
    binding.principal_kind,
    binding.principal_value,
  )}`;
}

export function requiresProjectAccessNarrowingConfirmation(
  currentMode: ProjectAccessMode,
  currentBindings: ProjectBinding[],
  nextMode: ProjectAccessMode,
  nextBindings: ProjectBinding[],
): boolean {
  if (currentMode === 'inherit') return nextMode !== 'inherit';
  if (currentMode === 'owner_only') return false;
  if (nextMode === 'owner_only') return true;
  if (nextMode === 'inherit') return false;

  const nextByPrincipal = new Map(nextBindings.map((binding) => [projectBindingKey(binding), binding]));
  return currentBindings.some((binding) => {
    const nextBinding = nextByPrincipal.get(projectBindingKey(binding));
    return (
      !nextBinding
      || (binding.access_role === 'editor' && nextBinding.access_role === 'viewer')
    );
  });
}

export function requiresResourceAccessNarrowingConfirmation(
  currentLevel: ResourceAccessLevel,
  currentGroupIds: string[],
  nextLevel: ResourceAccessLevel,
  nextGroupIds: string[],
): boolean {
  const audienceBreadth: Record<ResourceAccessLevel, number> = {
    private: 0,
    scope: 1,
    public: 2,
  };
  if (audienceBreadth[nextLevel] < audienceBreadth[currentLevel]) return true;
  if (currentLevel !== 'scope' || nextLevel !== 'scope') return false;

  const nextGroups = new Set(nextGroupIds);
  return currentGroupIds.some((groupId) => !nextGroups.has(groupId));
}

export function aggregateSyncStatus(counts: SyncCounts): AggregateSyncStatus {
  if (counts.error > 0) return 'error';
  if (counts.offline > 0) return 'offline';
  if (counts.applying > 0) return 'applying';
  return counts.active > 0 ? 'in_sync' : 'none';
}

export function isCurrentOrganizationLoad(
  requestedOrganizationId: string,
  selectedOrganizationId: string | null,
  requestGeneration: number,
  currentGeneration: number,
): boolean {
  return (
    requestedOrganizationId === selectedOrganizationId
    && requestGeneration === currentGeneration
  );
}

export function organizationSwitchDestination(pathname: string): string | null {
  if (ORGANIZATION_GROUP_DETAIL_PATH.test(pathname)) {
    return '/admin/organization/groups';
  }
  if (ORGANIZATION_INSTANCE_DETAIL_PATH.test(pathname)) {
    return '/admin/organization/instances';
  }
  return null;
}

export function organizationAuthorizationReturnPath(pathname: string, search: string): string {
  const params = new URLSearchParams(search);
  params.delete('cloud_management_error');
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ''}`;
}

export function requiresMemberRoleDowngradeConfirmation(
  currentRole: OrganizationRole,
  nextRole: OrganizationRole,
): boolean {
  return currentRole === 'admin' && nextRole === 'member';
}
