import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Eye,
  FolderKey,
  Globe2,
  LockKeyhole,
  Mail,
  Pencil,
  Plus,
  Shield,
  Trash2,
  Users,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { NavLink, useParams } from 'react-router-dom';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Select } from '@/components/ui/select';

import { isRevisionConflict, jsonBody, OrganizationApiError } from '../api/client';
import type {
  AccessRole,
  InstanceAccessEntry,
  OrganizationGroup,
  OrganizationInstance,
  OrganizationProject,
  ProjectBinding,
} from '../api/types';
import {
  ConflictBanner,
  EmptyState,
  ErrorBanner,
  ForbiddenState,
  InitialsAvatar,
  LoadingState,
  SearchField,
  SyncBadge,
  TableFrame,
} from '../components';
import { useOrganization } from '../context';
import {
  hasDuplicateProjectPrincipals,
  isCurrentOrganizationLoad,
  normalizeOrganizationPrincipal,
  type ProjectAccessMode,
  requiresProjectAccessNarrowingConfirmation,
} from '../policy';

type PrincipalKind = InstanceAccessEntry['kind'];

function displayPrincipal(kind: PrincipalKind, value: string, groups: OrganizationGroup[]): string {
  if (kind === 'organization_group') {
    return groups.find((group) => group.id === value)?.name ?? value;
  }
  if (kind === 'email_domain') return `@${value.replace(/^@/, '')}`;
  return value;
}

function principalIcon(kind: PrincipalKind) {
  if (kind === 'organization_group') return Users;
  if (kind === 'email_domain') return Globe2;
  return Mail;
}

function projectMode(project: OrganizationProject): ProjectAccessMode {
  if (project.access.mode === 'inherit') return 'inherit';
  return project.access.bindings.length === 0 ? 'owner_only' : 'restricted';
}

function InstanceHeader({ instance }: { instance: OrganizationInstance }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-5">
      <div className="flex min-w-0 items-center gap-3">
        <InitialsAvatar value={instance.slug} tone="mint" className="size-12" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate text-2xl font-semibold">{instance.slug}</h1>
            <Badge variant={instance.paired && instance.status === 'active' ? 'success' : 'secondary'}>
              {t(instance.paired ? 'organization.instances.paired' : 'organization.instances.notPaired')}
              {' · '}
              {t(`organization.instances.status.${instance.status}`)}
            </Badge>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-2 font-mono text-[11px] text-muted">
            <span>{instance.public_hostname}</span>
            <span>·</span>
            <span>{instance.id}</span>
            <span>·</span>
            <span>{instance.owner_is_current_user ? t('organization.instances.youAreOwner') : (instance.owner_email || t('organization.instances.ownerProtected'))}</span>
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2" role="tablist" aria-label={t('organization.instances.tabsLabel')}>
        {instance.can_manage_access ? (
          <NavLink
            to={`/admin/organization/instances/${encodeURIComponent(instance.id)}/access`}
            role="tab"
            className={({ isActive }) => clsx(
              'inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-[12px] font-medium',
              isActive ? 'border-border-strong bg-card text-foreground' : 'border-transparent text-muted hover:text-foreground',
            )}
          >
            <Users className="size-4" />
            {t('organization.instances.accessTab')}
          </NavLink>
        ) : null}
        <NavLink
          to={`/admin/organization/instances/${encodeURIComponent(instance.id)}/projects`}
          role="tab"
          className={({ isActive }) => clsx(
            'inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-[12px] font-medium',
            isActive ? 'border-border-strong bg-card text-foreground' : 'border-transparent text-muted hover:text-foreground',
          )}
        >
          <FolderKey className="size-4" />
          {t('organization.instances.projectsTab')}
        </NavLink>
      </div>
    </div>
  );
}

function useOrganizationInstance() {
  const { instanceId } = useParams();
  const { selectedOrganizationId, request, dataVersion } = useOrganization();
  const [loadedInstance, setLoadedInstance] = useState<{
    organizationId: string;
    instanceId: string;
    instance: OrganizationInstance | null;
  } | null>(null);
  const [errorState, setErrorState] = useState<{
    organizationId: string;
    instanceId: string;
    code: string;
  } | null>(null);
  const loadGeneration = useRef(0);
  const selectedOrganizationIdRef = useRef(selectedOrganizationId);
  const instanceIdRef = useRef(instanceId);
  const currentInstance = loadedInstance?.organizationId === selectedOrganizationId
    && loadedInstance.instanceId === instanceId
    ? loadedInstance
    : null;
  const instance = currentInstance ? currentInstance.instance : undefined;
  const error = errorState?.organizationId === selectedOrganizationId
    && errorState.instanceId === instanceId
    ? errorState.code
    : undefined;

  useLayoutEffect(() => {
    selectedOrganizationIdRef.current = selectedOrganizationId;
    instanceIdRef.current = instanceId;
    loadGeneration.current += 1;
  }, [instanceId, selectedOrganizationId]);

  const load = useCallback(async () => {
    if (!selectedOrganizationId || !instanceId) return;
    const organizationId = selectedOrganizationId;
    const requestedInstanceId = instanceId;
    if (
      selectedOrganizationIdRef.current !== organizationId
      || instanceIdRef.current !== requestedInstanceId
    ) return;
    const generation = ++loadGeneration.current;
    const isCurrent = () => isCurrentOrganizationLoad(
      organizationId,
      selectedOrganizationIdRef.current,
      generation,
      loadGeneration.current,
    );
    setErrorState(null);
    try {
      const result = await request<{ instances: OrganizationInstance[] }>(
        `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/instances`,
      );
      if (!isCurrent()) return;
      setLoadedInstance({
        organizationId,
        instanceId: requestedInstanceId,
        instance: result.instances.find((item) => item.id === requestedInstanceId) ?? null,
      });
    } catch (caught) {
      if (!isCurrent()) return;
      setErrorState({
        organizationId,
        instanceId: requestedInstanceId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  }, [instanceId, request, selectedOrganizationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [dataVersion, load]);
  return { instance, instanceId, error, reload: load };
}

function AccessEntryDialog({
  open,
  entries,
  editingIndex,
  groups,
  instanceId,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  entries: InstanceAccessEntry[];
  editingIndex: number | null;
  groups: OrganizationGroup[];
  instanceId: string;
  onOpenChange: (open: boolean) => void;
  onSaved: (entries: InstanceAccessEntry[]) => void;
}) {
  const { t } = useTranslation();
  const { request } = useOrganization();
  const editing = editingIndex === null ? null : entries[editingIndex];
  const [kind, setKind] = useState<PrincipalKind>('email');
  const [value, setValue] = useState('');
  const [role, setRole] = useState<AccessRole>('viewer');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);

  useEffect(() => {
    if (!open) return;
    setKind(editing?.kind ?? 'email');
    setValue(editing?.value ?? '');
    setRole(editing?.role ?? 'viewer');
    setError(undefined);
    setConfirmNarrowing(false);
  }, [editing, open]);

  const activeGroups = groups.filter((group) => !group.archived_at);
  const resolvedValue = kind === 'organization_group'
    ? (value || activeGroups[0]?.id || '')
    : value;
  const candidate: InstanceAccessEntry = {
    kind,
    value: normalizeOrganizationPrincipal(kind, resolvedValue),
    role,
  };

  const commit = async () => {
    if (!candidate.value) return;
    setSaving(true);
    setError(undefined);
    const next = editingIndex === null
      ? [...entries, candidate]
      : entries.map((entry, index) => (index === editingIndex ? candidate : entry));
    try {
      const result = await request<{ entries: InstanceAccessEntry[] }>(
        `/api/cloud-management/instances/${encodeURIComponent(instanceId)}/authorized-users`,
        { method: 'PUT', body: jsonBody({ entries: next }) },
      );
      onSaved(result.entries);
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
    } finally {
      setSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = () => {
    const narrows = Boolean(editing) && (
      editing?.role === 'editor' && candidate.role === 'viewer'
      || editing?.kind !== candidate.kind
      || normalizeOrganizationPrincipal(editing?.kind ?? 'email', editing?.value ?? '') !== candidate.value
    );
    if (narrows) setConfirmNarrowing(true);
    else void commit();
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t(editing ? 'organization.access.editTitle' : 'organization.access.addTitle')}</DialogTitle>
            <DialogDescription>{t('organization.access.dialogBody')}</DialogDescription>
          </DialogHeader>
          {error ? <ErrorBanner code={error} /> : null}
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="organization-access-kind">{t('organization.fields.principalType')}</Label>
              <Select
                id="organization-access-kind"
                value={kind}
                onChange={(event) => { setKind(event.target.value as PrincipalKind); setValue(''); }}
              >
                <option value="organization_group">{t('organization.principals.organization_group')}</option>
                <option value="email">{t('organization.principals.email')}</option>
                <option value="email_domain">{t('organization.principals.email_domain')}</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="organization-access-value">{t('organization.fields.principal')}</Label>
              {kind === 'organization_group' ? (
                <Select
                  id="organization-access-value"
                  value={resolvedValue}
                  onChange={(event) => setValue(event.target.value)}
                >
                  {activeGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                </Select>
              ) : (
                <Input
                  id="organization-access-value"
                  type={kind === 'email' ? 'email' : 'text'}
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder={t(kind === 'email' ? 'organization.access.emailPlaceholder' : 'organization.access.domainPlaceholder')}
                />
              )}
            </div>
            <div className="space-y-1.5">
              <Label>{t('organization.fields.role')}</Label>
              <SegmentedRadio
                value={role}
                onChange={setRole}
                ariaLabel={t('organization.fields.role')}
                options={[
                  { id: 'viewer', label: t('organization.access.viewer') },
                  { id: 'editor', label: t('organization.access.editor') },
                ]}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
            <Button variant="brand" disabled={saving || !candidate.value} onClick={save}>
              {saving ? t('organization.actions.saving') : t('organization.actions.saveChanges')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('organization.access.narrowTitle')}
        description={t('organization.access.narrowBody')}
        confirmLabel={t('organization.actions.saveChanges')}
        onConfirm={commit}
      />
    </>
  );
}

export function InstanceAccessPage() {
  const { t } = useTranslation();
  const { selectedOrganizationId, request } = useOrganization();
  const { instance, instanceId, error: instanceError, reload } = useOrganizationInstance();
  const [directory, setDirectory] = useState<{
    organizationId: string;
    instanceId: string;
    entries: InstanceAccessEntry[];
    groups: OrganizationGroup[];
  } | null>(null);
  const [errorState, setErrorState] = useState<{
    organizationId: string;
    instanceId: string;
    code: string;
  } | null>(null);
  const [editing, setEditing] = useState<{
    organizationId: string;
    instanceId: string;
    index: number | null;
  } | null>(null);
  const [removing, setRemoving] = useState<{
    organizationId: string;
    instanceId: string;
    index: number;
  } | null>(null);
  const loadGeneration = useRef(0);
  const selectedOrganizationIdRef = useRef(selectedOrganizationId);
  const instanceIdRef = useRef(instanceId);
  const currentDirectory = directory?.organizationId === selectedOrganizationId
    && directory.instanceId === instanceId
    ? directory
    : null;
  const entries = currentDirectory?.entries ?? null;
  const groups = currentDirectory?.groups ?? [];
  const error = errorState?.organizationId === selectedOrganizationId
    && errorState.instanceId === instanceId
    ? errorState.code
    : undefined;
  const currentEditing = editing?.organizationId === selectedOrganizationId
    && editing.instanceId === instanceId
    ? editing
    : null;
  const currentRemoving = removing?.organizationId === selectedOrganizationId
    && removing.instanceId === instanceId
    ? removing
    : null;

  useLayoutEffect(() => {
    selectedOrganizationIdRef.current = selectedOrganizationId;
    instanceIdRef.current = instanceId;
    loadGeneration.current += 1;
  }, [instanceId, selectedOrganizationId]);

  const load = useCallback(async () => {
    if (!instanceId || !selectedOrganizationId || instance?.can_manage_access !== true) return;
    const organizationId = selectedOrganizationId;
    const requestedInstanceId = instanceId;
    if (
      selectedOrganizationIdRef.current !== organizationId
      || instanceIdRef.current !== requestedInstanceId
    ) return;
    const generation = ++loadGeneration.current;
    const isCurrent = () => isCurrentOrganizationLoad(
      organizationId,
      selectedOrganizationIdRef.current,
      generation,
      loadGeneration.current,
    );
    setErrorState(null);
    try {
      const [accessResult, groupResult] = await Promise.all([
        request<{ entries: InstanceAccessEntry[] }>(
          `/api/cloud-management/instances/${encodeURIComponent(requestedInstanceId)}/authorized-users`,
        ),
        request<{ groups: OrganizationGroup[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/groups`,
        ),
      ]);
      if (!isCurrent()) return;
      setDirectory({
        organizationId,
        instanceId: requestedInstanceId,
        entries: accessResult.entries,
        groups: groupResult.groups,
      });
    } catch (caught) {
      if (!isCurrent()) return;
      setErrorState({
        organizationId,
        instanceId: requestedInstanceId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  }, [instance?.can_manage_access, instanceId, request, selectedOrganizationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const remove = async () => {
    if (!currentRemoving || !entries) return;
    const { organizationId, instanceId: requestedInstanceId, index: removingIndex } = currentRemoving;
    const next = entries.filter((_, index) => index !== removingIndex);
    try {
      const result = await request<{ entries: InstanceAccessEntry[] }>(
        `/api/cloud-management/instances/${encodeURIComponent(requestedInstanceId)}/authorized-users`,
        { method: 'PUT', body: jsonBody({ entries: next }) },
      );
      setDirectory((current) => (
        current?.organizationId === organizationId && current.instanceId === requestedInstanceId
          ? { ...current, entries: result.entries }
          : current
      ));
      setRemoving(null);
    } catch (caught) {
      setErrorState({
        organizationId,
        instanceId: requestedInstanceId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  };

  if (instance === undefined) return <LoadingState />;
  if (instanceError) return <ErrorBanner code={instanceError} onRetry={() => void reload()} />;
  if (!instance) return <ForbiddenState />;
  if (!instance.can_manage_access) return <ForbiddenState />;
  if (!entries && !error) return <LoadingState />;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <InstanceHeader instance={instance} />
        <Button
          variant="brand"
          onClick={() => {
            if (selectedOrganizationId && instanceId) {
              setEditing({ organizationId: selectedOrganizationId, instanceId, index: null });
            }
          }}
        >
          <Plus className="size-4" />
          {t('organization.actions.addAccess')}
        </Button>
      </div>
      <div className="flex items-center gap-3 rounded-lg border border-violet/35 bg-violet/10 p-4">
        <Shield className="size-5 shrink-0 text-violet" />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold">{t('organization.access.ownerTitle', { owner: instance.owner_is_current_user ? t('organization.instances.you') : (instance.owner_email || t('organization.instances.ownerProtected')) })}</div>
          <div className="mt-0.5 text-[12px] text-muted">{t('organization.access.ownerBody')}</div>
        </div>
        <Badge variant="secondary">{t('organization.roles.owner')}</Badge>
      </div>
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {(entries ?? []).length === 0 ? (
        <EmptyState title={t('organization.access.emptyTitle')} body={t('organization.access.emptyBody')} />
      ) : (
        <TableFrame>
          <div className="hidden grid-cols-[minmax(220px,1.5fr)_150px_140px_80px] gap-4 border-b border-border px-5 py-3 font-mono text-[10px] font-bold uppercase text-muted md:grid">
            <div>{t('organization.access.columns.principal')}</div>
            <div>{t('organization.access.columns.type')}</div>
            <div>{t('organization.access.columns.role')}</div>
            <div />
          </div>
          {(entries ?? []).map((entry, index) => {
            const Icon = principalIcon(entry.kind);
            return (
              <div key={`${entry.kind}:${entry.value}`} className="grid gap-3 border-b border-border px-4 py-4 last:border-0 md:grid-cols-[minmax(220px,1.5fr)_150px_140px_80px] md:items-center md:px-5">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/10 text-mint"><Icon className="size-4" /></span>
                  <div className="min-w-0">
                    <div className="truncate text-[12px] font-semibold">{displayPrincipal(entry.kind, entry.value, groups)}</div>
                    <div className="text-[10px] text-muted">{t(`organization.principals.${entry.kind}`)}</div>
                  </div>
                </div>
                <Badge variant="secondary">{t(`organization.principals.${entry.kind}`)}</Badge>
                <Badge variant={entry.role === 'editor' ? 'success' : 'secondary'}>
                  {entry.role === 'editor' ? <Pencil className="size-3" /> : <Eye className="size-3" />}
                  {t(`organization.access.${entry.role}`)}
                </Badge>
                <div className="flex justify-end gap-1">
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={t('organization.actions.editAccess')}
                    onClick={() => {
                      if (selectedOrganizationId && instanceId) {
                        setEditing({ organizationId: selectedOrganizationId, instanceId, index });
                      }
                    }}
                  >
                    <Pencil className="size-4" />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={t('organization.actions.removeAccess')}
                    onClick={() => {
                      if (selectedOrganizationId && instanceId) {
                        setRemoving({ organizationId: selectedOrganizationId, instanceId, index });
                      }
                    }}
                  >
                    <Trash2 className="size-4 text-destructive" />
                  </Button>
                </div>
              </div>
            );
          })}
        </TableFrame>
      )}
      <AccessEntryDialog
        open={Boolean(currentEditing)}
        entries={entries ?? []}
        editingIndex={currentEditing?.index ?? null}
        groups={groups}
        instanceId={instance.id}
        onOpenChange={(open) => { if (!open) setEditing(null); }}
        onSaved={(nextEntries) => {
          if (!selectedOrganizationId || !instanceId) return;
          setDirectory((current) => (
            current?.organizationId === selectedOrganizationId && current.instanceId === instanceId
              ? { ...current, entries: nextEntries }
              : current
          ));
        }}
      />
      <ConfirmDialog
        open={Boolean(currentRemoving)}
        onOpenChange={(open) => { if (!open) setRemoving(null); }}
        title={t('organization.access.removeTitle')}
        description={t('organization.access.removeBody', {
          principal: currentRemoving === null ? '' : displayPrincipal(entries?.[currentRemoving.index]?.kind ?? 'email', entries?.[currentRemoving.index]?.value ?? '', groups),
        })}
        destructive
        confirmLabel={t('organization.actions.removeAccess')}
        onConfirm={remove}
      />
    </div>
  );
}

function ProjectAccessDialog({
  project,
  groups,
  instanceId,
  onOpenChange,
  onSaved,
}: {
  project: OrganizationProject | null;
  groups: OrganizationGroup[];
  instanceId: string;
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<OrganizationProject | null>;
}) {
  const { t } = useTranslation();
  const { request } = useOrganization();
  const [mode, setMode] = useState<ProjectAccessMode>('inherit');
  const [bindings, setBindings] = useState<ProjectBinding[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [authoritativeProject, setAuthoritativeProject] = useState<OrganizationProject | null>(null);
  const [revision, setRevision] = useState(0);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);

  useEffect(() => {
    if (!project) return;
    setMode(projectMode(project));
    setBindings(project.access.bindings);
    setRevision(project.access.revision);
    setError(undefined);
    setConflict(false);
    setAuthoritativeProject(null);
    setConfirmNarrowing(false);
  }, [project]);

  const activeGroups = groups.filter((group) => !group.archived_at);
  const addBinding = () => {
    const firstGroup = activeGroups.find((group) => !bindings.some((binding) => (
      binding.principal_kind === 'organization_group' && binding.principal_value === group.id
    )));
    setBindings((current) => [...current, {
      principal_kind: firstGroup ? 'organization_group' : 'email',
      principal_value: firstGroup?.id ?? '',
      access_role: 'viewer',
    }]);
  };
  const updateBinding = (index: number, patch: Partial<ProjectBinding>) => {
    setBindings((current) => current.map((binding, itemIndex) => (
      itemIndex === index ? { ...binding, ...patch } : binding
    )));
  };

  const wireBindings = mode === 'restricted'
    ? bindings.map((binding) => ({
        ...binding,
        principal_value: normalizeOrganizationPrincipal(binding.principal_kind, binding.principal_value),
      }))
    : [];

  const commit = async () => {
    if (!project || (mode === 'restricted' && bindings.length === 0)) return;
    setSaving(true);
    setError(undefined);
    try {
      await request(
        `/api/cloud-management/instances/${encodeURIComponent(instanceId)}/projects/${encodeURIComponent(project.project_id)}/access`,
        {
          method: 'PUT',
          body: jsonBody({
            mode: mode === 'inherit' ? 'inherit' : 'restricted',
            bindings: wireBindings,
            if_match_revision: revision,
          }),
        },
      );
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        const latest = await onSaved();
        setAuthoritativeProject(latest);
        setConflict(true);
      } else {
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    } finally {
      setSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = () => {
    if (!project || (mode === 'restricted' && bindings.length === 0)) return;
    setError(undefined);
    if (hasDuplicateProjectPrincipals(wireBindings)) {
      setError('duplicate_project_access_principal');
      return;
    }
    if (requiresProjectAccessNarrowingConfirmation(
      projectMode(project),
      project.access.bindings,
      mode,
      wireBindings,
    )) {
      setConfirmNarrowing(true);
      return;
    }
    void commit();
  };

  const reloadAuthoritativeProject = () => {
    if (!authoritativeProject) {
      onOpenChange(false);
      return;
    }
    setMode(projectMode(authoritativeProject));
    setBindings(authoritativeProject.access.bindings);
    setRevision(authoritativeProject.access.revision);
    setError(undefined);
    setConflict(false);
    setAuthoritativeProject(null);
  };

  return (
    <>
      <Dialog open={Boolean(project)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('organization.projects.dialogTitle', { name: project?.display_name })}</DialogTitle>
          <DialogDescription>{t('organization.projects.dialogBody')}</DialogDescription>
        </DialogHeader>
        {conflict ? <ConflictBanner onReload={reloadAuthoritativeProject} /> : null}
        {error ? <ErrorBanner code={error} /> : null}
        <div className="space-y-5">
          <div className="space-y-1.5">
            <Label>{t('organization.projects.accessRule')}</Label>
            <SegmentedRadio
              value={mode}
              onChange={setMode}
              ariaLabel={t('organization.projects.accessRule')}
              options={[
                { id: 'inherit', label: t('organization.projects.modes.inherit') },
                { id: 'restricted', label: t('organization.projects.modes.restricted') },
                { id: 'owner_only', label: t('organization.projects.modes.owner_only') },
              ]}
            />
          </div>
          <div className="rounded-lg border border-border bg-foreground/[0.025] p-3 text-[12px] leading-5 text-muted">
            {t(`organization.projects.modeHelp.${mode}`)}
          </div>
          {mode === 'restricted' ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label>{t('organization.projects.bindings')}</Label>
                <Button size="sm" variant="outline" onClick={addBinding}><Plus className="size-4" />{t('organization.actions.addBinding')}</Button>
              </div>
              {bindings.length === 0 ? (
                <div className="rounded-lg border border-gold/35 bg-gold/10 p-3 text-[12px] text-muted">{t('organization.projects.bindingRequired')}</div>
              ) : bindings.map((binding, index) => {
                const archivedGroup = binding.principal_kind === 'organization_group'
                  ? groups.find((group) => group.id === binding.principal_value && group.archived_at)
                  : undefined;
                return (
                  <div key={`${index}:${binding.principal_kind}`} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-[150px_minmax(0,1fr)_110px_36px]">
                    <Select
                      aria-label={t('organization.fields.principalType')}
                      value={binding.principal_kind}
                      onChange={(event) => updateBinding(index, {
                        principal_kind: event.target.value as PrincipalKind,
                        principal_value: event.target.value === 'organization_group' ? (activeGroups[0]?.id ?? '') : '',
                      })}
                    >
                      <option value="organization_group">{t('organization.principals.organization_group')}</option>
                      <option value="email">{t('organization.principals.email')}</option>
                      <option value="email_domain">{t('organization.principals.email_domain')}</option>
                    </Select>
                    {binding.principal_kind === 'organization_group' ? (
                      <Select
                        aria-label={t('organization.fields.principal')}
                        value={binding.principal_value}
                        onChange={(event) => updateBinding(index, { principal_value: event.target.value })}
                      >
                        {archivedGroup ? <option value={archivedGroup.id}>{archivedGroup.name} ({t('organization.groups.archived')})</option> : null}
                        {activeGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                      </Select>
                    ) : (
                      <Input
                        aria-label={t('organization.fields.principal')}
                        value={binding.principal_value}
                        onChange={(event) => updateBinding(index, { principal_value: event.target.value })}
                      />
                    )}
                    <Select
                      aria-label={t('organization.fields.role')}
                      value={binding.access_role}
                      onChange={(event) => updateBinding(index, { access_role: event.target.value as AccessRole })}
                    >
                      <option value="viewer">{t('organization.access.viewer')}</option>
                      <option value="editor">{t('organization.access.editor')}</option>
                    </Select>
                    <Button size="icon" variant="ghost" aria-label={t('organization.actions.removeBinding')} onClick={() => setBindings((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Trash2 className="size-4" /></Button>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button
            variant="brand"
            disabled={saving || conflict || (mode === 'restricted' && (bindings.length === 0 || bindings.some((binding) => !binding.principal_value.trim())))}
            onClick={save}
          >
            {saving ? t('organization.actions.saving') : t('organization.actions.saveChanges')}
          </Button>
        </DialogFooter>
      </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('organization.projects.narrowTitle')}
        description={t('organization.projects.narrowBody')}
        confirmLabel={t('organization.actions.saveChanges')}
        onConfirm={commit}
      />
    </>
  );
}

export function InstanceProjectsPage() {
  const { t } = useTranslation();
  const { selectedOrganizationId, request } = useOrganization();
  const { instance, instanceId, error: instanceError, reload } = useOrganizationInstance();
  const [directory, setDirectory] = useState<{
    organizationId: string;
    instanceId: string;
    projects: OrganizationProject[];
    groups: OrganizationGroup[];
    canManageProjects: boolean;
  } | null>(null);
  const [search, setSearch] = useState('');
  const [errorState, setErrorState] = useState<{
    organizationId: string;
    instanceId: string;
    code: string;
  } | null>(null);
  const [forbiddenState, setForbiddenState] = useState<{
    organizationId: string;
    instanceId: string;
  } | null>(null);
  const [editing, setEditing] = useState<{
    organizationId: string;
    instanceId: string;
    project: OrganizationProject;
  } | null>(null);
  const loadGeneration = useRef(0);
  const selectedOrganizationIdRef = useRef(selectedOrganizationId);
  const instanceIdRef = useRef(instanceId);
  const currentDirectory = directory?.organizationId === selectedOrganizationId
    && directory.instanceId === instanceId
    ? directory
    : null;
  const projects = currentDirectory?.projects ?? null;
  const groups = currentDirectory?.groups ?? [];
  const canManageProjects = currentDirectory?.canManageProjects === true;
  const error = errorState?.organizationId === selectedOrganizationId
    && errorState.instanceId === instanceId
    ? errorState.code
    : undefined;
  const forbidden = forbiddenState?.organizationId === selectedOrganizationId
    && forbiddenState.instanceId === instanceId;
  const currentEditing = editing?.organizationId === selectedOrganizationId
    && editing.instanceId === instanceId
    ? editing.project
    : null;

  useLayoutEffect(() => {
    selectedOrganizationIdRef.current = selectedOrganizationId;
    instanceIdRef.current = instanceId;
    loadGeneration.current += 1;
  }, [instanceId, selectedOrganizationId]);

  const load = useCallback(async (): Promise<OrganizationProject[]> => {
    if (!instanceId || !selectedOrganizationId || instance?.id !== instanceId) return [];
    const organizationId = selectedOrganizationId;
    const requestedInstanceId = instanceId;
    if (
      selectedOrganizationIdRef.current !== organizationId
      || instanceIdRef.current !== requestedInstanceId
    ) return [];
    const generation = ++loadGeneration.current;
    const isCurrent = () => isCurrentOrganizationLoad(
      organizationId,
      selectedOrganizationIdRef.current,
      generation,
      loadGeneration.current,
    );
    setErrorState(null);
    setForbiddenState(null);
    try {
      const result = await request<{
        projects: OrganizationProject[];
        groups: OrganizationGroup[];
        capabilities: { can_manage_projects: boolean };
      }>(
        `/api/cloud-management/instances/${encodeURIComponent(requestedInstanceId)}/projects`,
      );
      const activeProjects = result.projects.filter((project) => project.sync.status !== 'deleted');
      if (!isCurrent()) return [];
      setDirectory({
        organizationId,
        instanceId: requestedInstanceId,
        projects: activeProjects,
        groups: result.groups,
        canManageProjects: result.capabilities.can_manage_projects,
      });
      return activeProjects;
    } catch (caught) {
      if (!isCurrent()) return [];
      if (caught instanceof OrganizationApiError && (caught.status === 403 || caught.status === 404)) {
        setForbiddenState({ organizationId, instanceId: requestedInstanceId });
      } else {
        setErrorState({
          organizationId,
          instanceId: requestedInstanceId,
          code: caught instanceof OrganizationApiError ? caught.code : 'generic',
        });
      }
      return [];
    }
  }, [instance?.id, instanceId, request, selectedOrganizationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  const filtered = useMemo(() => (projects ?? []).filter((project) => (
    `${project.display_name} ${project.project_id}`.toLowerCase().includes(search.trim().toLowerCase())
  )), [projects, search]);

  if (instance === undefined) return <LoadingState />;
  if (instanceError) return <ErrorBanner code={instanceError} onRetry={() => void reload()} />;
  if (!instance) return <ForbiddenState />;
  if (!projects && !error && !forbidden) return <LoadingState />;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
        <InstanceHeader instance={instance} />
        <SearchField value={search} onChange={setSearch} placeholder={t('organization.projects.searchPlaceholder')} />
      </div>
      <div className="flex items-center gap-2 rounded-lg border border-border px-4 py-3 text-[12px] text-muted">
        <Shield className="size-4 shrink-0" />
        {t('organization.projects.safeDescriptorNotice')}
      </div>
      {forbidden ? <ForbiddenState /> : null}
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {!forbidden && filtered.length === 0 ? (
        <EmptyState title={t('organization.projects.emptyTitle')} body={t('organization.projects.emptyBody')} />
      ) : !forbidden ? (
        <TableFrame>
          <div className="hidden grid-cols-[minmax(220px,1.25fr)_160px_minmax(180px,1fr)_130px_90px] gap-4 border-b border-border px-5 py-3 font-mono text-[10px] font-bold uppercase text-muted md:grid">
            <div>{t('organization.projects.columns.project')}</div>
            <div>{t('organization.projects.columns.rule')}</div>
            <div>{t('organization.projects.columns.bindings')}</div>
            <div>{t('organization.projects.columns.sync')}</div>
            <div />
          </div>
          {filtered.map((project) => {
            const mode = projectMode(project);
            return (
              <div key={project.project_id} className="grid gap-3 border-b border-border px-4 py-4 last:border-0 md:grid-cols-[minmax(220px,1.25fr)_160px_minmax(180px,1fr)_130px_90px] md:items-center md:px-5">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/10 text-mint"><FolderKey className="size-4" /></span>
                  <div className="min-w-0">
                    <div className="truncate text-[12px] font-semibold">{project.display_name}</div>
                    <div className="truncate font-mono text-[10px] text-muted">{project.project_id}</div>
                  </div>
                </div>
                <Badge variant={mode === 'owner_only' ? 'warning' : mode === 'restricted' ? 'info' : 'secondary'}>
                  {mode === 'owner_only' ? <Shield className="size-3" /> : mode === 'restricted' ? <LockKeyhole className="size-3" /> : <Users className="size-3" />}
                  {t(`organization.projects.modes.${mode}`)}
                </Badge>
                <div className="truncate text-[12px] text-muted">
                  {mode === 'inherit'
                    ? t('organization.projects.usesAvibeRole')
                    : mode === 'owner_only'
                      ? t('organization.projects.ownerOnlyAudience')
                      : t('organization.projects.bindingCount', { count: project.access.bindings.length })}
                </div>
                <SyncBadge status={project.sync.status} />
                {canManageProjects ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      if (selectedOrganizationId && instanceId) {
                        setEditing({ organizationId: selectedOrganizationId, instanceId, project });
                      }
                    }}
                  >
                    {t('organization.actions.manage')}
                  </Button>
                ) : <span />}
              </div>
            );
          })}
        </TableFrame>
      ) : null}
      <ProjectAccessDialog
        project={currentEditing}
        groups={groups}
        instanceId={instance.id}
        onOpenChange={(open) => { if (!open) setEditing(null); }}
        onSaved={async () => {
          const latestProjects = await load();
          return latestProjects.find((project) => project.project_id === currentEditing?.project_id) ?? null;
        }}
      />
    </div>
  );
}
