import type { DependencyItem } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set([
  'askill',
  'avault',
  'model-hub-engine',
  'show-runtime',
  'tmux',
]);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status' | 'action_class'>,
): boolean => (
  dependency.status !== 'unsupported'
  && dependency.status !== 'not_required'
  && dependency.action_class !== 'none'
  && dependency.action_class !== 'operator_only'
  && INSTALLABLE_DEPENDENCIES.has(dependency.id)
);
