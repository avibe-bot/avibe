import type {
  AccessEntry,
  PermissionProject,
  PrincipalKind,
  ProjectBinding,
} from './types';

export type ProjectAccessMode = 'inherit' | 'owner_only' | 'restricted';

export function normalizePrincipal(kind: PrincipalKind, value: string): string {
  const normalized = value.trim();
  if (kind === 'organization_group') return normalized;
  return (kind === 'email_domain' ? normalized.replace(/^@/, '') : normalized).toLowerCase();
}

export function hasDuplicateAccessEntries(entries: AccessEntry[]): boolean {
  const keys = entries.map((entry) => `${entry.kind}:${normalizePrincipal(entry.kind, entry.value)}`);
  return new Set(keys).size !== keys.length;
}

export function hasDuplicateProjectBindings(bindings: ProjectBinding[]): boolean {
  const keys = bindings.map((binding) => (
    `${binding.principal_kind}:${normalizePrincipal(binding.principal_kind, binding.principal_value)}`
  ));
  return new Set(keys).size !== keys.length;
}

export function projectMode(project: PermissionProject): ProjectAccessMode {
  return project.access.mode;
}

const bindingKey = (binding: ProjectBinding): string => (
  `${binding.principal_kind}:${normalizePrincipal(binding.principal_kind, binding.principal_value)}`
);

export function requiresProjectNarrowing(
  currentMode: ProjectAccessMode,
  currentBindings: ProjectBinding[],
  nextMode: ProjectAccessMode,
  nextBindings: ProjectBinding[],
): boolean {
  if (currentMode === 'inherit') return nextMode !== 'inherit';
  if (currentMode === 'owner_only') return false;
  if (nextMode === 'owner_only') return true;
  if (nextMode === 'inherit') return false;
  const nextByPrincipal = new Map(nextBindings.map((binding) => [bindingKey(binding), binding]));
  return currentBindings.some((binding) => {
    const next = nextByPrincipal.get(bindingKey(binding));
    return !next || (binding.access_role === 'editor' && next.access_role === 'viewer');
  });
}
