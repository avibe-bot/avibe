import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Boxes,
  FolderTree,
  Server,
  Users,
  Workflow,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

import type {
  OrganizationInstance,
  OrganizationMember,
  OrganizationProject,
  OrganizationResource,
} from '../api/types';
import {
  EmptyState,
  ErrorBanner,
  LoadingState,
  PageHeader,
  RoleBadge,
  StatCard,
  SyncBadge,
} from '../components';
import { useOrganization } from '../context';
import { aggregateSyncStatus } from '../policy';

type OverviewData = {
  instances: OrganizationInstance[];
  members: OrganizationMember[];
  resources: OrganizationResource[];
  projects: OrganizationProject[];
};

function isUnhealthy(status: ReturnType<typeof aggregateSyncStatus>): boolean {
  return status !== 'in_sync' && status !== 'none';
}

function instanceDiagnosticsPath(instance: OrganizationInstance, canManage: boolean): string {
  if (isUnhealthy(aggregateSyncStatus(instance.policy_sync.projects))) {
    return `/admin/organization/instances/${encodeURIComponent(instance.id)}/projects`;
  }
  if (canManage && isUnhealthy(aggregateSyncStatus(instance.policy_sync.resources))) {
    return '/admin/organization/resources';
  }
  return '/admin/organization/instances';
}

export function OrganizationOverviewPage() {
  const { t } = useTranslation();
  const { detail, selectedOrganizationId, request, dataVersion } = useOrganization();
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState<string | undefined>();
  const canManage = detail?.capabilities.can_manage_organization === true;

  const load = useCallback(async () => {
    if (!selectedOrganizationId) return;
    setError(undefined);
    try {
      const [instanceResult, memberResult, resourceResult] = await Promise.all([
        request<{ instances: OrganizationInstance[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/instances`,
        ),
        canManage
          ? request<{ members: OrganizationMember[] }>(
              `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/members`,
            )
          : Promise.resolve({ members: [] }),
        canManage
          ? request<{ resources: OrganizationResource[] }>(
              `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/resources`,
            )
          : Promise.resolve({ resources: [] }),
      ]);
      const projectResults = await Promise.all(
        instanceResult.instances.map((instance) => request<{ projects: OrganizationProject[] }>(
            `/api/cloud-management/instances/${encodeURIComponent(instance.id)}/projects`,
          ).catch(() => ({ projects: [] }))),
      );
      setData({
        instances: instanceResult.instances,
        members: memberResult.members,
        resources: resourceResult.resources,
        projects: projectResults.flatMap((result) => result.projects),
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'cloud_management_unavailable');
    }
  }, [canManage, request, selectedOrganizationId]);

  useEffect(() => {
    void load();
  }, [dataVersion, load]);

  const attention = useMemo(() => {
    if (!data) return [];
    return [
      ...(canManage && (detail?.counts.invitedMembers ?? 0) > 0
        ? [{
            key: 'invites',
            title: t('organization.overview.pendingInvites', { count: detail?.counts.invitedMembers }),
            body: t('organization.overview.pendingInvitesBody', { count: detail?.counts.invitedMembers }),
            to: '/admin/organization/members',
          }]
        : []),
      ...data.instances.flatMap((instance) => {
        const projectStatus = aggregateSyncStatus(instance.policy_sync.projects);
        const resourceStatus = aggregateSyncStatus(instance.policy_sync.resources);
        return [
          ...(isUnhealthy(projectStatus) ? [{
            key: `${instance.id}:projects`,
            title: t('organization.overview.projectSyncNeedsAttention', { name: instance.slug }),
            body: t(`organization.sync.${projectStatus}`),
            to: `/admin/organization/instances/${encodeURIComponent(instance.id)}/projects`,
          }] : []),
          ...(isUnhealthy(resourceStatus) ? [{
            key: `${instance.id}:resources`,
            title: t('organization.overview.resourceSyncNeedsAttention', { name: instance.slug }),
            body: t(`organization.sync.${resourceStatus}`),
            to: canManage ? '/admin/organization/resources' : '/admin/organization/instances',
          }] : []),
        ];
      }),
    ];
  }, [canManage, data, detail?.counts, t]);

  if (!detail) return null;
  if (!data && !error) return <LoadingState rows={6} />;
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.overview')}
        title={detail.organization.name}
        titleAccessory={<RoleBadge role={detail.membership.role} />}
        description={canManage ? t('organization.overview.description') : t('organization.overview.memberDescription')}
      />
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <StatCard
              label={t('organization.nav.members')}
              value={detail.counts.members}
              detail={canManage ? t('organization.overview.memberBreakdown', {
                invited: detail.counts.invitedMembers ?? 0,
                removed: detail.counts.removedMembers ?? 0,
              }) : undefined}
              icon={<Users className="size-5" />}
            />
            <StatCard
              label={t('organization.nav.groups')}
              value={detail.counts.groups}
              detail={canManage ? t('organization.overview.groupBreakdown', { archived: detail.counts.archivedGroups ?? 0 }) : undefined}
              icon={<Boxes className="size-5" />}
            />
            <StatCard label={t('organization.nav.instances')} value={data.instances.length} icon={<Server className="size-5" />} />
            <StatCard label={t('organization.overview.projects')} value={data.projects.length} icon={<FolderTree className="size-5" />} />
            {canManage ? (
              <StatCard label={t('organization.nav.resources')} value={data.resources.length} icon={<Workflow className="size-5" />} />
            ) : null}
          </div>

          {!canManage ? (
            <div className="rounded-lg border border-cyan/30 bg-cyan/10 p-4 text-[13px] text-muted">
              <div className="font-semibold text-foreground">{t('organization.overview.memberSafeTitle')}</div>
              <div className="mt-1">{t('organization.overview.memberSafeBody')}</div>
            </div>
          ) : null}

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(340px,0.75fr)]">
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{t('organization.overview.policySync')}</CardTitle>
                <Button asChild variant="ghost" size="sm">
                  <Link to="/admin/organization/instances">{t('organization.overview.viewAllInstances')}</Link>
                </Button>
              </CardHeader>
              <CardContent className="p-0">
                {data.instances.length === 0 ? (
                  <div className="p-5"><EmptyState title={t('organization.instances.emptyTitle')} body={t('organization.instances.emptyBody')} /></div>
                ) : data.instances.map((instance) => (
                  <Link
                    key={instance.id}
                    to={instanceDiagnosticsPath(instance, canManage)}
                    className="flex items-center gap-3 border-b border-border px-5 py-4 last:border-b-0 hover:bg-foreground/[0.025]"
                  >
                    <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/10 font-bold text-mint">
                      {instance.slug.charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-semibold">{instance.slug}</div>
                      <div className="mt-0.5 truncate font-mono text-[10px] text-muted">
                        {instance.primary_url} · {t('organization.instances.accessCount', { count: instance.access_entries_count })}
                      </div>
                    </div>
                    <SyncBadge status={instance.policy_sync.status} />
                  </Link>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle className="flex items-center gap-2">
                  <AlertTriangle className="size-4 text-gold" />
                  {t('organization.overview.needsAttention')}
                </CardTitle>
                <span className="rounded-full bg-gold/15 px-2 py-0.5 text-[11px] font-semibold text-gold">{attention.length}</span>
              </CardHeader>
              <CardContent className="p-0">
                {attention.length === 0 ? (
                  <div className="p-6 text-center text-[13px] text-muted">{t('organization.overview.noAttention')}</div>
                ) : attention.map((item) => (
                  <Link key={item.key} to={item.to} className="block border-b border-border px-5 py-4 last:border-0 hover:bg-foreground/[0.025]">
                    <div className="text-[13px] font-semibold">{item.title}</div>
                    <div className="mt-0.5 text-[11px] text-muted">{item.body}</div>
                  </Link>
                ))}
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}
