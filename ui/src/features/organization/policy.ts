import type { ProjectBinding, SyncCounts } from './api/types';

export type OrganizationPrincipalKind = ProjectBinding['principal_kind'];
export type AggregateSyncStatus = 'none' | 'in_sync' | 'applying' | 'offline' | 'error';

export function normalizeOrganizationPrincipal(
  kind: OrganizationPrincipalKind,
  value: string,
): string {
  const normalized = value.trim();
  if (kind === 'organization_group') return normalized;
  return (kind === 'email_domain' ? normalized.replace(/^@/, '') : normalized).toLowerCase();
}

export function hasDuplicateProjectPrincipals(bindings: ProjectBinding[]): boolean {
  const keys = bindings.map((binding) => (
    `${binding.principal_kind}:${normalizeOrganizationPrincipal(binding.principal_kind, binding.principal_value)}`
  ));
  return new Set(keys).size !== keys.length;
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
