import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Archive,
  Boxes,
  Pencil,
  Plus,
  RotateCcw,
  Server,
  Workflow,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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
import { Textarea } from '@/components/ui/textarea';

import { isRevisionConflict, jsonBody, OrganizationApiError } from '../api/client';
import type {
  GroupColor,
  OrganizationGroup,
  OrganizationMember,
} from '../api/types';
import {
  ConflictBanner,
  EmptyState,
  ErrorBanner,
  GroupPill,
  InitialsAvatar,
  LoadingState,
  PageHeader,
  RoleBadge,
  SearchField,
  TableFrame,
} from '../components';
import { useOrganization } from '../context';
import { isCurrentOrganizationLoad } from '../policy';

type GroupFilter = 'active' | 'archived' | 'all';

const COLORS: GroupColor[] = ['mint', 'cyan', 'blue', 'violet', 'rose', 'gold'];

function GroupDialog({
  open,
  group,
  members,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  group: OrganizationGroup | null;
  members: OrganizationMember[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<OrganizationGroup | null>;
}) {
  const { t } = useTranslation();
  const { selectedOrganizationId, request, invalidate } = useOrganization();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [color, setColor] = useState<GroupColor>('violet');
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [authoritativeGroup, setAuthoritativeGroup] = useState<OrganizationGroup | null>(null);
  const [revision, setRevision] = useState(0);
  const draftKey = useRef<string | null>(null);

  useEffect(() => {
    if (!open) {
      draftKey.current = null;
      return;
    }
    const key = group?.id ?? 'new';
    if (draftKey.current === key) return;
    draftKey.current = key;
    setName(group?.name ?? '');
    setDescription(group?.description ?? '');
    setColor(group?.color ?? 'violet');
    setMemberIds(group?.members?.map((member) => member.id) ?? []);
    setRevision(group?.group_revision ?? 0);
    setError(undefined);
    setConflict(false);
    setAuthoritativeGroup(null);
  }, [group, open]);

  const save = async () => {
    if (!selectedOrganizationId || !name.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      const base = `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/groups`;
      if (group) {
        await request(`${base}/${encodeURIComponent(group.id)}`, {
          method: 'PATCH',
          body: jsonBody({
            name: name.trim(),
            description: description.trim() || null,
            color,
            if_match_revision: revision,
          }),
        });
      } else {
        await request(base, {
          method: 'POST',
          body: jsonBody({
            name: name.trim(),
            description: description.trim() || null,
            color,
            member_ids: memberIds,
          }),
        });
      }
      invalidate();
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        setAuthoritativeGroup(await onSaved());
        setConflict(true);
      } else {
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    } finally {
      setSaving(false);
    }
  };

  const reloadAuthoritativeGroup = () => {
    if (!authoritativeGroup) {
      onOpenChange(false);
      return;
    }
    setName(authoritativeGroup.name);
    setDescription(authoritativeGroup.description ?? '');
    setColor(authoritativeGroup.color ?? 'violet');
    setRevision(authoritativeGroup.group_revision);
    setError(undefined);
    setConflict(false);
    setAuthoritativeGroup(null);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t(group ? 'organization.groups.editTitle' : 'organization.groups.createTitle')}</DialogTitle>
          <DialogDescription>{t(group ? 'organization.groups.editBody' : 'organization.groups.createBody')}</DialogDescription>
        </DialogHeader>
        {conflict ? <ConflictBanner onReload={reloadAuthoritativeGroup} /> : null}
        {error ? <ErrorBanner code={error} /> : null}
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="organization-group-name">{t('organization.fields.name')}</Label>
            <Input id="organization-group-name" value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="organization-group-description">{t('organization.fields.description')}</Label>
            <Textarea id="organization-group-description" value={description} onChange={(event) => setDescription(event.target.value)} />
          </div>
          <fieldset>
            <legend className="mb-2 text-[12px] font-medium">{t('organization.fields.color')}</legend>
            <div className="flex flex-wrap gap-2">
              {COLORS.map((option) => (
                <button
                  key={option}
                  type="button"
                  className={`rounded-lg border p-1.5 ${color === option ? 'border-mint bg-mint/10' : 'border-border'}`}
                  onClick={() => setColor(option)}
                  aria-label={t(`organization.colors.${option}`)}
                >
                  <GroupPill name={t(`organization.colors.${option}`)} color={option} />
                </button>
              ))}
            </div>
          </fieldset>
          {!group && members.length ? (
            <fieldset>
              <legend className="mb-2 text-[12px] font-medium">{t('organization.groups.initialMembers')}</legend>
              <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-border p-2">
                {members.filter((member) => member.status === 'active').map((member) => (
                  <button
                    key={member.id}
                    type="button"
                    role="checkbox"
                    aria-checked={memberIds.includes(member.id)}
                    className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-foreground/[0.04]"
                    onClick={() => setMemberIds((current) => current.includes(member.id)
                      ? current.filter((id) => id !== member.id)
                      : [...current, member.id])}
                  >
                    <Checkbox checked={memberIds.includes(member.id)} presentational />
                    <span className="truncate text-[12px]">{member.email}</span>
                  </button>
                ))}
              </div>
            </fieldset>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={saving || conflict || !name.trim()} onClick={() => void save()}>
            {saving ? t('organization.actions.saving') : t('organization.actions.saveChanges')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MemberPickerDialog({
  open,
  group,
  allMembers,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  group: OrganizationGroup;
  allMembers: OrganizationMember[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<OrganizationGroup | null>;
}) {
  const { t } = useTranslation();
  const { selectedOrganizationId, request, invalidate } = useOrganization();
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [conflict, setConflict] = useState(false);
  const [authoritativeGroup, setAuthoritativeGroup] = useState<OrganizationGroup | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(group.group_revision);
  const draftGroupId = useRef<string | null>(null);

  useEffect(() => {
    if (!open) {
      draftGroupId.current = null;
      return;
    }
    if (draftGroupId.current === group.id) return;
    draftGroupId.current = group.id;
    setMemberIds(group.members?.map((member) => member.id) ?? []);
    setRevision(group.group_revision);
    setConflict(false);
    setError(undefined);
    setAuthoritativeGroup(null);
  }, [group, open]);

  const save = async () => {
    if (!selectedOrganizationId) return;
    setSaving(true);
    setError(undefined);
    try {
      await request(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/groups/${encodeURIComponent(group.id)}/members`,
        {
          method: 'PUT',
          body: jsonBody({ member_ids: memberIds, if_match_revision: revision }),
        },
      );
      invalidate();
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        setAuthoritativeGroup(await onSaved());
        setConflict(true);
        setError(undefined);
      } else {
        setConflict(false);
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    } finally {
      setSaving(false);
    }
  };

  const reloadAuthoritativeGroup = () => {
    if (!authoritativeGroup) {
      onOpenChange(false);
      return;
    }
    setMemberIds(authoritativeGroup.members?.map((member) => member.id) ?? []);
    setRevision(authoritativeGroup.group_revision);
    setConflict(false);
    setError(undefined);
    setAuthoritativeGroup(null);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('organization.groups.manageMembersTitle')}</DialogTitle>
          <DialogDescription>{t('organization.groups.manageMembersBody', { name: group.name })}</DialogDescription>
        </DialogHeader>
        {conflict ? <ConflictBanner onReload={reloadAuthoritativeGroup} /> : null}
        {error ? <ErrorBanner code={error} /> : null}
        <div className="max-h-[45vh] space-y-1 overflow-y-auto rounded-lg border border-border p-2">
          {allMembers.filter((member) => member.status === 'active').map((member) => (
            <button
              key={member.id}
              type="button"
              role="checkbox"
              aria-checked={memberIds.includes(member.id)}
              className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-foreground/[0.04]"
              onClick={() => setMemberIds((current) => current.includes(member.id)
                ? current.filter((id) => id !== member.id)
                : [...current, member.id])}
            >
              <Checkbox checked={memberIds.includes(member.id)} presentational />
              <InitialsAvatar value={member.email} className="size-8 rounded-full" />
              <span className="min-w-0 flex-1 truncate text-[12px]">{member.email}</span>
              <RoleBadge role={member.role} />
            </button>
          ))}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={saving || conflict} onClick={() => void save()}>{t('organization.actions.saveMembers')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function OrganizationGroupsPage() {
  const { t } = useTranslation();
  const { detail, selectedOrganizationId, request, dataVersion } = useOrganization();
  const [directory, setDirectory] = useState<{
    organizationId: string;
    groups: OrganizationGroup[];
    members: OrganizationMember[];
  } | null>(null);
  const [filter, setFilter] = useState<GroupFilter>('active');
  const [search, setSearch] = useState('');
  const [errorState, setErrorState] = useState<{ organizationId: string; code: string } | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const loadGeneration = useRef(0);
  const selectedOrganizationIdRef = useRef(selectedOrganizationId);
  const currentDirectory = directory?.organizationId === selectedOrganizationId ? directory : null;
  const groups = currentDirectory?.groups ?? null;
  const members = currentDirectory?.members ?? [];
  const error = errorState?.organizationId === selectedOrganizationId ? errorState.code : undefined;
  const canManage = detail?.capabilities.can_manage_organization === true;

  useLayoutEffect(() => {
    selectedOrganizationIdRef.current = selectedOrganizationId;
    loadGeneration.current += 1;
  }, [selectedOrganizationId]);

  const load = useCallback(async () => {
    if (!selectedOrganizationId) return;
    const organizationId = selectedOrganizationId;
    const generation = ++loadGeneration.current;
    const isCurrent = () => isCurrentOrganizationLoad(
      organizationId,
      selectedOrganizationIdRef.current,
      generation,
      loadGeneration.current,
    );
    setErrorState(null);
    try {
      const [groupResult, memberResult] = await Promise.all([
        request<{ groups: OrganizationGroup[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/groups`,
        ),
        canManage
          ? request<{ members: OrganizationMember[] }>(
              `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/members`,
            )
          : Promise.resolve({ members: [] }),
      ]);
      if (!isCurrent()) return;
      setDirectory({
        organizationId,
        groups: groupResult.groups,
        members: memberResult.members,
      });
    } catch (caught) {
      if (!isCurrent()) return;
      setErrorState({
        organizationId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  }, [canManage, request, selectedOrganizationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [dataVersion, load]);

  const filtered = useMemo(() => (groups ?? []).filter((group) => (
    (filter === 'all' || (filter === 'active' ? !group.archived_at : Boolean(group.archived_at)))
    && `${group.name} ${group.description ?? ''}`.toLowerCase().includes(search.trim().toLowerCase())
  )), [filter, groups, search]);

  if (!groups && !error) return <LoadingState />;
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.groups')}
        title={t('organization.nav.groups')}
        description={canManage ? t('organization.groups.description') : t('organization.groups.memberDescription')}
        actions={canManage ? <Button variant="brand" onClick={() => setDialogOpen(true)}><Plus className="size-4" />{t('organization.actions.newGroup')}</Button> : undefined}
      />
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <SegmentedRadio
          value={filter}
          onChange={setFilter}
          ariaLabel={t('organization.groups.filterLabel')}
          options={([
            ['active', t('organization.groups.active')],
            ['archived', t('organization.groups.archived')],
            ['all', t('organization.members.filterAll')],
          ] as Array<[GroupFilter, string]>).map(([id, label]) => ({ id, label }))}
        />
        <SearchField value={search} onChange={setSearch} placeholder={t('organization.groups.searchPlaceholder')} />
      </div>
      {filtered.length === 0 ? (
        <EmptyState title={t('organization.groups.emptyTitle')} body={t('organization.groups.emptyBody')} />
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((group) => (
            <Link key={group.id} to={`/admin/organization/groups/${encodeURIComponent(group.id)}`} className="rounded-lg border border-border bg-card p-4 transition hover:border-border-strong">
              <div className="flex items-start justify-between gap-3">
                <GroupPill name={group.name} color={group.color} />
                <div className="flex flex-wrap justify-end gap-1.5">
                  {!canManage && group.is_member ? <Badge variant="info">{t('organization.groups.yourGroup')}</Badge> : null}
                  <Badge variant={group.archived_at ? 'secondary' : 'success'}>{t(group.archived_at ? 'organization.groups.archived' : 'organization.groups.active')}</Badge>
                </div>
              </div>
              <p className="mt-3 min-h-10 text-[12px] leading-5 text-muted">{group.description || t('organization.groups.noDescription')}</p>
              {canManage ? (
                <div className="mt-4 grid grid-cols-4 gap-2 border-t border-border pt-3 text-center">
                  <div><div className="text-sm font-semibold">{group.member_count ?? 0}</div><div className="text-[9px] uppercase text-muted">{t('organization.nav.members')}</div></div>
                  <div><div className="text-sm font-semibold">{group.usage?.instances ?? 0}</div><div className="text-[9px] uppercase text-muted">{t('organization.nav.instances')}</div></div>
                  <div><div className="text-sm font-semibold">{group.usage?.projects ?? 0}</div><div className="text-[9px] uppercase text-muted">{t('organization.overview.projects')}</div></div>
                  <div><div className="text-sm font-semibold">{group.usage?.resources ?? 0}</div><div className="text-[9px] uppercase text-muted">{t('organization.nav.resources')}</div></div>
                </div>
              ) : null}
            </Link>
          ))}
        </div>
      )}
      <GroupDialog
        open={dialogOpen}
        group={null}
        members={members}
        onOpenChange={setDialogOpen}
        onSaved={async () => { await load(); return null; }}
      />
    </div>
  );
}

export function OrganizationGroupDetailPage() {
  const { t } = useTranslation();
  const { groupId = '' } = useParams();
  const { selectedOrganizationId, request, dataVersion, invalidate } = useOrganization();
  const [directory, setDirectory] = useState<{
    organizationId: string;
    groupId: string;
    group: OrganizationGroup;
    members: OrganizationMember[];
  } | null>(null);
  const [errorState, setErrorState] = useState<{
    organizationId: string;
    groupId: string;
    code: string;
  } | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const loadGeneration = useRef(0);
  const selectionRef = useRef({ organizationId: selectedOrganizationId, groupId });
  const currentDirectory = (
    directory?.organizationId === selectedOrganizationId
    && directory.groupId === groupId
  ) ? directory : null;
  const group = currentDirectory?.group ?? null;
  const allMembers = currentDirectory?.members ?? [];
  const error = (
    errorState?.organizationId === selectedOrganizationId
    && errorState.groupId === groupId
  ) ? errorState.code : undefined;

  useLayoutEffect(() => {
    selectionRef.current = { organizationId: selectedOrganizationId, groupId };
    loadGeneration.current += 1;
  }, [groupId, selectedOrganizationId]);

  const load = useCallback(async (): Promise<OrganizationGroup | null> => {
    if (!selectedOrganizationId || !groupId) return null;
    const organizationId = selectedOrganizationId;
    const requestedGroupId = groupId;
    const generation = ++loadGeneration.current;
    const isCurrent = () => (
      isCurrentOrganizationLoad(
        organizationId,
        selectionRef.current.organizationId,
        generation,
        loadGeneration.current,
      )
      && requestedGroupId === selectionRef.current.groupId
    );
    setErrorState(null);
    try {
      const result = await request<{ group: OrganizationGroup }>(
        `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(requestedGroupId)}`,
      );
      if (!isCurrent()) return null;
      let members: OrganizationMember[] = [];
      if (result.group.can_manage) {
        const memberResult = await request<{ members: OrganizationMember[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/members`,
        );
        if (!isCurrent()) return null;
        members = memberResult.members;
      }
      setDirectory({
        organizationId,
        groupId: requestedGroupId,
        group: result.group,
        members,
      });
      return result.group;
    } catch (caught) {
      if (!isCurrent()) return null;
      setErrorState({
        organizationId,
        groupId: requestedGroupId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
      return null;
    }
  }, [groupId, request, selectedOrganizationId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [dataVersion, load]);

  const setArchived = async (archived: boolean) => {
    if (!selectedOrganizationId || !group) return;
    const path = `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/groups/${encodeURIComponent(group.id)}`;
    try {
      await request(path, archived ? {
        method: 'DELETE',
        body: jsonBody({ if_match_revision: group.group_revision }),
      } : {
        method: 'PATCH',
        body: jsonBody({ archived: false, if_match_revision: group.group_revision }),
      });
      invalidate();
      await load();
      setArchiveOpen(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) await load();
      setErrorState({
        organizationId: selectedOrganizationId,
        groupId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  };

  if (!group && !error) return <LoadingState />;
  if (!group) return <ErrorBanner code={error} onRetry={() => void load()} />;
  const references = group.references;
  const referenceCount = (references?.instances.length ?? 0) + (references?.projects.length ?? 0) + (references?.resources.length ?? 0);
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.groupDetail', { name: group.name })}
        title={group.name}
        titleAccessory={!group.can_manage && group.is_member ? <Badge variant="info">{t('organization.groups.yourGroup')}</Badge> : undefined}
        description={group.description || t('organization.groups.noDescription')}
        actions={group.can_manage ? (
          <>
            <Button variant="outline" onClick={() => setEditOpen(true)}><Pencil className="size-4" />{t('organization.actions.editGroup')}</Button>
            {group.archived_at ? (
              <Button variant="outline" onClick={() => void setArchived(false)}><RotateCcw className="size-4" />{t('organization.actions.restoreGroup')}</Button>
            ) : (
              <Button variant="destructive-soft" onClick={() => setArchiveOpen(true)}><Archive className="size-4" />{t('organization.actions.archiveGroup')}</Button>
            )}
          </>
        ) : undefined}
      />
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {group.can_manage ? <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)]">
        <TableFrame>
          <div className="flex items-center justify-between border-b border-border px-5 py-4">
            <div className="text-[14px] font-semibold">{t('organization.nav.members')} <span className="ml-1 text-muted">{group.members?.length ?? group.member_count ?? 0}</span></div>
            {!group.archived_at ? <Button size="sm" variant="brand" onClick={() => setMembersOpen(true)}><Plus className="size-4" />{t('organization.actions.manageMembers')}</Button> : null}
          </div>
          {(group.members ?? []).length === 0 ? (
            <div className="p-5"><EmptyState title={t('organization.members.emptyTitle')} body={t('organization.members.emptyBody')} /></div>
          ) : group.members?.map((member) => (
            <div key={member.id} className="flex items-center gap-3 border-b border-border px-5 py-3 last:border-0">
              <InitialsAvatar value={member.email} className="rounded-full" />
              <div className="min-w-0 flex-1 truncate text-[12px]">{member.email}</div>
              <RoleBadge role={member.role} />
            </div>
          ))}
        </TableFrame>
        <div className="space-y-4">
            <TableFrame>
              <div className="border-b border-border px-5 py-4 text-[14px] font-semibold">
                {t('organization.groups.referencedBy')} <span className="ml-1 text-muted">{referenceCount}</span>
              </div>
              <div className="space-y-4 p-5 text-[12px]">
                <ReferenceSection icon={<Server className="size-4 text-cyan" />} title={t('organization.nav.instances')} items={references?.instances.map((item) => item.slug) ?? []} />
                <ReferenceSection icon={<Workflow className="size-4 text-gold" />} title={t('organization.overview.projects')} items={references?.projects.map((item) => item.displayName) ?? []} />
                <ReferenceSection icon={<Boxes className="size-4 text-pink" />} title={t('organization.nav.resources')} items={references?.resources.map((item) => item.displayName) ?? []} />
              </div>
            </TableFrame>
            <div className="rounded-lg border border-gold/35 bg-gold/10 p-4">
              <div className="flex items-center gap-2 text-[13px] font-semibold"><Archive className="size-4 text-gold" />{t('organization.groups.archiveImpactTitle')}</div>
              <p className="mt-1 text-[12px] leading-5 text-muted">{t('organization.groups.archiveImpactBody')}</p>
            </div>
        </div>
      </div> : null}
      <GroupDialog open={editOpen} group={group} members={allMembers} onOpenChange={setEditOpen} onSaved={load} />
      <MemberPickerDialog open={membersOpen} group={group} allMembers={allMembers} onOpenChange={setMembersOpen} onSaved={load} />
      <ConfirmDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={t('organization.groups.archiveTitle')}
        description={t('organization.groups.archiveBody', { name: group.name, count: referenceCount })}
        destructive
        confirmLabel={t('organization.actions.archiveGroup')}
        onConfirm={() => setArchived(true)}
      >
        <div className="rounded-lg border border-gold/30 bg-gold/10 p-3 text-[12px] text-muted">
          {t('organization.groups.archiveImpactBody')}
        </div>
      </ConfirmDialog>
    </div>
  );
}

function ReferenceSection({ icon, title, items }: { icon: React.ReactNode; title: string; items: string[] }) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] font-bold uppercase text-muted">{icon}{title}</div>
      {items.length ? items.slice(0, 5).map((item) => <div key={item} className="mb-1 truncate">{item}</div>) : <div className="text-muted">{t('common.none')}</div>}
      {items.length > 5 ? <div className="text-muted">{t('organization.groups.moreReferences', { count: items.length - 5 })}</div> : null}
    </div>
  );
}
