import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  Cloud,
  CloudOff,
  ExternalLink,
  Eye,
  FolderKey,
  Globe2,
  Loader2,
  LockKeyhole,
  Mail,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  ShieldX,
  Trash2,
  Users,
  WifiOff,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
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
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';

import {
  getPermissions,
  isRevisionConflict,
  PermissionsApiError,
  replaceAuthorizedUsers,
  updateProjectAccess,
} from './api';
import {
  hasDuplicateAccessEntries,
  hasDuplicateProjectBindings,
  normalizePrincipal,
  projectMode,
  requiresAccessNarrowing,
  requiresProjectNarrowing,
  type ProjectAccessMode,
} from './policy';
import type {
  AccessEntry,
  AccessRole,
  DirectoryGroup,
  PermissionProject,
  PermissionsResponse,
  PrincipalKind,
  ProjectBinding,
  ProjectSyncStatus,
} from './types';

type PageState =
  | { kind: 'loading' }
  | { kind: 'ready'; response: PermissionsResponse }
  | { kind: 'denied'; code: string }
  | { kind: 'unavailable'; code: string; offline: boolean };

type PageLoadResult = {
  state: Exclude<PageState, { kind: 'loading' }>;
  response: PermissionsResponse | null;
  canPreserveReady: boolean;
};

type AuthoritativeRefreshResult =
  | { kind: 'ready'; response: PermissionsResponse }
  | { kind: 'offline' }
  | { kind: 'failed' };

type AuthoritativeRefresh = () => Promise<AuthoritativeRefreshResult>;

const POLICY_REFRESH_INTERVAL_MS = 2_000;
const POLICY_REFRESH_MAX_ATTEMPTS = 30;
const RETRYABLE_PERMISSIONS_STATUSES = new Set([408, 425, 429]);

const isTransientPermissionsFailure = (error: PermissionsApiError | null): boolean => (
  error === null
  || error.offline
  || RETRYABLE_PERMISSIONS_STATUSES.has(error.status)
  || error.status >= 500
);

async function fetchPermissionsPage(): Promise<PageLoadResult> {
  try {
    const response = await getPermissions();
    return { state: { kind: 'ready', response }, response, canPreserveReady: false };
  } catch (caught) {
    const error = caught instanceof PermissionsApiError ? caught : null;
    if (error?.code === 'instance_access_forbidden') {
      return {
        state: { kind: 'denied', code: error.code },
        response: null,
        canPreserveReady: false,
      };
    }
    return {
      state: {
        kind: 'unavailable',
        code: error?.code ?? 'permissions_unavailable',
        offline: error?.offline === true,
      },
      response: null,
      canPreserveReady: isTransientPermissionsFailure(error),
    };
  }
}

const projectionIsApplying = (response: PermissionsResponse): boolean => (
  response.projection.policy_sync.status === 'applying'
  || response.projection.projects.some((project) => (
    project.sync.status === 'pending'
  ))
);

const shouldRefreshPolicy = (response: PermissionsResponse): boolean => (
  response.source === 'live' && !response.offline && projectionIsApplying(response)
);

const policyRefreshSignature = (response: PermissionsResponse): string => JSON.stringify([
  response.projection.instance.id,
  response.projection.instance.authorization_revision,
  response.projection.policy_sync.status,
  response.projection.projects.map((project) => [
    project.project_id,
    project.sync.status,
    project.sync.desired_access_revision,
    project.sync.applied_access_revision,
  ]),
]);

const mutationErrorCode = (caught: unknown): string => (
  caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable'
);

const revisionMonotonicResponse = (
  current: PermissionsResponse | null,
  candidate: PermissionsResponse,
): PermissionsResponse => {
  if (
    current !== null
    && current.projection.instance.id === candidate.projection.instance.id
    && current.projection.instance.authorization_revision
      > candidate.projection.instance.authorization_revision
  ) {
    if (
      current.source === candidate.source
      && current.offline === candidate.offline
      && current.cached_at === candidate.cached_at
    ) {
      return current;
    }
    return {
      ...current,
      source: candidate.source,
      offline: candidate.offline,
      cached_at: candidate.cached_at,
    };
  }
  return candidate;
};

const mergeProjectMutationAcknowledgement = (
  current: PermissionsResponse,
  result: Awaited<ReturnType<typeof updateProjectAccess>>,
): PermissionsResponse => {
  const currentRevision = current.projection.instance.authorization_revision;
  const acknowledgedProject = result.project;
  const currentProject = current.projection.projects.find(
    (project) => project.project_id === acknowledgedProject.project_id,
  );
  let project = acknowledgedProject;
  if (currentProject) {
    if (currentProject.access.revision > acknowledgedProject.access.revision) {
      project = currentProject;
    } else if (
      currentProject.access.revision === acknowledgedProject.access.revision
      && currentRevision > result.authorization_revision
    ) {
      // A later acknowledgement may already have observed this policy's sync
      // progress. Keep that progress while applying the committed policy.
      project = { ...acknowledgedProject, sync: currentProject.sync };
    }
  }
  const projects = current.projection.projects.map((candidate) => (
    candidate.project_id === project.project_id ? project : candidate
  ));
  if (!currentProject) projects.push(project);
  return {
    ...current,
    projection: {
      ...current.projection,
      instance: {
        ...current.projection.instance,
        authorization_revision: Math.max(currentRevision, result.authorization_revision),
      },
      projects,
    },
  };
};

const principalIcon = (kind: PrincipalKind) => {
  if (kind === 'organization_group') return Users;
  if (kind === 'email_domain') return Globe2;
  return Mail;
};

const displayPrincipal = (kind: PrincipalKind, value: string, groups: DirectoryGroup[]): string => {
  if (kind === 'organization_group') {
    return groups.find((group) => group.id === value)?.name ?? value;
  }
  if (kind === 'email_domain') return `@${value.replace(/^@/, '')}`;
  return value;
};

const publicUrlLabel = (publicUrl: string | undefined): string | null => {
  if (!publicUrl) return null;
  try {
    return new URL(publicUrl).host;
  } catch {
    return null;
  }
};

const accessEntryKey = (entry: AccessEntry): string => (
  `${entry.kind}:${normalizePrincipal(entry.kind, entry.value)}`
);

function SyncBadge({ status }: { status: ProjectSyncStatus }) {
  const { t } = useTranslation();
  const normalized = status === 'pending' ? 'applying' : status;
  const Icon = normalized === 'in_sync'
    ? CheckCircle2
    : normalized === 'applying'
      ? Loader2
      : normalized === 'offline'
        ? WifiOff
        : normalized === 'error'
          ? AlertTriangle
          : CloudOff;
  const variant = normalized === 'in_sync'
    ? 'success'
    : normalized === 'applying'
      ? 'warning'
      : normalized === 'error'
        ? 'destructive'
        : 'secondary';
  return (
    <Badge variant={variant}>
      <Icon className={clsx('size-3', normalized === 'applying' && 'animate-spin')} />
      {t(`permissions.sync.${normalized}`)}
    </Badge>
  );
}

function Notice({
  tone,
  icon: Icon,
  title,
  body,
  action,
}: {
  tone: 'neutral' | 'warning' | 'danger' | 'info';
  icon: typeof ShieldCheck;
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      className={clsx(
        'flex flex-col gap-3 rounded-lg border px-4 py-3 sm:flex-row sm:items-center',
        tone === 'warning' && 'border-gold/40 bg-gold/10',
        tone === 'danger' && 'border-destructive/35 bg-destructive/10',
        tone === 'info' && 'border-cyan/35 bg-cyan/10',
        tone === 'neutral' && 'border-border bg-foreground/[0.025]',
      )}
      role={tone === 'danger' ? 'alert' : 'status'}
    >
      <Icon className={clsx(
        'size-4 shrink-0',
        tone === 'warning' && 'text-gold-ink',
        tone === 'danger' && 'text-destructive-ink',
        tone === 'info' && 'text-cyan-ink',
        tone === 'neutral' && 'text-muted',
      )} />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold">{title}</div>
        <div className="mt-0.5 text-[12px] leading-5 text-muted">{body}</div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

function EmptyState({ icon: Icon, title, body }: {
  icon: typeof ShieldCheck;
  title: string;
  body: string;
}) {
  return (
    <div className="flex min-h-56 flex-col items-center justify-center rounded-lg border border-dashed border-border px-6 text-center">
      <Icon className="mb-3 size-7 text-muted" />
      <h2 className="text-[15px] font-semibold">{title}</h2>
      <p className="mt-1 max-w-md text-[13px] leading-5 text-muted">{body}</p>
    </div>
  );
}

function AccessEntryDialog({
  open,
  editingKey,
  response,
  editable,
  onOpenChange,
  onRefresh,
  onSaved,
}: {
  open: boolean;
  editingKey: string | null;
  response: PermissionsResponse;
  editable: boolean;
  onOpenChange: (open: boolean) => void;
  onRefresh: AuthoritativeRefresh;
  onSaved: (
    instanceId: string,
    result: Awaited<ReturnType<typeof replaceAuthorizedUsers>>,
  ) => void;
}) {
  const { t } = useTranslation();
  const initialized = useRef(false);
  const entries = response.projection.access.entries;
  const editing = editingKey === null
    ? null
    : entries.find((entry) => accessEntryKey(entry) === editingKey) ?? null;
  const groups = response.projection.directory.groups;
  const [kind, setKind] = useState<PrincipalKind>('email');
  const [value, setValue] = useState('');
  const [role, setRole] = useState<AccessRole>('viewer');
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const originalEntry = useRef<AccessEntry | null>(null);
  const baselineEntry = useRef<AccessEntry | null>(null);
  const expectedInstanceId = useRef('');

  const updateSaving = (next: boolean): void => {
    savingRef.current = next;
    setSaving(next);
  };
  const handleOpenChange = (nextOpen: boolean): void => {
    if (!nextOpen && savingRef.current) return;
    onOpenChange(nextOpen);
  };
  const closeAfterRequest = (): void => {
    updateSaving(false);
    handleOpenChange(false);
  };
  const handleConfirmOpenChange = (nextOpen: boolean): void => {
    if (!nextOpen && savingRef.current) return;
    setConfirmNarrowing(nextOpen);
  };

  useEffect(() => {
    if (open && !initialized.current) {
      originalEntry.current = editing ?? null;
      baselineEntry.current = editing ?? null;
      expectedInstanceId.current = response.projection.instance.id;
      setKind(editing?.kind ?? 'email');
      setValue(editing?.value ?? '');
      setRole(editing?.role ?? 'viewer');
      setRevision(response.projection.instance.authorization_revision);
      setError(undefined);
      setConflict(false);
      setRefreshRequired(false);
      setConfirmNarrowing(false);
    }
    initialized.current = open;
  }, [editing, open, response]);

  const activeGroups = groups.filter((group) => !group.archived_at);
  const archivedGroup = kind === 'organization_group'
    ? groups.find((group) => group.id === value && group.archived_at)
    : undefined;
  const resolvedValue = kind === 'organization_group'
    ? (value || activeGroups[0]?.id || '')
    : normalizePrincipal(kind, value);
  const candidate: AccessEntry = { kind, value: resolvedValue, role };
  const originalKey = originalEntry.current ? accessEntryKey(originalEntry.current) : null;
  const candidateKey = accessEntryKey(candidate);
  const originalIndex = originalKey === null
    ? -1
    : entries.findIndex((entry) => accessEntryKey(entry) === originalKey);
  const candidateIndex = entries.findIndex((entry) => accessEntryKey(entry) === candidateKey);
  const nextEntries = (() => {
    const filtered = entries.filter((entry) => {
      const key = accessEntryKey(entry);
      return key !== originalKey && key !== candidateKey;
    });
    if (candidateIndex >= 0) {
      filtered.splice(Math.min(candidateIndex, filtered.length), 0, candidate);
    } else if (originalIndex >= 0) {
      filtered.splice(Math.min(originalIndex, filtered.length), 0, candidate);
    } else {
      filtered.push(candidate);
    }
    return filtered;
  })();

  const refreshConflict = async (): Promise<boolean> => {
    const latest = await onRefresh();
    setConflict(true);
    if (latest.kind !== 'ready') {
      setRefreshRequired(true);
      setError('permissions_refresh_failed');
      return false;
    }
    if (latest.response.projection.instance.id !== expectedInstanceId.current) {
      setRefreshRequired(true);
      setError('permissions_pairing_changed');
      return false;
    }
    const latestEntries = latest.response.projection.access.entries;
    const latestOriginal = originalKey === null
      ? null
      : latestEntries.find((entry) => accessEntryKey(entry) === originalKey) ?? null;
    const latestCandidate = latestEntries.find(
      (entry) => accessEntryKey(entry) === candidateKey,
    );
    if (
      latestCandidate?.role === candidate.role
      && (originalKey === null || originalKey === candidateKey || latestOriginal === null)
    ) {
      setConflict(false);
      setRefreshRequired(false);
      setError(undefined);
      closeAfterRequest();
      return true;
    }
    if (originalKey === null && latestCandidate) {
      originalEntry.current = latestCandidate;
      baselineEntry.current = latestCandidate;
    } else {
      baselineEntry.current = latestOriginal;
    }
    setRevision(latest.response.projection.instance.authorization_revision);
    setRefreshRequired(false);
    setError(undefined);
    return true;
  };

  const validateDraft = (): boolean => {
    if (!candidate.value || hasDuplicateAccessEntries(nextEntries)) {
      setError(candidate.value ? 'duplicate_access_principal' : 'invalid_request');
      return false;
    }
    return true;
  };

  const commit = async () => {
    if (!editable || !validateDraft()) return;
    updateSaving(true);
    setError(undefined);
    try {
      const requestInstanceId = expectedInstanceId.current;
      const result = await replaceAuthorizedUsers(
        nextEntries,
        revision,
        requestInstanceId,
      );
      onSaved(requestInstanceId, result);
      closeAfterRequest();
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        await refreshConflict();
      } else {
        setError(mutationErrorCode(caught));
      }
    } finally {
      updateSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = async () => {
    if (!editable) return;
    if (refreshRequired) {
      updateSaving(true);
      try {
        await refreshConflict();
      } finally {
        updateSaving(false);
      }
      return;
    }
    if (!validateDraft()) return;
    if (requiresAccessNarrowing(baselineEntry.current, candidate)) {
      setError(undefined);
      setConfirmNarrowing(true);
      return;
    }
    await commit();
  };

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent>
        <DialogHeader>
          <DialogTitle>{t(originalKey === null ? 'permissions.access.addTitle' : 'permissions.access.editTitle')}</DialogTitle>
          <DialogDescription>{t('permissions.access.dialogBody')}</DialogDescription>
        </DialogHeader>
        {conflict ? (
          <Notice
            tone="warning"
            icon={AlertTriangle}
            title={t('permissions.states.conflictTitle')}
            body={t(refreshRequired
              ? error === 'permissions_pairing_changed'
                ? 'permissions.states.pairingChangedBody'
                : 'permissions.states.conflictRefreshBody'
              : 'permissions.states.conflictBody')}
          />
        ) : null}
        {error ? (
          <Notice tone="danger" icon={ShieldX} title={t('permissions.states.errorTitle')} body={t(`permissions.errors.${error}`, { defaultValue: t('permissions.errors.generic') })} />
        ) : null}
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="permissions-access-kind">{t('permissions.fields.principalType')}</Label>
            <Select
              id="permissions-access-kind"
              value={kind}
              onChange={(event) => { setKind(event.target.value as PrincipalKind); setValue(''); }}
            >
              {activeGroups.length > 0 || archivedGroup ? <option value="organization_group">{t('permissions.principals.organization_group')}</option> : null}
              <option value="email">{t('permissions.principals.email')}</option>
              <option value="email_domain">{t('permissions.principals.email_domain')}</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="permissions-access-value">{t('permissions.fields.principal')}</Label>
            {kind === 'organization_group' ? (
              <Select id="permissions-access-value" value={resolvedValue} onChange={(event) => setValue(event.target.value)}>
                {archivedGroup ? <option value={archivedGroup.id}>{archivedGroup.name} ({t('common.archived')})</option> : null}
                {activeGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
              </Select>
            ) : (
              <Input
                id="permissions-access-value"
                type={kind === 'email' ? 'email' : 'text'}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder={t(kind === 'email' ? 'permissions.access.emailPlaceholder' : 'permissions.access.domainPlaceholder')}
              />
            )}
          </div>
          <div className="space-y-1.5">
            <Label>{t('permissions.fields.role')}</Label>
            <SegmentedRadio
              value={role}
              onChange={setRole}
              ariaLabel={t('permissions.fields.role')}
              options={[
                { id: 'viewer', label: t('permissions.roles.viewer') },
                { id: 'editor', label: t('permissions.roles.editor') },
              ]}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={saving}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={!editable || saving || !candidate.value} onClick={() => void save()}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : null}
            {t(conflict ? 'permissions.actions.retrySave' : 'permissions.actions.save')}
          </Button>
        </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={handleConfirmOpenChange}
        title={t('permissions.access.narrowTitle')}
        description={t('permissions.access.narrowBody')}
        confirmLabel={t('permissions.actions.save')}
        confirmDisabled={!editable || saving}
        onConfirm={commit}
      />
    </>
  );
}

function ProjectAccessDialog({
  project,
  instanceId,
  groups,
  editable,
  onOpenChange,
  onRefresh,
  onSaved,
}: {
  project: PermissionProject | null;
  instanceId: string;
  groups: DirectoryGroup[];
  editable: boolean;
  onOpenChange: (open: boolean) => void;
  onRefresh: AuthoritativeRefresh;
  onSaved: (
    instanceId: string,
    result: Awaited<ReturnType<typeof updateProjectAccess>>,
  ) => void;
}) {
  const { t } = useTranslation();
  const initialized = useRef(false);
  const [mode, setMode] = useState<ProjectAccessMode>('inherit');
  const [bindings, setBindings] = useState<ProjectBinding[]>([]);
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const baseline = useRef<PermissionProject | null>(null);
  const expectedInstanceId = useRef('');
  const open = Boolean(project);

  const updateSaving = (next: boolean): void => {
    savingRef.current = next;
    setSaving(next);
  };
  const handleOpenChange = (nextOpen: boolean): void => {
    if (!nextOpen && savingRef.current) return;
    onOpenChange(nextOpen);
  };
  const closeAfterRequest = (): void => {
    updateSaving(false);
    handleOpenChange(false);
  };
  const handleConfirmOpenChange = (nextOpen: boolean): void => {
    if (!nextOpen && savingRef.current) return;
    setConfirmNarrowing(nextOpen);
  };

  useEffect(() => {
    if (project && !initialized.current) {
      baseline.current = project;
      expectedInstanceId.current = instanceId;
      setMode(projectMode(project));
      setBindings(project.access.bindings);
      setRevision(project.access.revision);
      setError(undefined);
      setConflict(false);
      setRefreshRequired(false);
      setConfirmNarrowing(false);
    }
    initialized.current = open;
  }, [instanceId, open, project]);

  const activeGroups = groups.filter((group) => !group.archived_at);
  const wireBindings = mode === 'restricted'
    ? bindings.map((binding) => ({
        ...binding,
        principal_value: normalizePrincipal(binding.principal_kind, binding.principal_value),
      }))
    : [];
  const wireMode: PermissionProject['access']['mode'] = mode === 'inherit'
    ? 'inherit'
    : 'restricted';
  const invalid = mode === 'restricted' && (
    wireBindings.length === 0 || wireBindings.some((binding) => !binding.principal_value)
  );

  const refreshConflict = async (): Promise<boolean> => {
    if (!project) return false;
    const latest = await onRefresh();
    setConflict(true);
    if (latest.kind !== 'ready') {
      setRefreshRequired(true);
      setError('permissions_refresh_failed');
      return false;
    }
    if (latest.response.projection.instance.id !== expectedInstanceId.current) {
      setRefreshRequired(true);
      setError('permissions_pairing_changed');
      return false;
    }
    const authoritative = latest.response.projection.projects.find(
      (item) => item.project_id === project.project_id && item.sync.status !== 'deleted',
    );
    if (!authoritative) {
      closeAfterRequest();
      return false;
    }
    baseline.current = authoritative;
    setRevision(authoritative.access.revision);
    setRefreshRequired(false);
    setError(undefined);
    return true;
  };

  const commit = async () => {
    if (!editable || !project || invalid) return;
    if (hasDuplicateProjectBindings(wireBindings)) {
      setError('duplicate_project_access_principal');
      return;
    }
    updateSaving(true);
    setError(undefined);
    try {
      const requestInstanceId = expectedInstanceId.current;
      const result = await updateProjectAccess(
        project,
        wireMode,
        wireBindings,
        revision,
        requestInstanceId,
      );
      onSaved(requestInstanceId, result);
      closeAfterRequest();
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        await refreshConflict();
      } else {
        setError(mutationErrorCode(caught));
      }
    } finally {
      updateSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = async () => {
    if (!editable || !project || invalid) return;
    if (refreshRequired) {
      updateSaving(true);
      try {
        await refreshConflict();
      } finally {
        updateSaving(false);
      }
      return;
    }
    const current = baseline.current ?? project;
    if (requiresProjectNarrowing(projectMode(current), current.access.bindings, mode, wireBindings)) {
      setConfirmNarrowing(true);
      return;
    }
    await commit();
  };

  const addBinding = () => {
    const group = activeGroups.find((candidate) => !bindings.some((binding) => (
      binding.principal_kind === 'organization_group' && binding.principal_value === candidate.id
    )));
    setBindings((current) => [...current, {
      principal_kind: group ? 'organization_group' : 'email',
      principal_value: group?.id ?? '',
      access_role: 'viewer',
    }]);
  };

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('permissions.projects.dialogTitle', { name: project?.display_name })}</DialogTitle>
            <DialogDescription>{t('permissions.projects.dialogBody')}</DialogDescription>
          </DialogHeader>
          {conflict ? <Notice tone="warning" icon={AlertTriangle} title={t('permissions.states.conflictTitle')} body={t(refreshRequired ? error === 'permissions_pairing_changed' ? 'permissions.states.pairingChangedBody' : 'permissions.states.conflictRefreshBody' : 'permissions.states.conflictBody')} /> : null}
          {error ? <Notice tone="danger" icon={ShieldX} title={t('permissions.states.errorTitle')} body={t(`permissions.errors.${error}`, { defaultValue: t('permissions.errors.generic') })} /> : null}
          <div className="space-y-5">
            <div className="space-y-1.5">
              <Label>{t('permissions.projects.accessRule')}</Label>
              <SegmentedRadio
                value={mode}
                onChange={setMode}
                ariaLabel={t('permissions.projects.accessRule')}
                options={[
                  { id: 'inherit', label: t('permissions.projects.modes.inherit') },
                  { id: 'restricted', label: t('permissions.projects.modes.restricted') },
                  { id: 'owner_only', label: t('permissions.projects.modes.owner_only') },
                ]}
              />
            </div>
            <p className="rounded-lg border border-border bg-foreground/[0.025] p-3 text-[12px] leading-5 text-muted">
              {t(`permissions.projects.modeHelp.${mode}`)}
            </p>
            {mode === 'restricted' ? (
              <div className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <Label>{t('permissions.projects.bindings')}</Label>
                  <Button size="sm" variant="outline" onClick={addBinding}><Plus className="size-4" />{t('permissions.actions.addBinding')}</Button>
                </div>
                {bindings.map((binding, index) => {
                  const archivedGroup = binding.principal_kind === 'organization_group'
                    ? groups.find((group) => (
                        group.id === binding.principal_value && group.archived_at
                      ))
                    : undefined;
                  return (
                    <div key={`${index}:${binding.principal_kind}`} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-[150px_minmax(0,1fr)_110px_36px]">
                      <Select
                        aria-label={t('permissions.fields.principalType')}
                        value={binding.principal_kind}
                        onChange={(event) => setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? {
                          ...item,
                          principal_kind: event.target.value as PrincipalKind,
                          principal_value: event.target.value === 'organization_group' ? (activeGroups[0]?.id ?? '') : '',
                        } : item))}
                      >
                        {activeGroups.length > 0 || archivedGroup ? <option value="organization_group">{t('permissions.principals.organization_group')}</option> : null}
                        <option value="email">{t('permissions.principals.email')}</option>
                        <option value="email_domain">{t('permissions.principals.email_domain')}</option>
                      </Select>
                      {binding.principal_kind === 'organization_group' ? (
                        <Select
                          aria-label={t('permissions.fields.principal')}
                          value={binding.principal_value}
                          onChange={(event) => setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, principal_value: event.target.value } : item))}
                        >
                          {archivedGroup ? <option value={archivedGroup.id}>{archivedGroup.name} ({t('common.archived')})</option> : null}
                          {activeGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                        </Select>
                      ) : (
                        <Input
                          aria-label={t('permissions.fields.principal')}
                          value={binding.principal_value}
                          onChange={(event) => setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, principal_value: event.target.value } : item))}
                        />
                      )}
                      <Select
                        aria-label={t('permissions.fields.role')}
                        value={binding.access_role}
                        onChange={(event) => setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, access_role: event.target.value as AccessRole } : item))}
                      >
                        <option value="viewer">{t('permissions.roles.viewer')}</option>
                        <option value="editor">{t('permissions.roles.editor')}</option>
                      </Select>
                      <Button size="icon" variant="ghost" aria-label={t('permissions.actions.removeBinding')} onClick={() => setBindings((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Trash2 className="size-4" /></Button>
                    </div>
                  );
                })}
                {bindings.length === 0 ? <p className="text-[12px] text-gold-ink">{t('permissions.projects.bindingRequired')}</p> : null}
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={saving}>{t('common.cancel')}</Button>
            <Button variant="brand" disabled={!editable || saving || invalid} onClick={() => void save()}>
              {saving ? <Loader2 className="size-4 animate-spin" /> : null}
              {t(conflict ? 'permissions.actions.retrySave' : 'permissions.actions.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={handleConfirmOpenChange}
        title={t('permissions.projects.narrowTitle')}
        description={t('permissions.projects.narrowBody')}
        confirmLabel={t('permissions.actions.save')}
        confirmDisabled={!editable || saving}
        onConfirm={commit}
      />
    </>
  );
}

export function PermissionsPage() {
  const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [tab, setTab] = useState<'access' | 'projects'>('access');
  const [editingAccess, setEditingAccess] = useState<string | null | undefined>(undefined);
  const [editingProject, setEditingProject] = useState<PermissionProject | null>(null);
  const [removingAccess, setRemovingAccess] = useState<string | null>(null);
  const [removalInstanceId, setRemovalInstanceId] = useState<string | null>(null);
  const [removalConflict, setRemovalConflict] = useState(false);
  const [removalRefreshRequired, setRemovalRefreshRequired] = useState(false);
  const [removalError, setRemovalError] = useState<string>();
  const [exhaustedPolicySignature, setExhaustedPolicySignature] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const mounted = useRef(true);
  const readyResponseRef = useRef<PermissionsResponse | null>(null);
  const requestGenerationRef = useRef(0);
  const latestRequestRef = useRef<{
    generation: number;
    promise: Promise<AuthoritativeRefreshResult>;
  } | null>(null);
  const currentPolicySignature = state.kind === 'ready' && shouldRefreshPolicy(state.response)
    ? policyRefreshSignature(state.response)
    : null;

  const clearTransientEditors = useCallback((): void => {
    setEditingAccess(undefined);
    setEditingProject(null);
    setRemovingAccess(null);
    setRemovalInstanceId(null);
    setRemovalConflict(false);
    setRemovalRefreshRequired(false);
    setRemovalError(undefined);
  }, []);

  const installReadyResponse = useCallback((candidate: PermissionsResponse): PermissionsResponse => {
    const previous = readyResponseRef.current;
    const accepted = revisionMonotonicResponse(previous, candidate);
    if (accepted === previous || !mounted.current) return accepted;
    if (
      previous !== null
      && previous.projection.instance.id !== accepted.projection.instance.id
    ) {
      clearTransientEditors();
    }
    readyResponseRef.current = accepted;
    setState({ kind: 'ready', response: accepted });
    return accepted;
  }, [clearTransientEditors]);

  const installMutationAcknowledgement = useCallback((
    requestInstanceId: string,
    acknowledgementInstanceId: string,
    updater: (current: PermissionsResponse) => PermissionsResponse,
  ): void => {
    const current = readyResponseRef.current;
    if (
      current === null
      || current.projection.instance.id !== requestInstanceId
      || acknowledgementInstanceId !== requestInstanceId
    ) return;
    installReadyResponse(updater(current));
  }, [installReadyResponse]);

  const installPageResult = useCallback((result: PageLoadResult): void => {
    if (!mounted.current) return;
    if (result.response) {
      installReadyResponse(result.response);
      return;
    }
    clearTransientEditors();
    readyResponseRef.current = null;
    setState(result.state);
  }, [clearTransientEditors, installReadyResponse]);

  const requestPermissionsPage = useCallback((
    preserveReady: boolean,
  ): Promise<AuthoritativeRefreshResult> => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    const promise = (async (): Promise<AuthoritativeRefreshResult> => {
      const result = await fetchPermissionsPage();
      if (!mounted.current) return { kind: 'failed' };
      if (generation !== requestGenerationRef.current) {
        const latest = latestRequestRef.current;
        return latest !== null && latest.generation > generation
          ? latest.promise
          : { kind: 'failed' };
      }
      if (!result.response) {
        if (!preserveReady || !result.canPreserveReady) installPageResult(result);
        return { kind: 'failed' };
      }
      const accepted = installReadyResponse(result.response);
      if (accepted.source !== 'live' || accepted.offline) {
        return { kind: 'offline' };
      }
      return { kind: 'ready', response: accepted };
    })();
    latestRequestRef.current = { generation, promise };
    return promise;
  }, [installPageResult, installReadyResponse]);

  const loadPage = useCallback(async (): Promise<void> => {
    await requestPermissionsPage(false);
  }, [requestPermissionsPage]);

  const refreshReady = useCallback((): Promise<AuthoritativeRefreshResult> => (
    requestPermissionsPage(true)
  ), [requestPermissionsPage]);

  const refreshPolicyStatus = useCallback(async (): Promise<void> => {
    const result = await refreshReady();
    if (!mounted.current || result.kind === 'failed') return;
    if (result.kind === 'ready' && shouldRefreshPolicy(result.response)) {
      setExhaustedPolicySignature(policyRefreshSignature(result.response));
    } else {
      setExhaustedPolicySignature(null);
    }
  }, [refreshReady]);

  useEffect(() => {
    mounted.current = true;
    void loadPage();
    return () => {
      mounted.current = false;
    };
  }, [loadPage]);

  const policyRefreshExhausted = currentPolicySignature !== null
    && exhaustedPolicySignature === currentPolicySignature;

  useEffect(() => {
    if (currentPolicySignature === null) return undefined;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      timer = setTimeout(() => { void poll(); }, POLICY_REFRESH_INTERVAL_MS);
    };
    const poll = async () => {
      attempts += 1;
      const result = await refreshReady();
      if (!active || result.kind === 'offline') return;
      if (result.kind === 'ready' && !shouldRefreshPolicy(result.response)) return;
      if (attempts < POLICY_REFRESH_MAX_ATTEMPTS) schedule();
      else if (mounted.current) {
        const signature = result.kind === 'ready'
          ? policyRefreshSignature(result.response)
          : currentPolicySignature;
        if (signature !== null) setExhaustedPolicySignature(signature);
      }
    };

    schedule();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [currentPolicySignature, refreshReady]);

  if (state.kind === 'loading') {
    return (
      <div className="space-y-4" aria-label={t('permissions.states.loadingTitle')}>
        <div className="h-20 animate-pulse rounded-lg border border-border bg-card/70" />
        <div className="h-64 animate-pulse rounded-lg border border-border bg-card/70" />
      </div>
    );
  }
  if (state.kind === 'denied') {
    return <EmptyState icon={ShieldX} title={t('permissions.states.deniedTitle')} body={t('permissions.states.deniedBody')} />;
  }
  if (state.kind === 'unavailable') {
    return (
      <div className="space-y-5">
        <EmptyState icon={state.offline ? WifiOff : CloudOff} title={t('permissions.states.unavailableTitle')} body={t('permissions.states.unavailableBody')} />
        <div className="flex justify-center"><Button variant="outline" onClick={() => void loadPage()}><RefreshCw className="size-4" />{t('common.retry')}</Button></div>
      </div>
    );
  }

  const { response } = state;
  const projection = response.projection;
  const groups = projection.directory.groups;
  const editable = capabilities.can_manage_instance
    && projection.instance.local_mutation_allowed
    && projection.capabilities.includes('instance.permissions.mutate')
    && !response.offline;
  const applying = projectionIsApplying(response);
  const instanceName = projection.instance.name?.trim() || t('permissions.currentInstance');
  const instanceInitial = Array.from(instanceName)[0]?.toUpperCase() || 'A';
  const instancePublicUrl = publicUrlLabel(projection.instance.public_url);
  const filteredProjects = projection.projects.filter((project) => (
    project.sync.status !== 'deleted'
    && `${project.display_name} ${project.project_id}`.toLowerCase().includes(search.trim().toLowerCase())
  ));

  const closeRemoval = () => {
    setRemovingAccess(null);
    setRemovalInstanceId(null);
    setRemovalConflict(false);
    setRemovalRefreshRequired(false);
    setRemovalError(undefined);
  };

  const refreshRemovalConflict = async (): Promise<boolean> => {
    if (removingAccess === null || removalInstanceId === null) return false;
    const latest = await refreshReady();
    setRemovalConflict(true);
    if (latest.kind !== 'ready') {
      setRemovalRefreshRequired(true);
      setRemovalError('permissions_refresh_failed');
      return false;
    }
    if (latest.response.projection.instance.id !== removalInstanceId) {
      setRemovalRefreshRequired(true);
      setRemovalError('permissions_pairing_changed');
      return false;
    }
    const targetExists = latest.response.projection.access.entries.some(
      (entry) => accessEntryKey(entry) === removingAccess,
    );
    if (!targetExists) {
      closeRemoval();
      return false;
    }
    setRemovalRefreshRequired(false);
    setRemovalError(undefined);
    return true;
  };

  const removeAccess = async () => {
    if (!editable || removingAccess === null || removalInstanceId === null) return;
    if (removalRefreshRequired) {
      await refreshRemovalConflict();
      return;
    }
    const targetExists = projection.access.entries.some(
      (entry) => accessEntryKey(entry) === removingAccess,
    );
    if (!targetExists) {
      closeRemoval();
      return;
    }
    const entries = projection.access.entries.filter(
      (entry) => accessEntryKey(entry) !== removingAccess,
    );
    setRemovalError(undefined);
    try {
      const requestInstanceId = removalInstanceId;
      const result = await replaceAuthorizedUsers(
        entries,
        projection.instance.authorization_revision,
        requestInstanceId,
      );
      installMutationAcknowledgement(requestInstanceId, result.instance_id, (current) => ({
        ...current,
        projection: {
          ...current.projection,
          instance: { ...current.projection.instance, authorization_revision: result.authorization_revision },
          access: { ...current.projection.access, entries: result.entries },
        },
      }));
      closeRemoval();
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        await refreshRemovalConflict();
      } else {
        setRemovalError(mutationErrorCode(caught));
      }
    }
  };

  return (
    <div className="w-full space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold md:text-[30px]">{t('permissions.title')}</h1>
          <p className="mt-1.5 max-w-3xl text-[13px] leading-5 text-muted md:text-sm">{t('permissions.description')}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          <Badge variant={projection.instance.permission_authority === 'instance' ? 'success' : 'info'}>
            {projection.instance.permission_authority === 'instance' ? <ShieldCheck className="size-3" /> : <Cloud className="size-3" />}
            {t(`permissions.authority.${projection.instance.permission_authority}`)}
          </Badge>
          <Button asChild size="sm" variant="outline">
            <a href="https://avibe.bot" target="_blank" rel="noreferrer">
              {t('permissions.actions.openCloud')}
              <ExternalLink className="size-3.5" />
            </a>
          </Button>
        </div>
      </header>

      <div className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-card px-4 py-4 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-mint/10 text-sm font-semibold text-mint-ink" aria-hidden="true">
            {instanceInitial}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="truncate text-[13px] font-semibold">{instanceName}</span>
              <Badge variant="secondary" className="text-[10px] font-semibold uppercase">
                {t('permissions.currentInstance')}
              </Badge>
            </div>
            {instancePublicUrl ? <div className="mt-0.5 truncate text-[11px] text-muted">{instancePublicUrl}</div> : null}
          </div>
        </div>
        {projection.instance.organization?.name ? (
          <Badge variant="outline" className="w-fit max-w-full shrink-0">
            <Building2 className="size-3" />
            <span className="truncate">{projection.instance.organization.name}</span>
          </Badge>
        ) : null}
      </div>

      {response.offline ? (
        <Notice
          tone="warning"
          icon={WifiOff}
          title={t('permissions.states.offlineTitle')}
          body={t('permissions.states.offlineBody')}
          action={(
            <Button size="sm" variant="outline" onClick={() => void refreshReady()}>
              <RefreshCw className="size-3.5" />
              {t('permissions.actions.refresh')}
            </Button>
          )}
        />
      ) : projection.instance.permission_authority === 'cloud' ? (
        <Notice
          tone="info"
          icon={Cloud}
          title={t('permissions.states.cloudTitle')}
          body={t('permissions.states.cloudBody')}
        />
      ) : !editable ? (
        <Notice tone="neutral" icon={Eye} title={t('permissions.states.readOnlyTitle')} body={t('permissions.states.readOnlyBody')} />
      ) : null}

      {!response.offline && projection.policy_sync.status === 'error' ? (
        <Notice tone="danger" icon={ShieldX} title={t('permissions.states.syncErrorTitle')} body={t('permissions.states.syncErrorBody')} />
      ) : !response.offline && projection.policy_sync.status === 'offline' ? (
        <Notice tone="warning" icon={CloudOff} title={t('permissions.states.syncOfflineTitle')} body={t('permissions.states.syncOfflineBody')} />
      ) : !response.offline && applying ? (
        <Notice
          tone="warning"
          icon={Loader2}
          title={t('permissions.states.applyingTitle')}
          body={t('permissions.states.applyingBody')}
          action={policyRefreshExhausted ? (
            <Button size="sm" variant="outline" onClick={() => void refreshPolicyStatus()}>
              <RefreshCw className="size-3.5" />
              {t('permissions.actions.refresh')}
            </Button>
          ) : undefined}
        />
      ) : null}

      <div className="flex h-10 items-center gap-1 border-b border-border" role="tablist" aria-label={t('permissions.tabs.label')}>
        {(['access', 'projects'] as const).map((candidate) => (
          <button
            key={candidate}
            type="button"
            role="tab"
            aria-selected={tab === candidate}
            onClick={() => setTab(candidate)}
            className={clsx(
              'inline-flex h-10 items-center gap-2 border-b-2 px-3 text-[13px] font-medium',
              tab === candidate ? 'border-mint text-foreground' : 'border-transparent text-muted hover:text-foreground',
            )}
          >
            {candidate === 'access' ? <Users className="size-4" /> : <FolderKey className="size-4" />}
            {t(`permissions.tabs.${candidate}`)}
          </button>
        ))}
      </div>

      {tab === 'access' ? (
        <section className="space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-[15px] font-semibold">{t('permissions.access.title')}</h2>
              <p className="mt-0.5 text-[12px] text-muted">{t('permissions.access.description')}</p>
            </div>
            {editable ? <Button variant="brand" size="sm" onClick={() => setEditingAccess(null)}><Plus className="size-4" />{t('permissions.actions.addAccess')}</Button> : null}
          </div>
          <div className="flex items-center gap-3 rounded-lg border border-violet/35 bg-violet/10 px-4 py-3">
            <ShieldCheck className="size-5 shrink-0 text-violet-ink" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-semibold">{projection.access.owner.email ?? t('permissions.access.protectedOwner')}</div>
              <div className="text-[11px] text-muted">{t('permissions.access.ownerBody')}</div>
            </div>
            <Badge variant="secondary">{t('permissions.roles.owner')}</Badge>
          </div>
          {projection.instance.access_mode === 'public' ? (
            <Notice
              tone="info"
              icon={Globe2}
              title={t('permissions.access.publicTitle')}
              body={t(projection.access.entries.length === 0
                ? 'permissions.access.publicBody'
                : 'permissions.access.publicAssignmentsBody')}
            />
          ) : null}
          {projection.access.entries.length === 0 ? (
            projection.instance.access_mode === 'public' ? null : (
              <EmptyState
                icon={Users}
                title={t('permissions.access.emptyTitle')}
                body={t('permissions.access.emptyBody')}
              />
            )
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-card">
              {projection.access.entries.map((entry) => {
                const Icon = principalIcon(entry.kind);
                const entryKey = accessEntryKey(entry);
                return (
                  <div key={entryKey} className="grid gap-3 border-b border-border px-4 py-3 last:border-0 md:grid-cols-[minmax(220px,1fr)_150px_120px_84px] md:items-center">
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/10 text-mint-ink"><Icon className="size-4" /></span>
                      <span className="truncate text-[12px] font-semibold">{displayPrincipal(entry.kind, entry.value, groups)}</span>
                    </div>
                    <Badge variant="secondary">{t(`permissions.principals.${entry.kind}`)}</Badge>
                    <Badge variant={entry.role === 'editor' ? 'success' : 'secondary'}>{entry.role === 'editor' ? <Pencil className="size-3" /> : <Eye className="size-3" />}{t(`permissions.roles.${entry.role}`)}</Badge>
                    <div className="flex justify-end gap-1">
                      {editable ? <><Button size="icon" variant="ghost" aria-label={t('permissions.actions.editAccess')} onClick={() => setEditingAccess(entryKey)}><Pencil className="size-4" /></Button><Button size="icon" variant="ghost" aria-label={t('permissions.actions.removeAccess')} onClick={() => { setRemovingAccess(entryKey); setRemovalInstanceId(projection.instance.id); setRemovalConflict(false); setRemovalRefreshRequired(false); setRemovalError(undefined); }}><Trash2 className="size-4 text-destructive-ink" /></Button></> : null}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>
      ) : (
        <section className="space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-[15px] font-semibold">{t('permissions.projects.title')}</h2>
              <p className="mt-0.5 text-[12px] text-muted">{t('permissions.projects.description')}</p>
            </div>
            <Input className="h-9 sm:w-64" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('permissions.projects.searchPlaceholder')} />
          </div>
          {filteredProjects.length === 0 ? (
            <EmptyState icon={FolderKey} title={t('permissions.projects.emptyTitle')} body={t('permissions.projects.emptyBody')} />
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-card">
              {filteredProjects.map((project) => {
                const mode = projectMode(project);
                return (
                  <div key={project.project_id} className="grid gap-3 border-b border-border px-4 py-3 last:border-0 md:grid-cols-[minmax(220px,1fr)_150px_minmax(160px,1fr)_130px_90px] md:items-center">
                    <div className="flex min-w-0 items-center gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-lg bg-cyan/10 text-cyan-ink"><FolderKey className="size-4" /></span><div className="min-w-0"><div className="truncate text-[12px] font-semibold">{project.display_name}</div><div className="truncate font-mono text-[10px] text-muted">{project.project_id}</div></div></div>
                    <Badge variant={mode === 'owner_only' ? 'warning' : mode === 'restricted' ? 'info' : 'secondary'}>{mode === 'owner_only' ? <LockKeyhole className="size-3" /> : mode === 'restricted' ? <ShieldCheck className="size-3" /> : <Users className="size-3" />}{t(`permissions.projects.modes.${mode}`)}</Badge>
                    <span className="truncate text-[12px] text-muted">{mode === 'restricted' ? t('permissions.projects.bindingCount', { count: project.access.bindings.length }) : t(`permissions.projects.audience.${mode}`)}</span>
                    <SyncBadge status={project.sync.status} />
                    {editable ? <Button size="sm" variant="outline" onClick={() => setEditingProject(project)}>{t('permissions.actions.manage')}</Button> : <span />}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      )}

      <AccessEntryDialog
        open={editingAccess !== undefined}
        editingKey={editingAccess ?? null}
        response={response}
        editable={editable}
        onOpenChange={(open) => { if (!open) setEditingAccess(undefined); }}
        onRefresh={refreshReady}
        onSaved={(instanceId, result) => installMutationAcknowledgement(instanceId, result.instance_id, (current) => ({
          ...current,
          projection: {
            ...current.projection,
            instance: { ...current.projection.instance, authorization_revision: result.authorization_revision },
            access: { ...current.projection.access, entries: result.entries },
          },
        }))}
      />
      <ProjectAccessDialog
        project={editingProject}
        instanceId={projection.instance.id}
        groups={groups}
        editable={editable}
        onOpenChange={(open) => { if (!open) setEditingProject(null); }}
        onRefresh={refreshReady}
        onSaved={(instanceId, result) => installMutationAcknowledgement(
          instanceId,
          result.instance_id,
          (current) => mergeProjectMutationAcknowledgement(current, result),
        )}
      />
      <ConfirmDialog
        open={removingAccess !== null}
        onOpenChange={(open) => { if (!open) closeRemoval(); }}
        title={t('permissions.access.removeTitle')}
        description={t('permissions.access.removeBody')}
        destructive
        confirmLabel={t('permissions.actions.removeAccess')}
        confirmDisabled={!editable}
        onConfirm={removeAccess}
      >
        {removalConflict ? (
          <Notice
            tone="warning"
            icon={AlertTriangle}
            title={t('permissions.states.conflictTitle')}
            body={t(removalRefreshRequired
              ? removalError === 'permissions_pairing_changed'
                ? 'permissions.states.pairingChangedBody'
                : 'permissions.states.conflictRefreshBody'
              : 'permissions.states.conflictBody')}
          />
        ) : null}
        {removalError ? (
          <Notice
            tone="danger"
            icon={ShieldX}
            title={t('permissions.states.errorTitle')}
            body={t(`permissions.errors.${removalError}`, {
              defaultValue: t('permissions.errors.generic'),
            })}
          />
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
