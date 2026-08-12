import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CloudOff,
  LoaderCircle,
  Search,
  ShieldAlert,
  WifiOff,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';

import type {
  GroupColor,
  OrganizationRole,
  SyncStatus,
} from './api/types';

export function PageHeader({
  title,
  titleAccessory,
  description,
  eyebrow,
  actions,
}: {
  title: string;
  titleAccessory?: React.ReactNode;
  description?: string;
  eyebrow?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
      <div className="min-w-0">
        {eyebrow ? <div className="mb-2 text-[13px] text-muted">{eyebrow}</div> : null}
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h1 className="min-w-0 text-2xl font-semibold md:text-[30px]">{title}</h1>
          {titleAccessory}
        </div>
        {description ? <p className="mt-1.5 max-w-4xl text-[13px] leading-5 text-muted md:text-sm">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function SearchField({ value, onChange, placeholder }: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <label className="flex h-9 min-w-0 items-center gap-2 rounded-lg border border-border bg-card px-3 md:w-64">
      <Search className="size-4 shrink-0 text-muted" />
      <Input
        variant="bare"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="min-w-0 flex-1 text-[13px]"
      />
    </label>
  );
}

export function TableFrame({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={clsx('overflow-hidden rounded-lg border border-border bg-card', className)}>{children}</div>;
}

export function StatCard({ label, value, detail, icon }: {
  label: string;
  value: React.ReactNode;
  detail?: string;
  icon: React.ReactNode;
}) {
  return (
    <Card className="flex min-h-28 items-start justify-between p-4">
      <div>
        <div className="font-mono text-[10px] font-bold uppercase text-muted">{label}</div>
        <div className="mt-3 text-3xl font-semibold tabular-nums">{value}</div>
        {detail ? <div className="mt-1 text-[12px] text-muted">{detail}</div> : null}
      </div>
      <div className="grid size-9 place-items-center rounded-lg bg-mint/10 text-mint">{icon}</div>
    </Card>
  );
}

export function InitialsAvatar({ value, tone = 'mint', className }: {
  value: string;
  tone?: 'mint' | 'cyan' | 'violet' | 'gold' | 'pink';
  className?: string;
}) {
  const initial = value.trim().charAt(0).toUpperCase() || '?';
  const tones = {
    mint: 'border-mint/30 bg-mint/15 text-mint',
    cyan: 'border-cyan/30 bg-cyan/15 text-cyan',
    violet: 'border-violet/30 bg-violet/15 text-violet',
    gold: 'border-gold/30 bg-gold/15 text-gold',
    pink: 'border-pink/30 bg-pink/15 text-pink',
  };
  return (
    <span className={clsx('grid size-9 shrink-0 place-items-center rounded-lg border text-[13px] font-bold', tones[tone], className)}>
      {initial}
    </span>
  );
}

export function RoleBadge({ role }: { role: OrganizationRole }) {
  const { t } = useTranslation();
  return (
    <Badge variant={role === 'owner' ? 'success' : role === 'admin' ? 'info' : 'secondary'}>
      {t(`organization.roles.${role}`)}
    </Badge>
  );
}

const GROUP_TONES: Record<GroupColor, string> = {
  mint: 'border-mint/25 bg-mint/10 text-mint',
  cyan: 'border-cyan/25 bg-cyan/10 text-cyan',
  blue: 'border-cyan/25 bg-cyan/10 text-cyan',
  violet: 'border-violet/25 bg-violet/10 text-violet',
  rose: 'border-pink/25 bg-pink/10 text-pink',
  gold: 'border-gold/25 bg-gold/10 text-gold',
};

export function GroupPill({ name, color = 'violet' }: { name: string; color?: GroupColor | null }) {
  return (
    <span className={clsx('inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium', GROUP_TONES[color ?? 'violet'])}>
      <span className="size-1.5 rounded-full bg-current" />
      {name}
    </span>
  );
}

export function SyncBadge({ status }: { status: SyncStatus }) {
  const { t } = useTranslation();
  const normalized = status === 'pending' ? 'applying' : status;
  const variant = normalized === 'in_sync'
    ? 'success'
    : normalized === 'applying'
      ? 'warning'
      : normalized === 'error'
        ? 'destructive'
        : 'secondary';
  const Icon = normalized === 'in_sync'
    ? CheckCircle2
    : normalized === 'applying'
      ? LoaderCircle
      : normalized === 'offline'
        ? WifiOff
        : normalized === 'error'
          ? AlertTriangle
          : CloudOff;
  return (
    <Badge variant={variant}>
      <Icon className={clsx('size-3', normalized === 'applying' && 'animate-spin')} />
      {t(`organization.sync.${normalized}`)}
    </Badge>
  );
}

export function LoadingState({ rows = 5 }: { rows?: number }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3" aria-label={t('organization.states.loadingTitle')}>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-16 animate-pulse rounded-lg border border-border bg-card/70" />
      ))}
    </div>
  );
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return (
    <div className="flex min-h-64 flex-col items-center justify-center rounded-lg border border-dashed border-border px-6 text-center">
      <CloudOff className="mb-4 size-8 text-muted" />
      <h2 className="text-[15px] font-semibold">{title}</h2>
      <p className="mt-1 max-w-md text-[13px] leading-5 text-muted">{body}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

export function ForbiddenState() {
  const { t } = useTranslation();
  return (
    <EmptyState
      title={t('organization.states.unavailableTitle')}
      body={t('organization.states.unavailableBody')}
    />
  );
}

export function ErrorBanner({ code, onRetry }: { code?: string; onRetry?: () => void }) {
  const { t } = useTranslation();
  const known = [
    'forbidden',
    'organization_not_found',
    'organization_member_not_found',
    'organization_group_not_found',
    'organization_group_archived',
    'instance_not_found',
    'project_not_found',
    'resource_not_found',
    'member_email_taken',
    'group_name_taken',
    'organization_member_not_active_in_organization',
    'invalid_organization_group_name',
    'invalid_organization_group_color',
    'invalid_request',
    'too_many_entries',
    'invalid_project_access_intent',
    'invalid_project_access_principal',
    'invalid_project_access_role',
    'invalid_resource_acl_intent',
    'invalid_email',
    'invalid_domain',
    'duplicate_instance_access_principal',
    'duplicate_project_access_principal',
    'owner_entry_locked',
    'resource_sync_conflict',
    'organization_member_conflict',
    'organization_group_conflict',
  ];
  const messageKey = code && known.includes(code) ? `organization.errors.${code}` : 'organization.errors.generic';
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-destructive/35 bg-destructive/10 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div>
          <div className="text-[13px] font-semibold">{t('organization.states.errorTitle')}</div>
          <div className="mt-0.5 text-[12px] text-muted">{t(messageKey)}</div>
        </div>
      </div>
      {onRetry ? <Button variant="outline" size="sm" onClick={onRetry}>{t('common.retry')}</Button> : null}
    </div>
  );
}

export function ConflictBanner({ onReload }: { onReload: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-gold/35 bg-gold/10 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold" />
        <div>
          <div className="text-[13px] font-semibold">{t('organization.states.conflictTitle')}</div>
          <div className="mt-0.5 text-[12px] text-muted">{t('organization.states.conflictBody')}</div>
        </div>
      </div>
      <Button variant="outline" size="sm" onClick={onReload}>{t('organization.actions.reload')}</Button>
    </div>
  );
}
