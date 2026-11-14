import { useTranslation } from 'react-i18next';
import { Check, ChevronDown, ChevronRight, Laptop, RefreshCw, Server, WifiOff } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { Skeleton } from '../components/ui/skeleton';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../components/ui/dialog';
import { cn } from '../lib/utils';
import type { DesktopClientState } from './bridge';

export interface InstanceSwitcherViewProps {
  state: DesktopClientState;
  localInstanceId: string | null;
  busy: boolean;
  pendingId: string | null;
  failure: { instanceId: string; message: string } | null;
  offlineId: string | null;
  onOfflineChange: (id: string | null) => void;
  onSelect: (id: string) => void;
  onLocal: () => void;
  onCancel: () => void;
  onRefresh: () => void;
  onConsent: (approve: boolean) => void;
  consentError?: string | null;
}

/** ui/design: desktop/instances/instances-*, switching-*, offline-*, consent-*.
 * Native projection is rendered as supplied. Identity is never inferred from names or URLs. */
export function InstanceSwitcherView({ state, localInstanceId, busy, pendingId, failure, offlineId,
  onOfflineChange, onSelect, onLocal, onCancel, onRefresh, onConsent, consentError }: InstanceSwitcherViewProps) {
  const { t } = useTranslation();
  const current = state.connection.committed;
  const remote = state.catalog.instances.filter((item) => item.id !== localInstanceId);
  const unreachable = state.connection.status === 'remote_unreachable';
  const target = state.connection.pending ?? current;
  return <div data-instance-switcher className="mx-auto w-full max-w-2xl pt-6">
    <div className="mb-7 flex items-start justify-between gap-4"><div><h1 className="text-2xl font-semibold tracking-tight">{t('desktopClient.switchInstance')}</h1><p className="mt-3 text-sm leading-6 text-muted">{t('desktopClient.instancesBody')}</p></div>
      <Button variant="ghost" size="icon" aria-label={t('desktopClient.refreshInstances')} disabled={busy || !!pendingId} onClick={onRefresh}><RefreshCw className={cn('size-4', state.catalog.status === 'loading' && 'animate-spin')} /></Button>
    </div>
    {unreachable && <div role="alert" className="mb-5 text-sm text-destructive-ink"><p className="break-words font-medium [overflow-wrap:anywhere]">{t('desktopClient.unreachable', { name: target.name })}</p><p className="mt-2 text-muted">{t('desktopClient.offlineInstructions')}</p><Button variant="outline" className="mt-3" onClick={onLocal}>{t('desktopClient.backLocal')}</Button></div>}
    <div data-instance-list aria-busy={!!pendingId} className="relative">
      <div className="mb-5"><InstanceRow name={state.local.name.replace(/\.local$/i, '')} detail={t('desktopClient.localDetail')} selected={current.kind === 'local'} local disabled={!!pendingId} onClick={onLocal} /></div>
      {state.catalog.status === 'loading' && !remote.length ? <div className="space-y-4" aria-label={t('desktopClient.loading')}><Skeleton className="h-20 w-full rounded-xl" /><Skeleton className="h-20 w-full rounded-xl" /></div>
        : state.catalog.status === 'error' ? <div role="alert" className="text-sm text-destructive-ink">{t('desktopClient.catalogError')}<Button variant="link" onClick={onRefresh} disabled={busy}>{t('common.retry')}</Button></div>
        : !remote.length ? <div className="rounded-xl border border-edge bg-field p-5 text-sm text-muted"><p className="font-semibold text-foreground">{t('desktopClient.emptyTitle')}</p><p className="mt-2">{t('desktopClient.emptyBody')}</p></div>
        : (['owner', 'shared'] as const).map((relationship) => {
          const items = remote.filter((item) => item.relationship === relationship);
          return items.length ? <section className="mb-6" key={relationship}><h2 className="mb-3 text-sm font-medium text-muted">{t(relationship === 'owner' ? 'desktopClient.owned' : 'desktopClient.shared')}</h2><div className="space-y-3">{items.map((item) => {
            const opening = pendingId === item.id;
            const expanded = offlineId === item.id && !item.available;
            const message = failure?.instanceId === item.id ? failure.message : null;
            return <div key={item.id} data-instance-id={item.id} className={cn('relative rounded-xl', opening && 'z-10')}>
              <InstanceRow name={item.name} detail={item.relationship === 'shared' && item.owner_email ? `${item.owner_email} · ${item.host}` : item.host} selected={current.instance_id === item.id} offline={!item.available} opening={opening} expanded={expanded} disabled={!!pendingId || busy}
                onClick={() => item.available ? onSelect(item.id) : onOfflineChange(expanded ? null : item.id)} />
              {message && <div role="alert" className="px-4 py-3 text-sm leading-6 text-destructive-ink"><p>{message}</p><Button variant="outline" className="mt-2" onClick={() => onSelect(item.id)} disabled={busy || !!pendingId}>{t('common.retry')}</Button></div>}
              {expanded && <div className="px-4 py-3 text-sm leading-6 text-muted"><p>{t('desktopClient.offlineInstructions')}</p><Button variant="outline" className="mt-2" onClick={onRefresh} disabled={busy || !!pendingId}><RefreshCw className="size-4" />{t('desktopClient.refreshInstances')}</Button></div>}
            </div>;
          })}</div></section> : null;
        })}
      {pendingId && <div data-instance-mask aria-hidden="true" className="absolute inset-0 rounded-xl bg-background/40" />}
    </div>
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

function InstanceRow({ name, detail, selected, local = false, offline = false, opening = false, expanded = false, disabled, onClick }: {
  name: string; detail: string; selected: boolean; local?: boolean; offline?: boolean; opening?: boolean; expanded?: boolean; disabled: boolean; onClick: () => void;
}) {
  const { t } = useTranslation();
  return <Button variant="ghost" className={cn('h-auto min-h-20 w-full justify-start gap-4 whitespace-normal rounded-xl border px-4 py-4 text-left disabled:opacity-100 [--press-scale:1]', selected ? 'border-mint/40 bg-mint-soft' : 'border-edge bg-surface')} onClick={onClick} disabled={disabled} aria-expanded={offline ? expanded : undefined}>
    <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-edge bg-field">{local ? <Laptop className="size-5" /> : <Server className="size-5" />}</span>
    <span className="min-w-0 flex-1"><span className="block break-words text-sm font-semibold [overflow-wrap:anywhere]">{name}</span><span className={cn('mt-1 block break-words text-xs font-normal leading-5 [overflow-wrap:anywhere]', opening ? 'text-mint-ink' : 'text-muted')}>{opening ? t('desktopClient.opening') : detail}</span></span>
    <span className="flex shrink-0 items-center gap-2">{opening ? <RefreshCw className="size-4 animate-spin text-mint-ink" /> : <>{offline && <Badge variant="secondary"><WifiOff className="mr-1 size-3" />{t('desktopClient.offline')}</Badge>}{selected ? <span aria-label={t('desktopClient.current')} className="flex items-center gap-1 text-xs text-mint-ink"><Check className="size-4" />{t('desktopClient.current')}</span> : offline ? <ChevronDown className={cn('size-4 text-muted', expanded && 'rotate-180')} /> : <ChevronRight className="size-4 text-muted" />}</>}</span>
  </Button>;
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
