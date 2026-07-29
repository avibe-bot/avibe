import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bot,
  Building2,
  KeyRound,
  LayoutPanelTop,
  LockKeyhole,
  Sparkles,
  Users,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { SegmentedRadio } from '@/components/ui/segmented';

import { isRevisionConflict, jsonBody, OrganizationApiError } from '../api/client';
import type {
  OrganizationGroup,
  OrganizationInstance,
  OrganizationResource,
  ResourceAccessLevel,
  ResourceKind,
} from '../api/types';
import {
  ConflictBanner,
  EmptyState,
  ErrorBanner,
  ForbiddenState,
  LoadingState,
  PageHeader,
  SyncBadge,
  TableFrame,
} from '../components';
import { useOrganization } from '../context';

const RESOURCE_KINDS: Array<{ kind: ResourceKind; icon: typeof Bot }> = [
  { kind: 'agent', icon: Bot },
  { kind: 'vault_secret', icon: KeyRound },
  { kind: 'skill', icon: Sparkles },
  { kind: 'show_page', icon: LayoutPanelTop },
];

function resourceIcon(kind: ResourceKind) {
  return RESOURCE_KINDS.find((item) => item.kind === kind)?.icon ?? Bot;
}

function ResourceAccessDialog({
  resource,
  groups,
  onOpenChange,
  onSaved,
}: {
  resource: OrganizationResource | null;
  groups: OrganizationGroup[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const { selectedOrganizationId, request } = useOrganization();
  const [level, setLevel] = useState<ResourceAccessLevel>('private');
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);

  useEffect(() => {
    if (!resource) return;
    setLevel(resource.access?.access_level ?? 'private');
    setGroupIds(resource.access?.group_ids ?? []);
    setRevision(resource.access?.revision ?? 0);
    setError(undefined);
    setConflict(false);
  }, [resource]);

  const visibleGroups = groups.filter((group) => !group.archived_at || groupIds.includes(group.id));
  const toggleGroup = (group: OrganizationGroup) => {
    const selected = groupIds.includes(group.id);
    if (group.archived_at && !selected) return;
    setGroupIds((current) => selected
      ? current.filter((id) => id !== group.id)
      : [...current, group.id]);
  };

  const save = async () => {
    if (!resource || !selectedOrganizationId || (level === 'scope' && groupIds.length === 0)) return;
    setSaving(true);
    setError(undefined);
    try {
      await request(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/resources/${encodeURIComponent(resource.instance_id)}/${encodeURIComponent(resource.resource_kind)}/${encodeURIComponent(resource.resource_id)}/access`,
        {
          method: 'PATCH',
          body: jsonBody({
            access_level: level,
            group_ids: level === 'scope' ? [...new Set(groupIds)] : [],
            if_match_revision: revision,
          }),
        },
      );
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        setConflict(true);
        await onSaved();
        if (caught.currentRevision !== undefined) setRevision(caught.currentRevision);
      } else {
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={Boolean(resource)} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t('organization.resources.dialogTitle', { name: resource?.display_name })}</DialogTitle>
          <DialogDescription>{t('organization.resources.dialogBody')}</DialogDescription>
        </DialogHeader>
        {conflict ? <ConflictBanner onReload={() => setConflict(false)} /> : null}
        {error ? <ErrorBanner code={error} /> : null}
        <div className="space-y-5">
          <div className="space-y-1.5">
            <Label>{t('organization.resources.accessLevel')}</Label>
            <SegmentedRadio
              value={level}
              onChange={setLevel}
              ariaLabel={t('organization.resources.accessLevel')}
              options={[
                { id: 'private', label: t('organization.resources.levels.private') },
                { id: 'public', label: t('organization.resources.levels.public') },
                { id: 'scope', label: t('organization.resources.levels.scope') },
              ]}
            />
          </div>
          <div className="rounded-lg border border-border bg-foreground/[0.025] p-3 text-[12px] leading-5 text-muted">
            {t(`organization.resources.levelHelp.${level}`)}
          </div>
          {level === 'scope' ? (
            <fieldset>
              <legend className="mb-2 text-[12px] font-medium">{t('organization.resources.selectGroups')}</legend>
              <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-border p-2">
                {visibleGroups.map((group) => {
                  const selected = groupIds.includes(group.id);
                  return (
                    <button
                      key={group.id}
                      type="button"
                      role="checkbox"
                      aria-checked={selected}
                      className={clsx(
                        'flex w-full items-center gap-3 rounded-md px-2 py-2 text-left',
                        group.archived_at && !selected ? 'cursor-not-allowed opacity-50' : 'hover:bg-foreground/[0.04]',
                      )}
                      disabled={Boolean(group.archived_at && !selected)}
                      onClick={() => toggleGroup(group)}
                    >
                      <Checkbox checked={selected} presentational />
                      <span className="min-w-0 flex-1 truncate text-[12px]">{group.name}</span>
                      {group.archived_at ? <Badge variant="secondary">{t('organization.groups.archived')}</Badge> : null}
                    </button>
                  );
                })}
              </div>
              {groupIds.length === 0 ? <div className="mt-2 text-[11px] text-gold">{t('organization.resources.groupRequired')}</div> : null}
            </fieldset>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={saving || (level === 'scope' && groupIds.length === 0)} onClick={() => void save()}>
            {saving ? t('organization.actions.saving') : t('organization.actions.saveChanges')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function OrganizationResourcesPage() {
  const { t } = useTranslation();
  const { detail, selectedOrganizationId, request, dataVersion } = useOrganization();
  const [resources, setResources] = useState<OrganizationResource[] | null>(null);
  const [groups, setGroups] = useState<OrganizationGroup[]>([]);
  const [instances, setInstances] = useState<OrganizationInstance[]>([]);
  const [kind, setKind] = useState<ResourceKind>('agent');
  const [editing, setEditing] = useState<OrganizationResource | null>(null);
  const [error, setError] = useState<string>();
  const [forbidden, setForbidden] = useState(false);
  const canManage = detail?.capabilities.can_manage_organization === true;

  const load = useCallback(async () => {
    if (!selectedOrganizationId || !canManage) return;
    setError(undefined);
    setForbidden(false);
    try {
      const [resourceResult, groupResult, instanceResult] = await Promise.all([
        request<{ resources: OrganizationResource[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/resources`,
        ),
        request<{ groups: OrganizationGroup[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/groups`,
        ),
        request<{ instances: OrganizationInstance[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/instances`,
        ),
      ]);
      setResources(resourceResult.resources.filter((resource) => resource.sync.status !== 'deleted'));
      setGroups(groupResult.groups);
      setInstances(instanceResult.instances);
    } catch (caught) {
      if (caught instanceof OrganizationApiError && (caught.status === 403 || caught.status === 404)) {
        setForbidden(true);
      } else {
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    }
  }, [canManage, request, selectedOrganizationId]);

  useEffect(() => { void load(); }, [dataVersion, load]);

  const visible = useMemo(() => (resources ?? []).filter((resource) => resource.resource_kind === kind), [kind, resources]);
  const groupNames = useMemo(() => new Map(groups.map((group) => [group.id, group.name])), [groups]);
  const instanceNames = useMemo(() => new Map(instances.map((instance) => [instance.id, instance.slug])), [instances]);

  if (!canManage) return <ForbiddenState />;
  if (!resources && !error && !forbidden) return <LoadingState />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.resources')}
        title={t('organization.nav.resources')}
        description={t('organization.resources.description')}
      />
      <div className="flex flex-wrap items-center gap-1 border-b border-border" role="tablist" aria-label={t('organization.resources.tabsLabel')}>
        {RESOURCE_KINDS.map((item) => {
          const Icon = item.icon;
          const count = (resources ?? []).filter((resource) => resource.resource_kind === item.kind).length;
          return (
            <button
              key={item.kind}
              type="button"
              role="tab"
              aria-selected={kind === item.kind}
              className={clsx(
                'inline-flex h-10 items-center gap-2 border-b-2 px-3 text-[12px] font-medium',
                kind === item.kind ? 'border-mint text-foreground' : 'border-transparent text-muted hover:text-foreground',
              )}
              onClick={() => setKind(item.kind)}
            >
              <Icon className="size-4" />
              {t(`organization.resources.kinds.${item.kind}`)}
              <span className="text-[10px] text-muted">{count}</span>
            </button>
          );
        })}
      </div>
      {forbidden ? <ForbiddenState /> : null}
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {!forbidden && visible.length === 0 ? (
        <EmptyState title={t('organization.resources.emptyTitle')} body={t('organization.resources.emptyBody', { kind: t(`organization.resources.kinds.${kind}`) })} />
      ) : !forbidden ? (
        <TableFrame>
          <div className="hidden grid-cols-[minmax(220px,1.35fr)_170px_minmax(190px,1fr)_130px_90px] gap-4 border-b border-border px-5 py-3 font-mono text-[10px] font-bold uppercase text-muted md:grid">
            <div>{t(`organization.resources.kinds.${kind}`)}</div>
            <div>{t('organization.resources.columns.access')}</div>
            <div>{t('organization.resources.columns.audience')}</div>
            <div>{t('organization.resources.columns.sync')}</div>
            <div />
          </div>
          {visible.map((resource) => {
            const Icon = resourceIcon(resource.resource_kind);
            const level = resource.access?.access_level ?? 'private';
            const selectedNames = (resource.access?.group_ids ?? []).map((id) => groupNames.get(id) ?? id);
            return (
              <div key={`${resource.instance_id}:${resource.resource_kind}:${resource.resource_id}`} className="grid gap-3 border-b border-border px-4 py-4 last:border-0 md:grid-cols-[minmax(220px,1.35fr)_170px_minmax(190px,1fr)_130px_90px] md:items-center md:px-5">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/10 text-mint"><Icon className="size-4" /></span>
                  <div className="min-w-0">
                    <div className="truncate text-[12px] font-semibold">{resource.display_name}</div>
                    <div className="truncate font-mono text-[10px] text-muted">{resource.resource_id} · {instanceNames.get(resource.instance_id) ?? resource.instance_id}</div>
                  </div>
                </div>
                <Badge variant={level === 'public' ? 'success' : level === 'scope' ? 'info' : 'secondary'}>
                  {level === 'public' ? <Building2 className="size-3" /> : level === 'scope' ? <Users className="size-3" /> : <LockKeyhole className="size-3" />}
                  {t(`organization.resources.levels.${level}`)}
                </Badge>
                <div className="truncate text-[12px] text-muted">
                  {level === 'public'
                    ? t('organization.resources.everyActiveMember')
                    : level === 'private'
                      ? t('organization.resources.ownerOnly')
                      : selectedNames.join(', ')}
                </div>
                <SyncBadge status={resource.sync.status} />
                <Button size="sm" variant="outline" onClick={() => setEditing(resource)}>{t('organization.actions.manage')}</Button>
              </div>
            );
          })}
        </TableFrame>
      ) : null}
      <ResourceAccessDialog
        resource={editing}
        groups={groups}
        onOpenChange={(open) => { if (!open) setEditing(null); }}
        onSaved={load}
      />
    </div>
  );
}
