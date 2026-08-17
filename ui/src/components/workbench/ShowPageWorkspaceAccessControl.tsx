import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import {
  Building2,
  CloudOff,
  Loader2,
  LockKeyhole,
  LogIn,
  RefreshCw,
  TriangleAlert,
  Users,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Select } from '@/components/ui/select';
import {
  isRevisionConflict,
  jsonBody,
  OrganizationApiError,
  organizationRequest,
} from '@/features/organization/api/client';
import type {
  CloudManagementSession,
  OrganizationGroup,
  OrganizationResource,
  ResourceAccessLevel,
} from '@/features/organization/api/types';
import {
  organizationAuthorizationReturnPath,
  requiresResourceAccessNarrowingConfirmation,
} from '@/features/organization/policy';
import {
  buildShowPageAccessPatch,
  showPageAudienceLabelKey,
  showPageAudienceLevels,
  showPageSyncPresentation,
  type ShowPageAccess,
} from '@/lib/showPageAccess';

type ManagementGate =
  | 'idle'
  | 'loading'
  | 'authorization_required'
  | 'cloud_not_connected'
  | 'subject_mismatch'
  | 'ready'
  | 'conflict'
  | 'unreachable'
  | 'error';

type ResourceResponse = { resource: OrganizationResource };
type OrganizationAuthorizationGate = 'authorization_required' | 'subject_mismatch';

const LEVEL_ICONS = {
  private: LockKeyhole,
  public: Building2,
  scope: Users,
} satisfies Record<ResourceAccessLevel, typeof LockKeyhole>;

function gateForError(error: unknown): ManagementGate {
  if (!(error instanceof OrganizationApiError)) return 'unreachable';
  if (error.code === 'cloud_management_subject_mismatch') return 'subject_mismatch';
  if (error.status === 401) return 'authorization_required';
  if (error.status === 409 && error.code === 'cloud_management_not_connected') {
    return 'cloud_not_connected';
  }
  if (error.retryable || error.status >= 500) return 'unreachable';
  return 'error';
}

function sessionGate(session: CloudManagementSession): ManagementGate {
  if (session.connected) return 'ready';
  return session.state;
}

export function ShowPageOrganizationAuthorizationPrompt({
  gate,
  onAuthorize,
}: {
  gate: OrganizationAuthorizationGate;
  onAuthorize: () => void;
}) {
  const { t } = useTranslation();
  const subjectMismatch = gate === 'subject_mismatch';
  return (
    <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
      <span className={clsx(
        'flex min-w-0 items-start gap-1.5 text-[11px] leading-snug',
        subjectMismatch ? 'text-destructive-ink' : 'text-muted',
      )}>
        {subjectMismatch ? <TriangleAlert className="mt-0.5 size-3.5 shrink-0" /> : null}
        <span>
          {t(subjectMismatch
            ? 'chat.showPage.organizationSubjectMismatch'
            : 'chat.showPage.organizationSignInDesc')}
        </span>
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-7 shrink-0"
        onClick={onAuthorize}
      >
        <LogIn className="size-3.5" />
        {t(subjectMismatch
          ? 'chat.showPage.organizationSignInAgain'
          : 'chat.showPage.organizationSignIn')}
      </Button>
    </div>
  );
}

export function ShowPageWorkspaceAccessControl({
  access,
  active,
  sessionId,
  onConfirmationOpenChange,
  ownerWindowId,
}: {
  access: ShowPageAccess | null;
  active: boolean;
  sessionId: string;
  onConfirmationOpenChange?: (open: boolean) => void;
  /** Attribute this control's body-portalled ConfirmDialog to its owning app window. */
  ownerWindowId?: string;
}) {
  const { t } = useTranslation();
  const [gate, setGate] = useState<ManagementGate>('idle');
  const [resource, setResource] = useState<OrganizationResource | null>(null);
  const [groups, setGroups] = useState<OrganizationGroup[]>([]);
  const [level, setLevel] = useState<ResourceAccessLevel>(access?.access_level ?? 'private');
  const [groupIds, setGroupIds] = useState<string[]>(access?.group_ids ?? []);
  const [saving, setSaving] = useState(false);
  const [confirmNarrowing, setConfirmNarrowing] = useState(false);
  const generationRef = useRef(0);

  const organizationId = access?.organization_id ?? null;
  const instanceId = access?.instance_id ?? null;
  const canManage = access?.can_manage === true;
  const setNarrowingConfirmationOpen = useCallback((next: boolean) => {
    setConfirmNarrowing(next);
    onConfirmationOpenChange?.(next);
  }, [onConfirmationOpenChange]);

  const accessPath = useMemo(() => {
    if (!organizationId || !instanceId) return null;
    return `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/resources/${encodeURIComponent(instanceId)}/show_page/${encodeURIComponent(sessionId)}/access`;
  }, [instanceId, organizationId, sessionId]);

  const loadManagement = useCallback(async () => {
    if (!access || access.mode !== 'organization' || !canManage || !organizationId || !accessPath) {
      return;
    }
    const generation = ++generationRef.current;
    setGate('loading');
    try {
      const session = await organizationRequest<CloudManagementSession>('/api/cloud-management/session');
      if (generation !== generationRef.current) return;
      const nextGate = sessionGate(session);
      if (!session.connected) {
        setGate(nextGate);
        setResource(null);
        setGroups([]);
        return;
      }
      const [resourceResult, groupResult] = await Promise.all([
        organizationRequest<ResourceResponse>(accessPath),
        organizationRequest<{ groups: OrganizationGroup[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/groups`,
        ),
      ]);
      if (generation !== generationRef.current) return;
      const nextResource = resourceResult.resource;
      setResource(nextResource);
      setGroups(groupResult.groups);
      setLevel(nextResource.access?.access_level ?? access.access_level);
      setGroupIds(nextResource.access?.group_ids ?? access.group_ids);
      setGate('ready');
    } catch (error) {
      if (generation !== generationRef.current) return;
      setGate(gateForError(error));
    }
  }, [access, accessPath, canManage, organizationId]);

  useEffect(() => {
    generationRef.current += 1;
    setResource(null);
    setGroups([]);
    setLevel(access?.access_level ?? 'private');
    setGroupIds(access?.group_ids ?? []);
    setNarrowingConfirmationOpen(false);
    setGate('idle');
    if (active && access?.mode === 'organization' && access.can_manage) {
      void loadManagement();
    }
    return () => {
      generationRef.current += 1;
    };
  }, [access, active, loadManagement, setNarrowingConfirmationOpen]);

  const visibleGroups = groups.filter((group) => !group.archived_at || groupIds.includes(group.id));
  const toggleGroup = (group: OrganizationGroup) => {
    const selected = groupIds.includes(group.id);
    if (group.archived_at && !selected) return;
    setGroupIds((current) => selected
      ? current.filter((id) => id !== group.id)
      : [...current, group.id]);
  };

  const currentLevel = resource?.access?.access_level ?? access?.access_level ?? 'private';
  const currentGroupIds = resource?.access?.group_ids ?? access?.group_ids ?? [];
  const revision = resource?.access?.revision ?? access?.policy_revision ?? null;
  const patch = revision === null ? null : buildShowPageAccessPatch(level, groupIds, revision);
  const normalizedCurrentGroups = currentLevel === 'scope' ? [...currentGroupIds].sort() : [];
  const normalizedDraftGroups = patch?.group_ids ? [...patch.group_ids].sort() : [];
  const dirty = Boolean(
    patch
    && (level !== currentLevel
      || normalizedCurrentGroups.join('\u0000') !== normalizedDraftGroups.join('\u0000')),
  );
  const editable = access?.mode === 'organization' && canManage && gate === 'ready' && !saving;

  const commit = async () => {
    if (!accessPath || !patch || !dirty || !editable) return;
    setSaving(true);
    try {
      const result = await organizationRequest<ResourceResponse>(accessPath, {
        method: 'PATCH',
        body: jsonBody(patch),
      });
      setResource(result.resource);
      setLevel(result.resource.access?.access_level ?? level);
      setGroupIds(result.resource.access?.group_ids ?? []);
      setGate('ready');
    } catch (error) {
      setGate(isRevisionConflict(error) ? 'conflict' : gateForError(error));
    } finally {
      setSaving(false);
      setNarrowingConfirmationOpen(false);
    }
  };

  const save = () => {
    if (!patch || !dirty || !editable) return;
    if (requiresResourceAccessNarrowingConfirmation(
      currentLevel,
      currentGroupIds,
      patch.access_level,
      patch.group_ids,
    )) {
      setNarrowingConfirmationOpen(true);
      return;
    }
    void commit();
  };

  const startAuthorization = async () => {
    setGate('loading');
    try {
      const result = await organizationRequest<{ authorize_url: string }>(
        '/api/cloud-management/session/start',
        {
          method: 'POST',
          body: jsonBody({
            mode: 'interactive',
            next: organizationAuthorizationReturnPath(
              window.location.pathname,
              window.location.search,
            ),
          }),
        },
      );
      window.location.assign(result.authorize_url);
    } catch (error) {
      setGate(gateForError(error));
    }
  };

  const LevelIcon = LEVEL_ICONS[level];
  const syncPresentation = resource ? showPageSyncPresentation(resource.sync.status) : null;

  return (
    <>
      <section className="space-y-2.5" aria-label={t('chat.showPage.workspaceAccess')}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium">{t('chat.showPage.workspaceAccess')}</div>
          <div className="mt-0.5 text-[11px] leading-snug text-muted">
            {t('chat.showPage.workspaceAccessDesc')}
          </div>
        </div>
        {!access ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-muted" />
        ) : access.mode === 'personal' ? (
          <span className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs">
            <LockKeyhole className="size-3.5 text-muted" />
            {t(showPageAudienceLabelKey('private'))}
          </span>
        ) : (
          <Select
            value={level}
            disabled={!editable}
            onChange={(event) => setLevel(event.target.value as ResourceAccessLevel)}
            wrapperClassName="w-[148px] shrink-0"
            className="h-8 text-xs"
            aria-label={t('chat.showPage.workspaceAccess')}
          >
            {showPageAudienceLevels('organization').map((candidate) => (
              <option key={candidate} value={candidate}>
                {t(showPageAudienceLabelKey(candidate))}
              </option>
            ))}
          </Select>
        )}
      </div>

      {access?.mode === 'organization' ? (
        <div className="flex items-start gap-2 text-[11px] leading-snug text-muted">
          <LevelIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>{t(`chat.showPage.workspaceHelp.${level}`)}</span>
        </div>
      ) : null}

      {access?.mode === 'organization' && level === 'scope' && gate === 'ready' ? (
        <fieldset>
          <legend className="mb-1.5 text-[11px] font-medium">
            {t('chat.showPage.selectedGroups')}
          </legend>
          <div className="max-h-36 space-y-0.5 overflow-y-auto rounded-md border border-border p-1">
            {visibleGroups.map((group) => {
              const selected = groupIds.includes(group.id);
              const disabled = !editable || Boolean(group.archived_at && !selected);
              return (
                <button
                  key={group.id}
                  type="button"
                  role="checkbox"
                  aria-checked={selected}
                  disabled={disabled}
                  className={clsx(
                    'flex w-full items-center gap-2 rounded px-1.5 py-1.5 text-left text-xs',
                    disabled ? 'cursor-not-allowed opacity-60' : 'hover:bg-foreground/[0.04]',
                  )}
                  onClick={() => toggleGroup(group)}
                >
                  <Checkbox checked={selected} presentational />
                  <span className="min-w-0 flex-1 truncate">{group.name}</span>
                  {group.archived_at ? (
                    <span className="text-[10px] text-muted">{t('chat.showPage.archivedGroup')}</span>
                  ) : null}
                </button>
              );
            })}
          </div>
          {!patch ? (
            <p className="mt-1.5 text-[11px] text-gold-ink">{t('chat.showPage.groupRequired')}</p>
          ) : null}
        </fieldset>
      ) : null}

      {access?.mode === 'organization' && canManage && gate === 'loading' ? (
        <div className="flex items-center gap-1.5 text-[11px] text-muted">
          <Loader2 className="size-3.5 animate-spin" />
          {t('chat.showPage.loadingWorkspaceAccess')}
        </div>
      ) : null}

      {access?.mode === 'organization'
        && canManage
        && (gate === 'authorization_required' || gate === 'subject_mismatch') ? (
          <ShowPageOrganizationAuthorizationPrompt
            gate={gate}
            onAuthorize={() => void startAuthorization()}
          />
        ) : null}

      {access?.mode === 'organization' && canManage && gate === 'cloud_not_connected' ? (
        <div className="flex items-start gap-1.5 text-[11px] leading-snug text-muted">
          <CloudOff className="mt-0.5 size-3.5 shrink-0" />
          {t('chat.showPage.organizationUnavailable')}
        </div>
      ) : null}

      {access?.mode === 'organization' && canManage && ['conflict', 'unreachable', 'error'].includes(gate) ? (
        <div className="flex items-center justify-between gap-2 border-t border-border pt-2">
          <span className="text-[11px] leading-snug text-destructive-ink">
            {t(`chat.showPage.workspaceErrors.${gate}`)}
          </span>
          <Button type="button" size="icon" variant="ghost" className="size-7 shrink-0" onClick={() => void loadManagement()} aria-label={t('common.retry')} title={t('common.retry')}>
            <RefreshCw className="size-3.5" />
          </Button>
        </div>
      ) : null}

      {access?.mode === 'organization' && !canManage ? (
        <p className="text-[11px] leading-snug text-muted">
          {t('chat.showPage.workspaceReadOnly')}
        </p>
      ) : null}

      {syncPresentation ? (
        <div
          className={clsx(
            'flex items-center gap-1.5 text-[11px] leading-snug',
            syncPresentation.tone === 'error' ? 'text-destructive-ink' : 'text-gold-ink',
          )}
        >
          {syncPresentation.tone === 'error' ? (
            <TriangleAlert className="size-3.5 shrink-0" />
          ) : (
            <Loader2 className="size-3.5 shrink-0 animate-spin" />
          )}
          {t(syncPresentation.key)}
        </div>
      ) : null}

      {access?.mode === 'organization' && canManage && gate === 'ready' ? (
        <div className="flex justify-end">
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7"
            disabled={!dirty || !patch || saving}
            onClick={save}
          >
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {t('chat.showPage.applyWorkspaceAccess')}
          </Button>
        </div>
      ) : null}

      </section>
      <ConfirmDialog
        open={confirmNarrowing}
        onOpenChange={setNarrowingConfirmationOpen}
        title={t('organization.resources.narrowTitle')}
        description={t('organization.resources.narrowBody')}
        confirmLabel={t('organization.actions.saveChanges')}
        onConfirm={commit}
        windowOwnerId={ownerWindowId}
      />
    </>
  );
}
