// The instance rows the App's instance sheet and its first-use page share (ui/design:
// mobile-app/switcher/*): one row per instance, the current instance in its own block, offline
// explained in place, an open failure reported on its row, Add an instance, and the account row.
import { AlertTriangle, Check, ChevronDown, ChevronRight, Laptop, Loader2, Plus, RotateCw, Users, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { accountHeaderAvatarClassName, accountInitialFor } from '@/components/AccountAvatar';
import { Button } from '@/components/ui/button';
import type { TranslationKey } from '@/i18n/types';
import { cn } from '@/lib/utils';

import type { MobileInstance } from './bridge';

const group = 'overflow-hidden rounded-2xl border border-edge bg-surface';
const sectionLabel = 'mb-2 px-1 text-xs font-medium text-muted';

/** What a row is doing: opening, failed to open (with the reason's copy), or idle. */
export type RowStatus =
  | { kind: 'idle' }
  | { kind: 'waiting' }
  | { kind: 'opening' }
  | { kind: 'failed'; reason: TranslationKey };

function InstanceIcon({ instance, current = false }: { instance: MobileInstance; current?: boolean }) {
  const Icon = instance.relationship === 'shared' ? Users : Laptop;
  return (
    <span className={cn('grid size-10 shrink-0 place-items-center rounded-xl', current ? 'bg-mint-soft' : 'bg-field')}>
      <Icon className={cn('size-5', !instance.available ? 'text-muted' : current ? 'text-mint-ink' : 'text-foreground')} />
    </span>
  );
}

function useSubline() {
  const { t } = useTranslation();
  return (instance: MobileInstance) => {
    if (!instance.available) return t('mobileApp.sheet.offlineLine', { host: instance.host });
    if (instance.relationship === 'shared') {
      return t('mobileApp.sheet.sharedLine', {
        owner: instance.owner_email ?? t('mobileApp.instances.unknownOwner'),
        host: instance.host,
      });
    }
    return instance.host;
  };
}

/** Where you are. Tapping it returns to it: you are already here. */
export function CurrentInstance({
  instance,
  disabled,
  onClick,
}: {
  instance: MobileInstance;
  disabled?: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  const subline = useSubline();
  return (
    <section>
      <h2 className={sectionLabel}>{t('mobileApp.sheet.youreIn')}</h2>
      <button
        type="button"
        disabled={disabled}
        aria-label={t('mobileApp.sheet.returnTo', { name: instance.name })}
        className="flex w-full items-center gap-3 rounded-2xl border border-mint/50 bg-surface p-4 text-left disabled:opacity-60"
        onClick={onClick}
      >
        <InstanceIcon instance={instance} current />
        <span className="min-w-0 flex-1">
          <span className="block break-words text-base font-semibold leading-6">{instance.name}</span>
          <span className="mt-0.5 block break-all text-xs leading-5 text-muted">{subline(instance)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1 text-xs font-medium text-mint-ink">
          <Check className="size-4" />
          {t('mobileApp.instances.current')}
        </span>
      </button>
    </section>
  );
}

export function InstanceListRow({
  instance,
  status = { kind: 'idle' },
  expanded = false,
  disabled = false,
  onOpen,
  onRetry,
  onRefresh,
}: {
  instance: MobileInstance;
  status?: RowStatus;
  /** An offline row shows how to bring it online. */
  expanded?: boolean;
  disabled?: boolean;
  onOpen: () => void;
  onRetry: () => void;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const subline = useSubline();
  const offline = !instance.available;
  const body = (
    <>
      <InstanceIcon instance={instance} />
      <span className="min-w-0 flex-1">
        <span className={cn('block break-words text-sm font-semibold leading-5', offline && 'text-muted')}>
          {instance.name}
        </span>
        {status.kind === 'opening' ? (
          <span role="status" className="mt-0.5 block text-xs leading-5 text-mint-ink">
            {t('mobileApp.sheet.opening')}
          </span>
        ) : status.kind === 'failed' ? (
          <span role="alert" className="mt-0.5 block break-words text-xs leading-5 text-destructive-ink">
            {t(status.reason, { name: instance.name })}
          </span>
        ) : (
          <span className="mt-0.5 block break-all text-xs leading-5 text-muted">{subline(instance)}</span>
        )}
      </span>
    </>
  );
  return (
    <div className={cn(status.kind === 'waiting' && 'opacity-50')}>
      {status.kind === 'failed' ? (
        // The row holds its own Retry, so the row itself is not a button.
        <div className="flex min-h-16 w-full items-center gap-3 px-4 py-3 text-left">
          {body}
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            className="shrink-0 rounded-full"
            onClick={onRetry}
          >
            {t('common.retry')}
          </Button>
        </div>
      ) : (
        <button
          type="button"
          disabled={disabled || status.kind !== 'idle'}
          aria-expanded={offline ? expanded : undefined}
          className="flex min-h-16 w-full items-center gap-3 px-4 py-3 text-left"
          onClick={onOpen}
        >
          {body}
          {status.kind === 'opening' ? (
            <Loader2 className="size-4 shrink-0 animate-spin text-mint-ink" aria-hidden="true" />
          ) : offline ? (
            <ChevronDown className={cn('size-4 shrink-0 text-muted', expanded && 'rotate-180')} aria-hidden="true" />
          ) : (
            <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
          )}
        </button>
      )}
      {offline && expanded ? (
        <div role="status" className="mx-4 mb-4 rounded-xl bg-field p-3 text-xs leading-5 text-muted">
          <p className="break-words">{t('mobileApp.sheet.offlineBody', { name: instance.name })}</p>
          <Button
            type="button"
            variant="link"
            size="sm"
            disabled={disabled}
            className="mt-1 h-auto gap-1 px-0 text-xs text-foreground"
            onClick={onRefresh}
          >
            <RotateCw className="size-3" />
            {t('mobileApp.sheet.refreshList')}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function InstanceGroup({ children }: { children: ReactNode }) {
  return <div className={cn(group, 'divide-y divide-border')}>{children}</div>;
}

export function OtherInstancesHeader({ refreshing }: { refreshing: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-baseline justify-between gap-3">
      <h2 className={sectionLabel}>{t('mobileApp.sheet.others')}</h2>
      {refreshing ? (
        <span role="status" className="mb-2 flex items-center gap-1.5 px-1 text-xs text-muted">
          <Loader2 className="size-3 animate-spin" />
          {t('mobileApp.sheet.updating')}
        </span>
      ) : (
        <span className="mb-2 px-1 text-xs text-muted">{t('mobileApp.sheet.recent')}</span>
      )}
    </div>
  );
}

export function NoOtherInstances() {
  const { t } = useTranslation();
  return (
    <div className={cn(group, 'px-4 py-5')}>
      <p className="text-sm font-semibold">{t('mobileApp.sheet.noOthers')}</p>
      <p className="mt-1 text-xs leading-5 text-muted">{t('mobileApp.sheet.noOthersBody')}</p>
    </div>
  );
}

export function AddInstanceRow({ disabled, onClick }: { disabled?: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <div className={cn(group, 'mt-3')}>
      <button
        type="button"
        disabled={disabled}
        className="flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left disabled:opacity-60"
        onClick={onClick}
      >
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-field">
          <Plus className="size-5 text-mint-ink" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold">{t('mobileApp.sheet.add')}</span>
          <span className="mt-0.5 block text-xs leading-5 text-muted">{t('mobileApp.sheet.addBody')}</span>
        </span>
        <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
      </button>
    </div>
  );
}

export function AccountRow({ email, disabled, onClick }: { email: string; disabled?: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <section className="mt-8">
      <h2 className={sectionLabel}>{t('mobileApp.account.title')}</h2>
      <div className={group}>
        <button
          type="button"
          disabled={disabled}
          className="flex min-h-14 w-full items-center gap-3 px-4 py-3 text-left disabled:opacity-60"
          onClick={onClick}
        >
          <span className={cn(accountHeaderAvatarClassName, 'size-10 text-sm')} aria-hidden="true">
            {accountInitialFor(email)}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block break-all text-sm font-semibold leading-5">{email}</span>
            <span className="mt-0.5 block text-xs leading-5 text-muted">{t('mobileApp.sheet.accountBody')}</span>
          </span>
          <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}

/** A refresh that failed above a list that still works. */
export function StaleBanner({ disabled, onRetry }: { disabled?: boolean; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div role="status" className="mb-4 flex items-start gap-2.5 rounded-xl border border-gold/40 bg-gold-soft px-3 py-2.5 text-xs leading-5">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="min-w-0 flex-1">{t('mobileApp.sheet.stale')}</p>
      <Button
        type="button"
        variant="link"
        size="sm"
        disabled={disabled}
        className="h-auto shrink-0 px-0 text-xs font-semibold text-foreground"
        onClick={onRetry}
      >
        {t('common.retry')}
      </Button>
    </div>
  );
}

export function SheetHeader({ currentName, disabled, onClose }: { currentName: string | null; disabled?: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <header className="flex items-center justify-between gap-3 pb-4">
      <h1 className="text-xl font-semibold">{t('mobileApp.sheet.title')}</h1>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        disabled={disabled}
        className="rounded-full bg-field"
        aria-label={currentName ? t('mobileApp.sheet.close', { name: currentName }) : t('common.close')}
        onClick={onClose}
      >
        <X className="size-4" />
      </Button>
    </header>
  );
}
