import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ChevronRight,
  CircleDot,
  Link2,
  Server,
  ShieldCheck,
  Users,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';

import { OrganizationApiError } from '../api/client';
import type { OrganizationInstance } from '../api/types';
import {
  EmptyState,
  ErrorBanner,
  InitialsAvatar,
  LoadingState,
  PageHeader,
  SearchField,
  SyncBadge,
  TableFrame,
} from '../components';
import { useOrganization } from '../context';

function safeHostname(value: string): string {
  try {
    return new URL(value).hostname;
  } catch {
    return value;
  }
}

function ownerLabel(instance: OrganizationInstance, t: (key: string) => string): string {
  if (instance.owner_is_current_user) return t('organization.instances.you');
  return instance.owner_email || t('organization.instances.organizationOwner');
}

export function OrganizationInstancesPage() {
  const { t } = useTranslation();
  const { selectedOrganizationId, request, dataVersion } = useOrganization();
  const [instances, setInstances] = useState<OrganizationInstance[] | null>(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string>();

  const load = useCallback(async () => {
    if (!selectedOrganizationId) return;
    setError(undefined);
    try {
      const result = await request<{ instances: OrganizationInstance[] }>(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/instances`,
      );
      setInstances(result.instances);
    } catch (caught) {
      setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
    }
  }, [request, selectedOrganizationId]);

  useEffect(() => { void load(); }, [dataVersion, load]);

  const filtered = useMemo(() => (instances ?? []).filter((instance) => (
    `${instance.slug} ${instance.id} ${instance.public_hostname}`
      .toLowerCase()
      .includes(search.trim().toLowerCase())
  )), [instances, search]);

  if (!instances && !error) return <LoadingState />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.instances')}
        title={t('organization.nav.instances')}
        description={t('organization.instances.description')}
        actions={(
          <SearchField
            value={search}
            onChange={setSearch}
            placeholder={t('organization.instances.searchPlaceholder')}
          />
        )}
      />
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {filtered.length === 0 ? (
        <EmptyState title={t('organization.instances.emptyTitle')} body={t('organization.instances.emptyBody')} />
      ) : (
        <TableFrame>
          <div className="hidden grid-cols-[minmax(220px,1.3fr)_minmax(150px,0.85fr)_minmax(190px,1.15fr)_140px_80px_130px_32px] gap-4 border-b border-border px-5 py-3 font-mono text-[10px] font-bold uppercase text-muted lg:grid">
            <div>{t('organization.instances.columns.instance')}</div>
            <div>{t('organization.instances.columns.owner')}</div>
            <div>{t('organization.instances.columns.address')}</div>
            <div>{t('organization.instances.columns.status')}</div>
            <div>{t('organization.instances.columns.access')}</div>
            <div>{t('organization.instances.columns.sync')}</div>
            <div />
          </div>
          {filtered.map((instance, index) => (
            <Link
              key={instance.id}
              to={`/admin/organization/instances/${encodeURIComponent(instance.id)}/${instance.can_manage_access ? 'access' : 'projects'}`}
              className="grid gap-4 border-b border-border px-4 py-4 transition-colors last:border-0 hover:bg-foreground/[0.025] lg:grid-cols-[minmax(220px,1.3fr)_minmax(150px,0.85fr)_minmax(190px,1.15fr)_140px_80px_130px_32px] lg:items-center lg:px-5"
            >
              <div className="flex min-w-0 items-center gap-3">
                <span className={index % 2
                  ? 'grid size-9 shrink-0 place-items-center rounded-lg border border-violet/30 bg-violet/15 text-violet'
                  : 'grid size-9 shrink-0 place-items-center rounded-lg border border-mint/30 bg-mint/15 text-mint'}>
                  <Server className="size-4" />
                </span>
                <div className="min-w-0">
                  <div className="truncate text-[13px] font-semibold">{instance.slug}</div>
                  <div className="truncate font-mono text-[10px] text-muted">{instance.id}</div>
                </div>
              </div>
              <div className="flex min-w-0 items-center gap-2 text-[12px] text-muted">
                <InitialsAvatar value={ownerLabel(instance, t)} className="size-7 rounded-full" />
                <span className="truncate">{ownerLabel(instance, t)}</span>
              </div>
              <div className="flex min-w-0 items-center gap-2 font-mono text-[11px] text-muted">
                <Link2 className="size-3.5 shrink-0" />
                <span className="truncate">{safeHostname(instance.primary_url)}</span>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant={instance.paired ? 'success' : 'secondary'}>
                  <ShieldCheck className="size-3" />
                  {t(instance.paired ? 'organization.instances.paired' : 'organization.instances.notPaired')}
                </Badge>
                <span className="inline-flex items-center gap-1 text-[11px] text-muted">
                  <CircleDot className={instance.status === 'active' ? 'size-3 text-mint' : 'size-3'} />
                  {t(`organization.instances.status.${instance.status}`)}
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-[12px]">
                <Users className="size-3.5 text-muted" />
                {instance.access_entries_count}
              </div>
              <SyncBadge status={instance.policy_sync.status} />
              <ChevronRight className="size-4 text-muted" />
            </Link>
          ))}
        </TableFrame>
      )}
    </div>
  );
}
