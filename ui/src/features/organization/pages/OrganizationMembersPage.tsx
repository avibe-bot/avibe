import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Download,
  Mail,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserCheck,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

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
import { Select } from '@/components/ui/select';

import { isRevisionConflict, jsonBody, OrganizationApiError } from '../api/client';
import type {
  MemberStatus,
  OrganizationGroup,
  OrganizationMember,
  OrganizationRole,
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
  StatCard,
  TableFrame,
} from '../components';
import { useOrganization } from '../context';
import {
  isCurrentOrganizationLoad,
  requiresMemberRoleDowngradeConfirmation,
} from '../policy';

type MemberFilter = 'all' | MemberStatus;

function memberName(email: string): string {
  return email.split('@')[0].split(/[._+-]/).filter(Boolean).map((part) => (
    `${part.charAt(0).toUpperCase()}${part.slice(1)}`
  )).join(' ');
}

function MemberDialog({
  open,
  member,
  groups,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  member: OrganizationMember | null;
  groups: OrganizationGroup[];
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<OrganizationMember[]>;
}) {
  const { t } = useTranslation();
  const { selectedOrganizationId, request, invalidate } = useOrganization();
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Exclude<OrganizationRole, 'owner'>>('member');
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [authoritativeMember, setAuthoritativeMember] = useState<OrganizationMember | null>(null);
  const [revision, setRevision] = useState(0);
  const [downgradeConfirmOpen, setDowngradeConfirmOpen] = useState(false);
  const comparisonMemberRef = useRef<OrganizationMember | null>(null);

  useEffect(() => {
    if (!open) return;
    comparisonMemberRef.current = member;
    setEmail(member?.email ?? '');
    setRole(member?.role === 'admin' ? 'admin' : 'member');
    setGroupIds(member?.groups.map((group) => group.id) ?? []);
    setRevision(member?.member_revision ?? 0);
    setError(undefined);
    setConflict(false);
    setAuthoritativeMember(null);
    setDowngradeConfirmOpen(false);
  }, [member, open]);

  const save = async () => {
    if (!selectedOrganizationId || !email.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      const base = `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/members`;
      if (member) {
        await request(`${base}/${encodeURIComponent(member.id)}`, {
          method: 'PATCH',
          body: jsonBody({
            role,
            group_ids: groupIds,
            if_match_revision: revision,
          }),
        });
      } else {
        await request(base, {
          method: 'POST',
          body: jsonBody({ email: email.trim(), role, group_ids: groupIds }),
        });
      }
      invalidate();
      await onSaved();
      onOpenChange(false);
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        const latestMembers = await onSaved();
        setAuthoritativeMember(
          latestMembers.find((candidate) => candidate.id === member?.id) ?? null,
        );
        setConflict(true);
      } else {
        setError(caught instanceof OrganizationApiError ? caught.code : 'generic');
      }
    } finally {
      setSaving(false);
    }
  };

  const reloadAuthoritativeMember = () => {
    if (!authoritativeMember) {
      onOpenChange(false);
      return;
    }
    comparisonMemberRef.current = authoritativeMember;
    setEmail(authoritativeMember.email);
    setRole(authoritativeMember.role === 'admin' ? 'admin' : 'member');
    setGroupIds(authoritativeMember.groups.map((group) => group.id));
    setRevision(authoritativeMember.member_revision);
    setError(undefined);
    setConflict(false);
    setAuthoritativeMember(null);
  };

  const requestSave = () => {
    const comparisonMember = comparisonMemberRef.current ?? member;
    if (comparisonMember && requiresMemberRoleDowngradeConfirmation(comparisonMember.role, role)) {
      setDowngradeConfirmOpen(true);
      return;
    }
    void save();
  };

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setDowngradeConfirmOpen(false);
          onOpenChange(nextOpen);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t(member ? 'organization.members.editTitle' : 'organization.members.inviteTitle')}</DialogTitle>
            <DialogDescription>{t(member ? 'organization.members.editBody' : 'organization.members.inviteBody')}</DialogDescription>
          </DialogHeader>
          {conflict ? <ConflictBanner onReload={reloadAuthoritativeMember} /> : null}
          {error ? <ErrorBanner code={error} /> : null}
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="organization-member-email">{t('organization.fields.email')}</Label>
              <Input
                id="organization-member-email"
                type="email"
                value={email}
                disabled={Boolean(member)}
                onChange={(event) => setEmail(event.target.value)}
                placeholder={t('organization.members.emailPlaceholder')}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="organization-member-role">{t('organization.fields.role')}</Label>
              <Select id="organization-member-role" value={role} onChange={(event) => setRole(event.target.value as typeof role)}>
                <option value="member">{t('organization.roles.member')}</option>
                <option value="admin">{t('organization.roles.admin')}</option>
              </Select>
            </div>
            <fieldset>
              <legend className="mb-2 text-[12px] font-medium">{t('organization.fields.groups')}</legend>
              <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-border p-2">
                {groups.filter((group) => !group.archived_at).map((group) => (
                  <button
                    key={group.id}
                    type="button"
                    role="checkbox"
                    aria-checked={groupIds.includes(group.id)}
                    className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-foreground/[0.04]"
                    onClick={() => setGroupIds((current) => current.includes(group.id)
                      ? current.filter((id) => id !== group.id)
                      : [...current, group.id])}
                  >
                    <Checkbox checked={groupIds.includes(group.id)} presentational />
                    <GroupPill name={group.name} color={group.color} />
                  </button>
                ))}
                {groups.filter((group) => !group.archived_at).length === 0 ? (
                  <div className="px-2 py-4 text-center text-[12px] text-muted">{t('organization.groups.emptyTitle')}</div>
                ) : null}
              </div>
            </fieldset>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('common.cancel')}</Button>
            <Button variant="brand" disabled={saving || conflict || !email.trim()} onClick={requestSave}>
              {saving ? t('organization.actions.saving') : t(member ? 'organization.actions.saveChanges' : 'organization.actions.sendInvite')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={downgradeConfirmOpen}
        onOpenChange={setDowngradeConfirmOpen}
        title={t('organization.members.downgradeTitle')}
        description={t('organization.members.downgradeBody', { email: member?.email })}
        confirmLabel={t('organization.members.downgradeConfirm')}
        destructive
        onConfirm={async () => {
          await save();
          setDowngradeConfirmOpen(false);
        }}
      />
    </>
  );
}

export function OrganizationMembersPage() {
  const { t } = useTranslation();
  const {
    detail,
    session,
    selectedOrganizationId,
    request,
    dataVersion,
    invalidate,
  } = useOrganization();
  const [directory, setDirectory] = useState<{
    organizationId: string;
    members: OrganizationMember[];
    groups: OrganizationGroup[];
  } | null>(null);
  const [filter, setFilter] = useState<MemberFilter>('all');
  const [search, setSearch] = useState('');
  const [errorState, setErrorState] = useState<{ organizationId: string; code: string } | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<OrganizationMember | null>(null);
  const [removing, setRemoving] = useState<OrganizationMember | null>(null);
  const loadGeneration = useRef(0);
  const selectedOrganizationIdRef = useRef(selectedOrganizationId);
  const currentDirectory = directory?.organizationId === selectedOrganizationId ? directory : null;
  const members = currentDirectory?.members ?? null;
  const groups = currentDirectory?.groups ?? [];
  const error = errorState?.organizationId === selectedOrganizationId ? errorState.code : undefined;
  const canManage = detail?.capabilities.can_manage_organization === true;

  useLayoutEffect(() => {
    selectedOrganizationIdRef.current = selectedOrganizationId;
    loadGeneration.current += 1;
  }, [selectedOrganizationId]);

  const load = useCallback(async (): Promise<OrganizationMember[]> => {
    if (
      !selectedOrganizationId
      || selectedOrganizationIdRef.current !== selectedOrganizationId
      || detail?.organization.id !== selectedOrganizationId
      || !session
    ) return [];
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
      const memberRequest = canManage
        ? request<{ members: OrganizationMember[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/members`,
        )
        : Promise.resolve({
          members: [{
            id: detail.membership.id,
            user_id: session.user.subject,
            email: session.user.email,
            role: detail.membership.role,
            status: detail.membership.status,
            member_revision: detail.membership.member_revision,
            invited_by_user_id: null,
            created_at: detail.organization.created_at,
            updated_at: detail.organization.updated_at,
            groups: detail.membership.groups,
          }],
        });
      const [memberResult, groupResult] = await Promise.all([
        memberRequest,
        request<{ groups: OrganizationGroup[] }>(
          `/api/cloud-management/organizations/${encodeURIComponent(organizationId)}/groups`,
        ),
      ]);
      if (!isCurrent()) return [];
      setDirectory({
        organizationId,
        members: memberResult.members,
        groups: groupResult.groups,
      });
      return memberResult.members;
    } catch (caught) {
      if (!isCurrent()) return [];
      setErrorState({
        organizationId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
      return [];
    }
  }, [canManage, detail, request, selectedOrganizationId, session]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [dataVersion, load]);

  const filtered = useMemo(() => (members ?? []).filter((member) => (
    (filter === 'all' || member.status === filter)
    && member.email.toLowerCase().includes(search.trim().toLowerCase())
  )), [filter, members, search]);
  const counts = useMemo(() => ({
    active: (members ?? []).filter((member) => member.status === 'active').length,
    invited: (members ?? []).filter((member) => member.status === 'invited').length,
    admin: (members ?? []).filter((member) => member.status === 'active' && ['owner', 'admin'].includes(member.role)).length,
  }), [members]);

  const exportCsv = async () => {
    if (!selectedOrganizationId) return;
    try {
      const result = await request<{ filename: string; csv: string }>(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/members/export`,
      );
      const url = URL.createObjectURL(new Blob([result.csv], { type: 'text/csv;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setErrorState({
        organizationId: selectedOrganizationId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  };

  const removeMember = async () => {
    if (!selectedOrganizationId || !removing) return;
    try {
      await request(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/members/${encodeURIComponent(removing.id)}`,
        {
          method: 'PATCH',
          body: jsonBody({ status: 'removed', if_match_revision: removing.member_revision }),
        },
      );
      setRemoving(null);
      invalidate();
      await load();
    } catch (caught) {
      if (isRevisionConflict(caught)) {
        const latestMembers = await load();
        setRemoving((current) => (
          current
            ? latestMembers.find((member) => member.id === current.id && member.status !== 'removed') ?? null
            : null
        ));
      }
      setErrorState({
        organizationId: selectedOrganizationId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  };

  const resend = async (member: OrganizationMember) => {
    if (!selectedOrganizationId) return;
    try {
      await request(
        `/api/cloud-management/organizations/${encodeURIComponent(selectedOrganizationId)}/members/${encodeURIComponent(member.id)}/resend-invite`,
        { method: 'POST', body: jsonBody({}) },
      );
    } catch (caught) {
      setErrorState({
        organizationId: selectedOrganizationId,
        code: caught instanceof OrganizationApiError ? caught.code : 'generic',
      });
    }
  };

  if (!detail) return null;
  if (!members && !error) return <LoadingState />;
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t('organization.breadcrumb.members')}
        title={t('organization.nav.members')}
        description={canManage ? t('organization.members.description') : t('organization.members.memberDescription')}
        actions={canManage ? (
          <>
            <Button variant="outline" onClick={() => void exportCsv()}><Download className="size-4" />{t('organization.actions.exportCsv')}</Button>
            <Button variant="brand" onClick={() => { setEditing(null); setDialogOpen(true); }}><Plus className="size-4" />{t('organization.actions.inviteMember')}</Button>
          </>
        ) : undefined}
      />
      {error ? <ErrorBanner code={error} onRetry={() => void load()} /> : null}
      {canManage ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <StatCard label={t('organization.members.active')} value={counts.active} icon={<UserCheck className="size-5" />} />
          <StatCard label={t('organization.members.invited')} value={counts.invited} icon={<Mail className="size-5" />} />
          <StatCard label={t('organization.members.admins')} value={counts.admin} icon={<ShieldCheck className="size-5" />} />
        </div>
      ) : null}
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <SegmentedRadio
          value={filter}
          onChange={setFilter}
          ariaLabel={t('organization.members.filterLabel')}
          options={([
            ['all', t('organization.members.filterAll')],
            ['active', t('organization.members.active')],
            ['invited', t('organization.members.invited')],
            ['removed', t('organization.members.removed')],
          ] as Array<[MemberFilter, string]>).map(([id, label]) => ({ id, label }))}
        />
        <SearchField value={search} onChange={setSearch} placeholder={t('organization.members.searchPlaceholder')} />
      </div>
      {filtered.length === 0 ? (
        <EmptyState title={t('organization.members.emptyTitle')} body={t('organization.members.emptyBody')} />
      ) : (
        <TableFrame>
          <div className="hidden grid-cols-[minmax(240px,2fr)_140px_minmax(220px,1.4fr)_120px_132px] gap-4 border-b border-border px-5 py-3 font-mono text-[10px] font-bold uppercase text-muted md:grid">
            <span>{t('organization.fields.member')}</span><span>{t('organization.fields.role')}</span><span>{t('organization.fields.groups')}</span><span>{t('organization.fields.status')}</span><span />
          </div>
          {filtered.map((member) => (
            <div key={member.id} className="grid gap-3 border-b border-border p-4 last:border-0 md:grid-cols-[minmax(240px,2fr)_140px_minmax(220px,1.4fr)_120px_132px] md:items-center md:gap-4 md:px-5">
              <div className="flex min-w-0 items-center gap-3">
                <InitialsAvatar value={member.email} className="rounded-full" />
                <div className="min-w-0">
                  <div className="truncate text-[13px] font-semibold">{memberName(member.email)}</div>
                  <div className="truncate font-mono text-[10px] text-muted">{member.email}</div>
                </div>
              </div>
              <div><RoleBadge role={member.role} /></div>
              <div className="flex flex-wrap gap-1.5">
                {member.groups.length ? member.groups.map((group) => <GroupPill key={group.id} name={group.name} />) : <span className="text-[12px] text-muted">{t('common.none')}</span>}
              </div>
              <div>
                <Badge variant={member.status === 'active' ? 'success' : member.status === 'invited' ? 'warning' : 'secondary'}>
                  {t(`organization.status.${member.status}`)}
                </Badge>
              </div>
              <div className="flex justify-end gap-1">
                {canManage && member.status === 'invited' ? (
                  <Button size="icon" variant="ghost" aria-label={t('organization.actions.resendInvite')} onClick={() => void resend(member)}><RefreshCw className="size-4" /></Button>
                ) : null}
                {canManage && member.role !== 'owner' && member.status !== 'removed' ? (
                  <Button size="icon" variant="ghost" aria-label={t('organization.actions.editMember')} onClick={() => { setEditing(member); setDialogOpen(true); }}><Pencil className="size-4" /></Button>
                ) : null}
                {canManage && member.role !== 'owner' && member.status !== 'removed' ? (
                  <Button size="icon" variant="ghost" aria-label={t('organization.actions.removeMember')} onClick={() => setRemoving(member)}><Trash2 className="size-4 text-destructive" /></Button>
                ) : null}
              </div>
            </div>
          ))}
        </TableFrame>
      )}
      <MemberDialog
        open={dialogOpen && currentDirectory !== null}
        member={currentDirectory ? editing : null}
        groups={groups}
        onOpenChange={setDialogOpen}
        onSaved={load}
      />
      <ConfirmDialog
        open={Boolean(removing && currentDirectory)}
        onOpenChange={(open) => { if (!open) setRemoving(null); }}
        title={t('organization.members.removeTitle')}
        description={t('organization.members.removeBody', { email: removing?.email })}
        confirmLabel={t('organization.actions.removeMember')}
        destructive
        onConfirm={removeMember}
      />
    </div>
  );
}
