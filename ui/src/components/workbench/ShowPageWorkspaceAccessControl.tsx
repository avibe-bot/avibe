import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CloudOff, Loader2, RefreshCw, Save, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { SegmentedRadio } from '@/components/ui/segmented';
import {
  getPermissions,
  getResourceAccess,
  isRevisionConflict,
  PermissionsApiError,
  updateResourceAccess,
} from '@/features/permissions/api';
import { requiresResourcePolicyNarrowing } from '@/features/permissions/policy';
import type {
  DirectoryGroup,
  PermissionResource,
  PermissionsResponse,
  ResourceAccessLevel,
} from '@/features/permissions/types';
import type { ShowPageAccess } from '@/lib/showPageAccess';

type Gate = 'idle' | 'loading' | 'ready' | 'conflict' | 'error';

type Draft = {
  level: ResourceAccessLevel;
  groupIds: string[];
};

const uniqueSorted = (values: string[]): string[] => [...new Set(values)].sort();

const resourceIdentityMatches = (
  resource: PermissionResource,
  sessionId: string,
  instanceId: string,
): boolean => resource.instance_id === instanceId
  && resource.resource_kind === 'show_page'
  && resource.resource_id === sessionId;

const organizationMatches = (
  permissions: PermissionsResponse,
  instanceId: string,
  organizationId: string,
): boolean => permissions.projection.instance.id === instanceId
  && permissions.projection.instance.organization?.id === organizationId;

export function ShowPageWorkspaceAccessControl({
  access,
  active,
  canManageInstance,
  sessionId,
}: {
  access: ShowPageAccess | null;
  active: boolean;
  canManageInstance: boolean;
  sessionId: string;
}) {
  const { t } = useTranslation();
  const [gate, setGate] = useState<Gate>('idle');
  const [resource, setResource] = useState<PermissionResource | null>(null);
  const [permissions, setPermissions] = useState<PermissionsResponse | null>(null);
  const [groups, setGroups] = useState<DirectoryGroup[]>([]);
  const [level, setLevel] = useState<ResourceAccessLevel>('private');
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const generationRef = useRef(0);
  const sessionIdRef = useRef(sessionId);
  const draftRef = useRef<Draft>({ level, groupIds });
  sessionIdRef.current = sessionId;
  draftRef.current = { level, groupIds };

  const instanceId = access?.instance_id ?? null;
  const organizationId = access?.organization_id ?? null;
  const ownershipConflict = access?.ownership_status === 'conflict';
  const organizationReady = access?.mode === 'organization'
    && !ownershipConflict
    && Boolean(instanceId && organizationId);

  const adopt = useCallback((next: PermissionResource, nextGroups: DirectoryGroup[]) => {
    setResource(next);
    setGroups(nextGroups);
    setLevel(next.access.access_level);
    setGroupIds(uniqueSorted(next.access.group_ids));
  }, []);

  const load = useCallback(async (
    settledGate: Gate = 'ready',
    preservedDraft?: Draft,
  ) => {
    if (!instanceId || !organizationId) return;
    const generation = ++generationRef.current;
    const requestSessionId = sessionId;
    setGate('loading');
    setErrorCode(null);
    try {
      const [nextPermissions, nextResource] = await Promise.all([
        getPermissions(),
        getResourceAccess({ resource_kind: 'show_page', resource_id: requestSessionId }),
      ]);
      if (
        generation !== generationRef.current
        || requestSessionId !== sessionIdRef.current
      ) return;
      if (
        !organizationMatches(nextPermissions, instanceId, organizationId)
        || !resourceIdentityMatches(nextResource.resource, requestSessionId, instanceId)
      ) {
        throw new Error('Show Page Workspace identity mismatch');
      }
      const directoryGroups = [...nextPermissions.projection.directory.groups];
      const knownIds = new Set(directoryGroups.map((group) => group.id));
      for (const boundId of nextResource.resource.access.group_ids) {
        if (!knownIds.has(boundId)) {
          directoryGroups.push({ id: boundId, name: boundId, archived_at: 'unknown' });
        }
      }
      setPermissions(nextPermissions);
      if (preservedDraft) {
        const archivedIds = new Set(
          directoryGroups
            .filter((group) => group.archived_at !== null)
            .map((group) => group.id),
        );
        const newlyBoundArchived = nextResource.resource.access.group_ids.filter(
          (groupId) => archivedIds.has(groupId),
        );
        setResource(nextResource.resource);
        setGroups(directoryGroups);
        setLevel(preservedDraft.level);
        setGroupIds(preservedDraft.level === 'scope'
          ? uniqueSorted([...preservedDraft.groupIds, ...newlyBoundArchived])
          : []);
      } else {
        adopt(nextResource.resource, directoryGroups);
      }
      setGate(settledGate);
    } catch (caught) {
      if (
        generation !== generationRef.current
        || requestSessionId !== sessionIdRef.current
      ) return;
      setErrorCode(caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable');
      setGate('error');
    }
  }, [adopt, instanceId, organizationId, sessionId]);

  useEffect(() => {
    generationRef.current += 1;
    setGate('idle');
    setResource(null);
    setPermissions(null);
    setGroups([]);
    setLevel('private');
    setGroupIds([]);
    setSaving(false);
    setConfirmNarrowing(false);
    setErrorCode(null);
    if (active && organizationReady) void load();
    return () => {
      generationRef.current += 1;
    };
  }, [active, load, organizationReady, sessionId]);

  const visibleGroups = useMemo(() => {
    const bound = new Set(resource?.access.group_ids ?? []);
    return groups.filter((group) => group.archived_at === null || bound.has(group.id));
  }, [groups, resource]);
  const targetGroupIds = level === 'scope' ? uniqueSorted(groupIds) : [];
  const ownerMember = useMemo(
    () => permissions?.projection.directory.members.find(
      (member) => member.id === resource?.owner_user_id,
    ) ?? null,
    [permissions, resource],
  );
  const ownerContext = useMemo(() => ({
    isInstanceOwner: resource?.owner_user_id === null
      || Boolean(
        ownerMember
        && permissions?.projection.access.owner.email
        && ownerMember.email.trim().toLowerCase()
          === permissions.projection.access.owner.email.trim().toLowerCase(),
      ),
    organizationGroupIds: ownerMember?.group_ids ?? null,
  }), [ownerMember, permissions, resource]);
  const dirty = Boolean(
    resource
    && (
      resource.access.access_level !== level
      || uniqueSorted(resource.access.group_ids).join('\u0000') !== targetGroupIds.join('\u0000')
    )
  );
  const localMutationAllowed = Boolean(
    permissions
    && !permissions.offline
    && permissions.projection.instance.local_mutation_allowed
    && permissions.projection.capabilities.includes('instance.permissions.mutate')
  );
  const editable = canManageInstance
    && localMutationAllowed
    && (gate === 'ready' || gate === 'conflict')
    && !saving;
  const invalid = level === 'scope' && targetGroupIds.length === 0;

  const commit = async () => {
    if (!resource || !instanceId || !dirty || invalid || !editable) return;
    const generation = generationRef.current;
    const requestSessionId = sessionId;
    const draft = draftRef.current;
    setSaving(true);
    setErrorCode(null);
    try {
      const result = await updateResourceAccess(
        { resource_kind: 'show_page', resource_id: requestSessionId },
        draft.level,
        draft.level === 'scope' ? uniqueSorted(draft.groupIds) : [],
        resource.access.revision,
        instanceId,
      );
      if (
        generation !== generationRef.current
        || requestSessionId !== sessionIdRef.current
      ) return;
      if (!resourceIdentityMatches(result.resource, requestSessionId, instanceId)) {
        throw new Error('Show Page Workspace identity mismatch');
      }
      adopt(result.resource, groups);
      setGate('ready');
    } catch (caught) {
      if (
        generation !== generationRef.current
        || requestSessionId !== sessionIdRef.current
      ) return;
      if (isRevisionConflict(caught)) {
        setSaving(false);
        setConfirmNarrowing(false);
        await load('conflict', draft);
        return;
      }
      setErrorCode(caught instanceof PermissionsApiError ? caught.code : 'permissions_unavailable');
      setGate('error');
    } finally {
      if (
        generation === generationRef.current
        && requestSessionId === sessionIdRef.current
      ) {
        setSaving(false);
        setConfirmNarrowing(false);
      }
    }
  };

  const save = async () => {
    if (!resource || !instanceId || !dirty || invalid || !editable) return;
    const draft = draftRef.current;
    if (requiresResourcePolicyNarrowing(
      resource.access.access_level,
      resource.access.group_ids,
      draft.level,
      draft.level === 'scope' ? uniqueSorted(draft.groupIds) : [],
      ownerContext,
    )) {
      setConfirmNarrowing(true);
      return;
    }
    await commit();
  };

  const retryLoad = useCallback(() => {
    void load('ready', resource ? draftRef.current : undefined);
  }, [load, resource]);

  return (
    <section className="space-y-2.5" aria-label={t('chat.showPage.workspaceAccess')}>
      <div>
        <div className="text-sm font-medium">{t('chat.showPage.workspaceAccess')}</div>
        <p className="mt-0.5 text-[11px] leading-snug text-muted">
          {t('chat.showPage.workspaceAccessDesc')}
        </p>
      </div>

      {access?.mode === 'configuration_unavailable' ? (
        <div className="flex items-start gap-1.5 text-[11px] leading-snug text-gold-ink">
          <CloudOff className="mt-0.5 size-3.5 shrink-0" />
          {t('chat.showPage.workspaceConfigurationUnavailable')}
        </div>
      ) : access?.mode === 'personal' || access?.mode === 'unmanaged' ? (
        <p className="text-[11px] leading-snug text-muted">
          {t(access.mode === 'personal'
            ? 'chat.showPage.workspacePersonal'
            : 'chat.showPage.workspaceUnmanaged')}
        </p>
      ) : access?.mode === 'organization_pending' ? (
        <div className="flex items-start gap-1.5 text-[11px] leading-snug text-gold-ink">
          <CloudOff className="mt-0.5 size-3.5 shrink-0" />
          {t('chat.showPage.workspacePending')}
        </div>
      ) : ownershipConflict ? (
        <div className="flex items-start gap-1.5 text-[11px] leading-snug text-destructive-ink">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
          {t('chat.showPage.workspaceOwnershipConflict')}
        </div>
      ) : gate === 'loading' || gate === 'idle' ? (
        <div className="flex h-9 items-center gap-1.5 text-[11px] text-muted">
          <Loader2 className="size-3.5 animate-spin" />
          {t('chat.showPage.loadingWorkspaceAccess')}
        </div>
      ) : resource ? (
        <>
          <SegmentedRadio<ResourceAccessLevel>
            value={level}
            onChange={(next) => {
              if (!editable) return;
              setLevel(next);
            }}
            disabled={!editable}
            ariaLabel={t('chat.showPage.workspaceAccess')}
            tone="muted"
            options={[
              { id: 'private', label: t('chat.showPage.workspaceModes.private') },
              { id: 'public', label: t('chat.showPage.workspaceModes.organization') },
              { id: 'scope', label: t('chat.showPage.workspaceModes.scope') },
            ]}
          />
          <p className="text-[11px] leading-snug text-muted">
            {t(`chat.showPage.workspaceHelp.${level}`)}
          </p>

          {level === 'scope' ? (
            <div className="space-y-1" aria-label={t('chat.showPage.workspaceGroups')}>
              {visibleGroups.length ? visibleGroups.map((group) => {
                const checked = groupIds.includes(group.id);
                const archived = group.archived_at !== null;
                return (
                  <div
                    key={group.id}
                    className="flex min-h-8 items-center gap-2 rounded-md px-1.5 py-1 text-xs"
                  >
                    <Checkbox
                      checked={checked}
                      disabled={!editable || archived}
                      onCheckedChange={(next) => {
                        if (!editable || archived) return;
                        setGroupIds((current) => uniqueSorted(
                          next
                            ? [...current, group.id]
                            : current.filter((value) => value !== group.id),
                        ));
                      }}
                      label={group.name}
                    />
                    <span className="min-w-0 flex-1 truncate">{group.name}</span>
                    {archived ? (
                      <span className="shrink-0 text-[10px] text-muted">
                        {t('chat.showPage.workspaceArchived')}
                      </span>
                    ) : null}
                  </div>
                );
              }) : (
                <p className="text-[11px] text-muted">{t('chat.showPage.workspaceNoGroups')}</p>
              )}
              {invalid ? (
                <p className="text-[11px] text-gold-ink">
                  {t('chat.showPage.workspaceGroupRequired')}
                </p>
              ) : null}
            </div>
          ) : null}

          {gate === 'conflict' ? (
            <div className="flex items-start gap-1.5 text-[11px] leading-snug text-gold-ink">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
              {t('chat.showPage.workspaceRevisionConflict')}
            </div>
          ) : null}
          {!localMutationAllowed ? (
            <p className="text-[11px] leading-snug text-muted">
              {t(permissions?.offline
                ? 'chat.showPage.workspaceOffline'
                : 'chat.showPage.workspaceReadOnly')}
            </p>
          ) : !canManageInstance ? (
            <p className="text-[11px] leading-snug text-muted">
              {t('chat.showPage.workspaceOwnerOnly')}
            </p>
          ) : null}
          {resource.sync.status !== 'in_sync' ? (
            <p className="text-[11px] leading-snug text-muted">
              {t(`chat.showPage.workspaceSync.${resource.sync.status}`)}
            </p>
          ) : null}
          <div className="flex justify-end">
            <Button
              type="button"
              size="sm"
              className="h-7"
              disabled={!dirty || invalid || !editable}
              onClick={() => void save()}
            >
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
              {t('chat.showPage.applyWorkspaceAccess')}
            </Button>
          </div>
        </>
      ) : null}

      {gate === 'error' ? (
        <div className="flex items-start justify-between gap-2 text-[11px] leading-snug text-destructive-ink">
          <span>{t(`permissions.errors.${errorCode}`, {
            defaultValue: t('chat.showPage.workspaceLoadError'),
          })}</span>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-7 shrink-0"
            onClick={retryLoad}
            aria-label={t('common.retry')}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setConfirmNarrowing}
        title={t('chat.showPage.workspaceNarrowTitle')}
        description={t('chat.showPage.workspaceNarrowBody')}
        confirmLabel={t('chat.showPage.applyWorkspaceAccess')}
        confirmDisabled={!editable}
        onConfirm={commit}
      />
    </section>
  );
}
