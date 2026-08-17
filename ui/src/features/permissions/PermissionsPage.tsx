import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
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
  SyncStatus,
} from './types';

type PageState =
  | { kind: 'loading' }
  | { kind: 'ready'; response: PermissionsResponse }
  | { kind: 'denied'; code: string }
  | { kind: 'unavailable'; code: string; offline: boolean };

type PageLoadResult = {
  state: Exclude<PageState, { kind: 'loading' }>;
  response: PermissionsResponse | null;
};

type AuthoritativeRefreshResult =
  | { kind: 'ready'; response: PermissionsResponse }
  | { kind: 'offline' }
  | { kind: 'failed' };

type AuthoritativeRefresh = () => Promise<AuthoritativeRefreshResult>;

const POLICY_REFRESH_INTERVAL_MS = 2_000;
const POLICY_REFRESH_MAX_ATTEMPTS = 30;

async function fetchPermissionsPage(): Promise<PageLoadResult> {
  try {
    const response = await getPermissions();
    return { state: { kind: 'ready', response }, response };
  } catch (caught) {
    const error = caught instanceof PermissionsApiError ? caught : null;
    if (error?.code === 'instance_access_forbidden') {
      return { state: { kind: 'denied', code: error.code }, response: null };
    }
    return {
      state: {
        kind: 'unavailable',
        code: error?.code ?? 'permissions_unavailable',
        offline: error?.offline === true,
      },
      response: null,
    };
  }
}

const projectionIsApplying = (response: PermissionsResponse): boolean => (
  response.projection.policy_sync.status === 'applying'
  || response.projection.projects.some((project) => (
    project.sync.status === 'pending' || project.sync.status === 'applying'
  ))
);

const shouldRefreshPolicy = (response: PermissionsResponse): boolean => (
  response.source === 'live' && !response.offline && projectionIsApplying(response)
);

const mutationErrorCode = (caught: unknown): string => (
  caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable'
);

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

const accessEntryKey = (entry: AccessEntry): string => (
  `${entry.kind}:${normalizePrincipal(entry.kind, entry.value)}`
);

function SyncBadge({ status }: { status: SyncStatus }) {
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
  onOpenChange,
  onRefresh,
  onSaved,
}: {
  open: boolean;
  editingKey: string | null;
  response: PermissionsResponse;
  onOpenChange: (open: boolean) => void;
  onRefresh: AuthoritativeRefresh;
  onSaved: (result: Awaited<ReturnType<typeof replaceAuthorizedUsers>>) => void;
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
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const originalEntry = useRef<AccessEntry | null>(null);
  const baselineEntry = useRef<AccessEntry | null>(null);
  const expectedInstanceId = useRef('');

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
  const resolvedValue = kind === 'organization_group'
    ? (value || activeGroups[0]?.id || '')
    : normalizePrincipal(kind, value);
  const candidate: AccessEntry = { kind, value: resolvedValue, role };
  const originalKey = originalEntry.current ? accessEntryKey(originalEntry.current) : null;
  const authoritativeIndex = originalKey === null
    ? -1
    : entries.findIndex((entry) => accessEntryKey(entry) === originalKey);
  const nextEntries = editingKey === null || authoritativeIndex < 0
    ? [...entries, candidate]
    : entries.map((entry, index) => (index === authoritativeIndex ? candidate : entry));

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
    baselineEntry.current = originalKey === null
      ? null
      : latest.response.projection.access.entries.find(
          (entry) => accessEntryKey(entry) === originalKey,
        ) ?? null;
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
    if (!validateDraft()) return;
    setSaving(true);
    setError(undefined);
    try {
      const result = await replaceAuthorizedUsers(
        nextEntries,
        revision,
        expectedInstanceId.current,
      );
      onSaved(result);
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        await refreshConflict();
      } else {
        setError(mutationErrorCode(caught));
      }
    } finally {
      setSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = async () => {
    if (refreshRequired) {
      setSaving(true);
      try {
        await refreshConflict();
      } finally {
        setSaving(false);
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
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent>
        <DialogHeader>
          <DialogTitle>{t(editingKey === null ? 'permissions.access.addTitle' : 'permissions.access.editTitle')}</DialogTitle>
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
              {activeGroups.length > 0 ? <option value="organization_group">{t('permissions.principals.organization_group')}</option> : null}
              <option value="email">{t('permissions.principals.email')}</option>
              <option value="email_domain">{t('permissions.principals.email_domain')}</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="permissions-access-value">{t('permissions.fields.principal')}</Label>
            {kind === 'organization_group' ? (
              <Select id="permissions-access-value" value={resolvedValue} onChange={(event) => setValue(event.target.value)}>
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
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={saving || !candidate.value} onClick={() => void save()}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : null}
            {t(conflict ? 'permissions.actions.retrySave' : 'permissions.actions.save')}
          </Button>
        </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('permissions.access.narrowTitle')}
        description={t('permissions.access.narrowBody')}
        confirmLabel={t('permissions.actions.save')}
        onConfirm={commit}
      />
    </>
  );
}

function ProjectAccessDialog({
  project,
  instanceId,
  groups,
  onOpenChange,
  onRefresh,
  onSaved,
}: {
  project: PermissionProject | null;
  instanceId: string;
  groups: DirectoryGroup[];
  onOpenChange: (open: boolean) => void;
  onRefresh: AuthoritativeRefresh;
  onSaved: (result: Awaited<ReturnType<typeof updateProjectAccess>>) => void;
}) {
  const { t } = useTranslation();
  const initialized = useRef(false);
  const [mode, setMode] = useState<ProjectAccessMode>('inherit');
  const [bindings, setBindings] = useState<ProjectBinding[]>([]);
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const baseline = useRef<PermissionProject | null>(null);
  const expectedInstanceId = useRef('');
  const open = Boolean(project);

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
      (item) => item.project_id === project.project_id,
    );
    if (!authoritative) {
      onOpenChange(false);
      return false;
    }
    baseline.current = authoritative;
    setRevision(authoritative.access.revision);
    setRefreshRequired(false);
    setError(undefined);
    return true;
  };

  const commit = async () => {
    if (!project || invalid) return;
    if (hasDuplicateProjectBindings(wireBindings)) {
      setError('duplicate_project_access_principal');
      return;
    }
    setSaving(true);
    setError(undefined);
    try {
      const result = await updateProjectAccess(
        project,
        mode,
        wireBindings,
        revision,
        expectedInstanceId.current,
      );
      onSaved(result);
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        await refreshConflict();
      } else {
        setError(mutationErrorCode(caught));
      }
    } finally {
      setSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = async () => {
    if (!project || invalid) return;
    if (refreshRequired) {
      setSaving(true);
      try {
        await refreshConflict();
      } finally {
        setSaving(false);
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
      <Dialog open={open} onOpenChange={onOpenChange}>
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
                {bindings.map((binding, index) => (
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
                      {activeGroups.length > 0 ? <option value="organization_group">{t('permissions.principals.organization_group')}</option> : null}
                      <option value="email">{t('permissions.principals.email')}</option>
                      <option value="email_domain">{t('permissions.principals.email_domain')}</option>
                    </Select>
                    {binding.principal_kind === 'organization_group' ? (
                      <Select
                        aria-label={t('permissions.fields.principal')}
                        value={binding.principal_value}
                        onChange={(event) => setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, principal_value: event.target.value } : item))}
                      >
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
                ))}
                {bindings.length === 0 ? <p className="text-[12px] text-gold-ink">{t('permissions.projects.bindingRequired')}</p> : null}
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
            <Button variant="brand" disabled={saving || invalid} onClick={() => void save()}>
              {saving ? <Loader2 className="size-4 animate-spin" /> : null}
              {t(conflict ? 'permissions.actions.retrySave' : 'permissions.actions.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('permissions.projects.narrowTitle')}
        description={t('permissions.projects.narrowBody')}
        confirmLabel={t('permissions.actions.save')}
        onConfirm={commit}
      />
    </>
  );
}

export function PermissionsPage() {
  const { t } = useTranslation();
  const { capabilities, instanceRole } = useInstanceAuthorization();
  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [tab, setTab] = useState<'access' | 'projects'>('access');
  const [editingAccess, setEditingAccess] = useState<string | null | undefined>(undefined);
  const [editingProject, setEditingProject] = useState<PermissionProject | null>(null);
  const [removingAccess, setRemovingAccess] = useState<string | null>(null);
  const [removalInstanceId, setRemovalInstanceId] = useState<string | null>(null);
  const [removalConflict, setRemovalConflict] = useState(false);
  const [removalRefreshRequired, setRemovalRefreshRequired] = useState(false);
  const [removalError, setRemovalError] = useState<string>();
  const [search, setSearch] = useState('');
  const mounted = useRef(true);

  const loadPage = useCallback(async (): Promise<void> => {
    const result = await fetchPermissionsPage();
    if (mounted.current) setState(result.state);
  }, []);

  const refreshReady = useCallback(async (): Promise<AuthoritativeRefreshResult> => {
    const result = await fetchPermissionsPage();
    if (!result.response) return { kind: 'failed' };
    if (result.response.source !== 'live' || result.response.offline) {
      return { kind: 'offline' };
    }
    if (mounted.current) setState({ kind: 'ready', response: result.response });
    return { kind: 'ready', response: result.response };
  }, []);

  useEffect(() => {
    let active = true;
    mounted.current = true;
    void fetchPermissionsPage().then((result) => {
      if (active) setState(result.state);
    });
    return () => {
      active = false;
      mounted.current = false;
    };
  }, []);

  const livePolicyIsApplying = state.kind === 'ready' && shouldRefreshPolicy(state.response);

  useEffect(() => {
    if (!livePolicyIsApplying) return undefined;
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
    };

    schedule();
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [livePolicyIsApplying, refreshReady]);

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
    && !response.offline;
  const applying = projectionIsApplying(response);
  const filteredProjects = projection.projects.filter((project) => (
    `${project.display_name} ${project.project_id}`.toLowerCase().includes(search.trim().toLowerCase())
  ));

  const updateResponse = (updater: (current: PermissionsResponse) => PermissionsResponse) => {
    setState((current) => current.kind === 'ready'
      ? { kind: 'ready', response: updater(current.response) }
      : current);
  };

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
    if (removingAccess === null || removalInstanceId === null) return;
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
      const result = await replaceAuthorizedUsers(
        entries,
        projection.instance.authorization_revision,
        removalInstanceId,
      );
      updateResponse((current) => ({
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
    <div className="mx-auto max-w-6xl space-y-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ShieldCheck className="size-6 text-mint-ink" />
            <h1 className="text-2xl font-semibold md:text-[30px]">{t('permissions.title')}</h1>
          </div>
          <p className="mt-1.5 max-w-3xl text-[13px] leading-5 text-muted md:text-sm">{t('permissions.description')}</p>
        </div>
        <Badge variant={projection.instance.permission_authority === 'instance' ? 'success' : 'info'}>
          {projection.instance.permission_authority === 'instance' ? <ShieldCheck className="size-3" /> : <Cloud className="size-3" />}
          {t(`permissions.authority.${projection.instance.permission_authority}`)}
        </Badge>
      </header>

      <div className="flex min-w-0 items-center gap-3 rounded-lg border border-border bg-card px-4 py-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-mint/10 text-mint-ink"><ShieldCheck className="size-5" /></span>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold">{t('permissions.currentInstance')}</div>
          <div className="truncate font-mono text-[11px] text-muted">{projection.instance.id}</div>
        </div>
        <Badge variant="secondary">{t(`permissions.roles.${instanceRole ?? 'viewer'}`)}</Badge>
      </div>

      {response.offline ? (
        <Notice tone="warning" icon={WifiOff} title={t('permissions.states.offlineTitle')} body={t('permissions.states.offlineBody')} />
      ) : projection.instance.permission_authority === 'cloud' ? (
        <Notice
          tone="info"
          icon={Cloud}
          title={t('permissions.states.cloudTitle')}
          body={t('permissions.states.cloudBody')}
          action={<Button asChild size="sm" variant="outline"><a href="https://avibe.bot" target="_blank" rel="noreferrer">{t('permissions.actions.openCloud')}<ExternalLink className="size-3.5" /></a></Button>}
        />
      ) : !capabilities.can_manage_instance ? (
        <Notice tone="neutral" icon={Eye} title={t('permissions.states.readOnlyTitle')} body={t('permissions.states.readOnlyBody')} />
      ) : applying ? (
        <Notice tone="warning" icon={Loader2} title={t('permissions.states.applyingTitle')} body={t('permissions.states.applyingBody')} />
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
          {projection.access.entries.length === 0 ? (
            <EmptyState
              icon={projection.instance.access_mode === 'public' ? Globe2 : Users}
              title={t(projection.instance.access_mode === 'public'
                ? 'permissions.access.publicTitle'
                : 'permissions.access.emptyTitle')}
              body={t(projection.instance.access_mode === 'public'
                ? 'permissions.access.publicBody'
                : 'permissions.access.emptyBody')}
            />
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
        onOpenChange={(open) => { if (!open) setEditingAccess(undefined); }}
        onRefresh={refreshReady}
        onSaved={(result) => updateResponse((current) => ({
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
        onOpenChange={(open) => { if (!open) setEditingProject(null); }}
        onRefresh={refreshReady}
        onSaved={(result) => updateResponse((current) => ({
          ...current,
          projection: {
            ...current.projection,
            instance: { ...current.projection.instance, authorization_revision: result.authorization_revision },
            projects: current.projection.projects.map((project) => project.project_id === result.project.project_id ? result.project : project),
          },
        }))}
      />
      <ConfirmDialog
        open={removingAccess !== null}
        onOpenChange={(open) => { if (!open) closeRemoval(); }}
        title={t('permissions.access.removeTitle')}
        description={t('permissions.access.removeBody')}
        destructive
        confirmLabel={t('permissions.actions.removeAccess')}
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
