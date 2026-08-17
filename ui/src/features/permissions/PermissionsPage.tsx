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
  onReload,
  onSaved,
}: {
  open: boolean;
  editingKey: string | null;
  response: PermissionsResponse;
  onOpenChange: (open: boolean) => void;
  onReload: () => Promise<PermissionsResponse | null>;
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
  const originalEntry = useRef<AccessEntry | null>(null);

  useEffect(() => {
    if (open && !initialized.current) {
      originalEntry.current = editing ?? null;
      setKind(editing?.kind ?? 'email');
      setValue(editing?.value ?? '');
      setRole(editing?.role ?? 'viewer');
      setRevision(response.projection.instance.authorization_revision);
      setError(undefined);
      setConflict(false);
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

  const commit = async () => {
    if (!candidate.value || hasDuplicateAccessEntries(nextEntries)) {
      setError(candidate.value ? 'duplicate_access_principal' : 'invalid_request');
      return;
    }
    setSaving(true);
    setError(undefined);
    try {
      const result = await replaceAuthorizedUsers(nextEntries, revision);
      onSaved(result);
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        const latest = await onReload();
        if (latest) setRevision(latest.projection.instance.authorization_revision);
        setConflict(true);
      } else {
        setError(caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable');
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t(editing ? 'permissions.access.editTitle' : 'permissions.access.addTitle')}</DialogTitle>
          <DialogDescription>{t('permissions.access.dialogBody')}</DialogDescription>
        </DialogHeader>
        {conflict ? (
          <Notice
            tone="warning"
            icon={AlertTriangle}
            title={t('permissions.states.conflictTitle')}
            body={t('permissions.states.conflictBody')}
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
          <Button variant="brand" disabled={saving || !candidate.value} onClick={() => void commit()}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : null}
            {t(conflict ? 'permissions.actions.retrySave' : 'permissions.actions.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ProjectAccessDialog({
  project,
  groups,
  onOpenChange,
  onReload,
  onSaved,
}: {
  project: PermissionProject | null;
  groups: DirectoryGroup[];
  onOpenChange: (open: boolean) => void;
  onReload: () => Promise<PermissionsResponse | null>;
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
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const baseline = useRef<PermissionProject | null>(null);
  const open = Boolean(project);

  useEffect(() => {
    if (project && !initialized.current) {
      baseline.current = project;
      setMode(projectMode(project));
      setBindings(project.access.bindings);
      setRevision(project.access.revision);
      setError(undefined);
      setConflict(false);
      setConfirmNarrowing(false);
    }
    initialized.current = open;
  }, [open, project]);

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
      );
      onSaved(result);
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        const latest = await onReload();
        const authoritative = latest?.projection.projects.find((item) => item.project_id === project.project_id);
        if (authoritative) {
          baseline.current = authoritative;
          setRevision(authoritative.access.revision);
        } else if (caught.currentRevision !== undefined) {
          setRevision(caught.currentRevision);
        }
        setConflict(true);
      } else {
        setError(caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable');
      }
    } finally {
      setSaving(false);
      setConfirmNarrowing(false);
    }
  };

  const save = () => {
    if (!project || invalid) return;
    const current = baseline.current ?? project;
    if (requiresProjectNarrowing(projectMode(current), current.access.bindings, mode, wireBindings)) {
      setConfirmNarrowing(true);
      return;
    }
    void commit();
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
          {conflict ? <Notice tone="warning" icon={AlertTriangle} title={t('permissions.states.conflictTitle')} body={t('permissions.states.conflictBody')} /> : null}
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
            <Button variant="brand" disabled={saving || invalid} onClick={conflict ? () => void commit() : save}>
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
  const [search, setSearch] = useState('');

  const load = useCallback(async (): Promise<PermissionsResponse | null> => {
    const result = await fetchPermissionsPage();
    setState(result.state);
    return result.response;
  }, []);

  useEffect(() => {
    let active = true;
    void fetchPermissionsPage().then((result) => {
      if (active) setState(result.state);
    });
    return () => { active = false; };
  }, []);

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
        <div className="flex justify-center"><Button variant="outline" onClick={() => void load()}><RefreshCw className="size-4" />{t('common.retry')}</Button></div>
      </div>
    );
  }

  const { response } = state;
  const projection = response.projection;
  const groups = projection.directory.groups;
  const editable = capabilities.can_manage_instance
    && projection.instance.local_mutation_allowed
    && !response.offline;
  const applying = projection.policy_sync.status === 'applying'
    || projection.projects.some((project) => ['pending', 'applying'].includes(project.sync.status));
  const filteredProjects = projection.projects.filter((project) => (
    `${project.display_name} ${project.project_id}`.toLowerCase().includes(search.trim().toLowerCase())
  ));

  const updateResponse = (updater: (current: PermissionsResponse) => PermissionsResponse) => {
    setState((current) => current.kind === 'ready'
      ? { kind: 'ready', response: updater(current.response) }
      : current);
  };

  const removeAccess = async () => {
    if (removingAccess === null) return;
    const targetExists = projection.access.entries.some(
      (entry) => accessEntryKey(entry) === removingAccess,
    );
    if (!targetExists) {
      setRemovingAccess(null);
      return;
    }
    const entries = projection.access.entries.filter(
      (entry) => accessEntryKey(entry) !== removingAccess,
    );
    try {
      const result = await replaceAuthorizedUsers(entries, projection.instance.authorization_revision);
      updateResponse((current) => ({
        ...current,
        projection: {
          ...current.projection,
          instance: { ...current.projection.instance, authorization_revision: result.authorization_revision },
          access: { ...current.projection.access, entries: result.entries },
        },
      }));
      setRemovingAccess(null);
    } catch (caught) {
      if (isRevisionConflict(caught)) await load();
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
            <EmptyState icon={Users} title={t('permissions.access.emptyTitle')} body={t('permissions.access.emptyBody')} />
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
                      {editable ? <><Button size="icon" variant="ghost" aria-label={t('permissions.actions.editAccess')} onClick={() => setEditingAccess(entryKey)}><Pencil className="size-4" /></Button><Button size="icon" variant="ghost" aria-label={t('permissions.actions.removeAccess')} onClick={() => setRemovingAccess(entryKey)}><Trash2 className="size-4 text-destructive-ink" /></Button></> : null}
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
        onReload={load}
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
        groups={groups}
        onOpenChange={(open) => { if (!open) setEditingProject(null); }}
        onReload={load}
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
        onOpenChange={(open) => { if (!open) setRemovingAccess(null); }}
        title={t('permissions.access.removeTitle')}
        description={t('permissions.access.removeBody')}
        destructive
        confirmLabel={t('permissions.actions.removeAccess')}
        onConfirm={removeAccess}
      />
    </div>
  );
}
