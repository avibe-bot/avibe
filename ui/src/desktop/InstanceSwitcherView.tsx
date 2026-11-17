import { useTranslation } from 'react-i18next';
import { Check, ChevronRight, Globe, Laptop, Plus, RefreshCw, UserRound, UsersRound } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Skeleton } from '../components/ui/skeleton';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog';
import { cn } from '../lib/utils';
import type { DesktopClientState, Instance } from './bridge';

export interface InstanceSwitcherViewProps {
  state: DesktopClientState;
  localInstanceId: string | null;
  busy: boolean;
  pendingId: string | null;
  failure: { instanceId: string; message: string } | null;
  onSelect: (id: string) => void;
  onLocal: () => void;
  onCancel: () => void;
  onRefresh: () => void;
  onConsent: (approve: boolean) => void;
  onAccount: () => void;
  onAdd: () => void;
  consentError?: string | null;
}

/** ui/design: desktop/instances/instances-*, switching-*, offline-*, consent-*.
 * Native owns identity and recency. Keep catalog order; never infer identity from a URL or name. */
export function InstanceSwitcherView({ state, localInstanceId, busy, pendingId, failure,
  onSelect, onLocal, onCancel, onRefresh, onConsent, onAccount, onAdd, consentError }: InstanceSwitcherViewProps) {
  const { t } = useTranslation();
  const current = state.connection.committed;
  const currentRemote = current.kind === 'remote';
  // Native validates account, ID and origin; a raw catalog match cannot replace that proof.
  const currentItem = currentRemote ? state.current_instance ?? null : null;
  const unreachable = state.connection.status === 'remote_unreachable';
  const blocked = busy || !!pendingId;
  const localItem = localInstanceId ? state.catalog.instances.find((item) => item.id === localInstanceId) : null;
  const others = state.catalog.instances.filter((item) => currentRemote ? item.id !== current.instance_id : item.id !== localInstanceId);
  const detail = (item: Instance) => [!item.available && t('desktopClient.offline'), item.relationship === 'shared' && (item.owner_email ? t('desktopClient.sharedBy', { owner: item.owner_email }) : t('desktopClient.shared')), item.host].filter(Boolean).join(' · ');
  const local = (selected: boolean) => <InstanceRow key="local" id="local" name={state.local.slug ?? t('desktopClient.thisDevice')} detail={localItem?.host ?? (state.local.slug ? t('desktopClient.thisDevice') : '')} relationship="owner" selected={selected} disabled={blocked} onClick={selected ? onCancel : onLocal} />;
  const remoteRow = (item: Instance, selected = false) => <InstanceRow key={item.id} id={item.id} name={selected ? current.name : item.name} detail={detail(selected && unreachable ? { ...item, available: false } : item)} relationship={item.relationship} selected={selected}
    offline={!item.available || (selected && unreachable)} opening={pendingId === item.id} disabled={blocked}
    error={failure?.instanceId === item.id ? failure.message : null} onClick={() => selected && failure?.instanceId !== item.id ? onCancel() : onSelect(item.id)} />;
  return <div data-instance-switcher className="mx-auto w-full max-w-2xl pt-6">
    <div className="mb-6 flex items-center justify-between gap-4"><h1 className="text-2xl font-semibold tracking-tight">{t('desktopClient.instancesTitle')}</h1>
      <Button variant="ghost" size="icon" aria-label={t('desktopClient.refreshInstances')} disabled={blocked} onClick={onRefresh}><RefreshCw className={cn('size-4', state.catalog.status === 'loading' && 'animate-spin')} /></Button>
    </div>
    {unreachable && <div role="alert" className="mb-5 text-sm text-destructive-ink"><p className="break-words font-medium [overflow-wrap:anywhere]">{t('desktopClient.unreachable', { name: current.name })}</p><p className="mt-2 text-muted">{t('desktopClient.offlineInstructions')}</p><Button variant="outline" className="mt-3" onClick={onLocal}>{t('desktopClient.backLocal')}</Button></div>}
    <div data-instance-list aria-busy={!!pendingId} className="relative space-y-6">
      <section aria-label={t('desktopClient.currentlyUsing')}><h2 className="mb-2 text-sm font-medium text-muted">{t('desktopClient.currentlyUsing')}</h2>
        <div className="overflow-hidden rounded-xl border border-mint/40 bg-surface">
          {!currentRemote ? local(true) : currentItem ? remoteRow(currentItem, true) : <InstanceRow id={current.instance_id} name={current.name} detail={current.canonical_origin} selected offline={unreachable} disabled={blocked} onClick={onCancel} />}
        </div>
      </section>
      <section aria-label={t('desktopClient.otherInstances')}><h2 className="mb-2 text-sm font-medium text-muted">{t('desktopClient.otherInstances')}</h2>
        {state.catalog.status === 'loading' && !others.length && <div className="mb-3 space-y-3" aria-label={t('desktopClient.loading')}><Skeleton className="h-20 w-full rounded-xl" /><Skeleton className="h-20 w-full rounded-xl" /></div>}
        {state.catalog.status === 'error' && <p role="alert" className="mb-3 text-sm text-destructive-ink">{t('desktopClient.catalogError')}</p>}
        {(others.length > 0 || currentRemote) ? <div data-other-instances className="divide-y divide-border overflow-hidden rounded-xl border border-edge bg-surface">
          {others.map((item) => item.id === localInstanceId ? local(false) : remoteRow(item))}
          {currentRemote && !others.some((item) => item.id === localInstanceId) && local(false)}
        </div> : state.catalog.status !== 'loading' && state.catalog.status !== 'error' && <p className="rounded-xl border border-edge bg-surface p-4 text-sm text-muted">{t('desktopClient.emptyBody')}</p>}
      </section>
      {pendingId && <div data-instance-mask aria-hidden="true" className="absolute inset-0 rounded-xl bg-background/40" />}
    </div>
    <div className="mt-6 space-y-3">
      <Button variant="ghost" disabled={blocked} className="h-auto w-full justify-start gap-3 rounded-xl border border-edge bg-surface p-4 [--press-scale:1]" onClick={onAdd}><Plus className="size-5 text-muted" /><span className="flex-1 text-left">{t('desktopClient.addInstance')}</span><ChevronRight className="size-4 text-muted" /></Button>
    </div>
    <section aria-label={t('desktopClient.accountTitle')} className="mt-6" data-instance-account>
      <h2 className="mb-2 text-sm font-medium text-muted">{t('desktopClient.accountTitle')}</h2>
      <Button variant="ghost" disabled={blocked} className="h-auto w-full justify-start gap-3 rounded-xl border border-edge bg-surface p-4 [--press-scale:1]" onClick={onAccount}><UserRound className="size-5 shrink-0 text-muted" /><span className="min-w-0 flex-1 whitespace-normal break-words text-left [overflow-wrap:anywhere]">{state.account.identity?.email ?? state.account.email ?? t('desktopClient.accountTitle')}</span><ChevronRight className="size-4 shrink-0 text-muted" /></Button>
    </section>
    <Dialog open={!!state.consent} onOpenChange={(open) => { if (!open) { if (busy) onCancel(); else onConsent(false); } }}>
      {state.consent && <DialogContent closeLabel={t('common.close')} onEscapeKeyDown={(event) => { event.preventDefault(); event.stopPropagation(); if (busy) onCancel(); else onConsent(false); }}>
        <DialogHeader><DialogTitle>{t('desktopClient.consentTitle', { name: state.consent.instance_name })}</DialogTitle><DialogDescription>{t('desktopClient.consentBody', { name: state.consent.instance_name })}</DialogDescription></DialogHeader>
        <div className="break-words text-sm leading-6 text-muted [overflow-wrap:anywhere]"><p>{state.consent.host}</p><ConsentExtras scope={state.consent.scope} /></div>
        {consentError && <p role="alert" className="text-sm text-destructive-ink">{consentError}</p>}
        <DialogFooter><Button variant="outline" disabled={busy} onClick={() => onConsent(false)}>{t('desktopClient.deny')}</Button><Button variant="brand" disabled={busy} onClick={() => onConsent(true)}>{t('desktopClient.allow')}</Button></DialogFooter>
      </DialogContent>}
    </Dialog>
  </div>;
}

function InstanceRow({ id, name, detail, relationship, selected = false, offline = false, opening = false, disabled, error, onClick }: {
  id: string; name: string; detail: string; relationship?: 'owner' | 'shared'; selected?: boolean; offline?: boolean; opening?: boolean; disabled: boolean; error?: string | null; onClick: () => void;
}) {
  const { t } = useTranslation();
  const icon = relationship === 'shared' ? <UsersRound className="size-5" /> : relationship === 'owner' ? <Laptop className="size-5" /> : <Globe className="size-5" />;
  const content = <><span data-instance-icon={relationship ?? 'unknown'} className={cn('grid size-10 shrink-0 place-items-center rounded-xl', selected && !offline ? 'bg-mint-soft text-mint-ink' : 'bg-field text-muted')}>{icon}</span>
    <span className="min-w-0 flex-1"><span className={cn("block break-words text-sm font-semibold [overflow-wrap:anywhere]", offline && "text-muted")}>{name}</span><span className="mt-1 block break-words text-xs font-normal leading-5 text-muted [overflow-wrap:anywhere]">{detail}</span>
      {opening && <span role="status" className="mt-1 block text-xs text-mint-ink">{t('desktopClient.opening')}</span>}
      {error && <span role="alert" className="mt-2 block break-words text-sm font-normal leading-6 text-destructive-ink [overflow-wrap:anywhere]">{error}</span>}
    </span></>;
  const slot = opening ? <RefreshCw className="size-4 animate-spin text-mint-ink" /> : selected ? <span aria-label={t('desktopClient.current')} className="flex items-center gap-1 text-xs text-mint-ink"><Check className="size-4" />{t('desktopClient.current')}</span> : offline ? null : <ChevronRight className="size-4 text-muted" />;
  const retry = !!error && !offline && !opening;
  return <div data-instance-id={id} data-instance-row className={cn('relative flex items-center gap-3', offline && 'text-muted', opening && 'z-10 bg-surface')}>
    {retry ? <><div className="flex min-w-0 flex-1 items-center gap-3 py-4 pl-4">{content}</div><div data-instance-action className="shrink-0 pr-4"><Button variant="outline" size="sm" disabled={disabled} onClick={onClick}>{t('common.retry')}</Button></div></>
      : <Button variant="ghost" className="h-auto min-h-20 w-full justify-start gap-3 whitespace-normal rounded-none p-4 text-left disabled:opacity-100 [--press-scale:1]" onClick={onClick} disabled={disabled || offline}>
        {content}<span data-instance-action className="shrink-0">{slot}</span>
      </Button>}
  </div>;
}

// Do not conceal additional permissions returned by a remote instance.
function ConsentExtras({ scope }: { scope: string }) {
  const { t, i18n } = useTranslation();
  const extras = [...new Set(scope.split(/\s+/).filter((token) => token && token !== 'openid' && token !== 'email'))];
  const known = extras.includes('profile') ? [t('desktopClient.consentScopes.profile')] : [];
  const unknown = extras.filter((token) => token !== 'profile');
  const list = (items: string[]) => new Intl.ListFormat(i18n.language, { type: 'conjunction' }).format(items);
  return <>{known.length > 0 && <p className="mt-2">{t('desktopClient.consentAlsoReceives', { items: list(known) })}</p>}{unknown.length > 0 && <p className="mt-2">{t('desktopClient.consentAlsoOther', { items: list(unknown) })}</p>}</>;
}
