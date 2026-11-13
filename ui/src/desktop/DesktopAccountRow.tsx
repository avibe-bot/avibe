import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Download, Globe, Laptop, LogOut, RefreshCw, UserRound, WifiOff } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from '../components/ui/popover';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { UpdatePanel } from '../components/update/UpdatePanel';
import { modelFromDesktopState } from '../components/update/updatePanelModel';
import { useDesktopClientProjection, usePresentDesktopClient } from './useDesktopClient';

/** Nonsecret footer: every account/updater action presents native-owned UI. */
export function DesktopAccountRow() {
  const { t } = useTranslation();
  const state = useDesktopClientProjection();
  const present = usePresentDesktopClient();
  const [panel, setPanel] = useState<'account' | 'update' | null>(null);
  useEffect(() => {
    const open = (event: Event) => {
      event.preventDefault();
      window.__AVIBE_DESKTOP_UPDATES_HANDLED__ = true;
      setPanel('update');
    };
    window.addEventListener(DESKTOP_OPEN_UPDATES_EVENT, open);
    return () => window.removeEventListener(DESKTOP_OPEN_UPDATES_EVENT, open);
  }, []);
  if (!state) return null;
  const signedIn = state.account.status === 'signed_in';
  const busy = state.connection.status === 'switching';
  const offline = state.connection.status === 'remote_unreachable';
  const update = state.desktop.update.phase === 'available';
  const instance = state.connection.committed.kind === 'local'
    ? t('desktopClient.localName', { name: state.local.name }) : state.connection.committed.name;
  const label = busy ? t('desktopClient.switchLabel', { from: instance, to: state.connection.pending?.name ?? '' })
    : signedIn ? instance : t('desktopClient.signIn');
  const open = (view: Parameters<typeof present>[0]) => { setPanel(null); present(view); };
  return <div data-desktop-account-row className="relative flex min-w-0 items-center gap-1">
    <Popover open={panel === 'account'} onOpenChange={(value) => setPanel(value ? 'account' : null)}>
      <PopoverTrigger asChild>
        <Button data-account-trigger variant="ghost" className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md px-0 text-left text-xs" title={label}>
          <UserRound data-account-avatar className="size-3.5 shrink-0 text-muted" />
          <span data-account-group className="flex min-w-0 flex-1 items-center gap-1.5">
            <span data-account-label className="min-w-0 truncate">{label}</span>
            {offline ? <WifiOff className="size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
              : busy ? <RefreshCw className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
              : <span data-status-indicator className="size-1.5 shrink-0 rounded-full bg-mint" aria-label={t('desktopClient.connected')} />}
            <ChevronDown data-account-chevron className="size-3 shrink-0 rotate-180 text-muted" />
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent data-instance-menu side="top" align="start" sideOffset={8} collisionPadding={16} className="w-80 max-w-[calc(100vw-2rem)] rounded-xl p-2">
        <div className="flex items-center gap-3 px-3 py-3"><UserRound className="size-5 shrink-0 text-muted" /><div className="min-w-0"><p className="break-all text-sm font-semibold">{state.account.identity?.email ?? t('desktopClient.signIn')}</p><p className="mt-1 text-xs text-muted">{t('desktopClient.accountService')}</p></div></div>
        <div className="border-y border-edge py-1"><Button variant="ghost" className="h-auto w-full justify-start gap-3 px-3 py-3" onClick={() => open(signedIn ? 'instances' : 'account')}><Globe className="size-4" /><span className="flex-1 text-left">{t(signedIn ? 'desktopClient.switchInstance' : 'desktopClient.signIn')}</span></Button></div>
        <div className="flex items-start gap-3 px-3 py-3"><Laptop className="mt-0.5 size-4 shrink-0 text-muted" /><div><p className="text-sm">{t('desktopClient.localRemoteAccess')}</p><p className="mt-1 text-xs text-muted">{t('desktopClient.localStatus', { name: state.local.name, status: t(`desktopClient.remoteAccess.${state.local.remote_access}`) })}</p></div></div>
        {state.remote_auxiliary === 'disabled_incomplete' && <p className="px-3 pb-3 text-xs leading-5 text-muted">{t('desktopClient.auxiliaryIncomplete')}</p>}
        {signedIn && <Button variant="ghost" className="w-full justify-start gap-3 text-muted" onClick={() => open('sign-out')}><LogOut className="size-4" />{t('desktopClient.signOut')}</Button>}
      </PopoverContent>
    </Popover>
    <Popover open={panel === 'update'} onOpenChange={(value) => setPanel(value ? 'update' : null)}>
      {/* Anchor to the entire row: the version stays right while the popover starts at x=16. */}
      <PopoverTrigger asChild><Button data-version-trigger variant="ghost" size="sm" className="h-8 shrink-0 gap-1 px-1.5 text-2xs text-muted" aria-label={t('desktopClient.localVersion', { version: state.desktop.version })}>
        {update ? <><Download className="size-3 text-gold-ink" /><span className="size-1.5 rounded-full bg-gold" /></> : state.desktop.version}
      </Button></PopoverTrigger>
      <PopoverAnchor className="pointer-events-none absolute inset-0" />
      {/* The same panel as the version popover (ui/design: update/panel); every action still opens native-owned UI. */}
      <PopoverContent data-update-menu side="top" align="start" sideOffset={8} collisionPadding={16} className="w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl p-0">
        <UpdatePanel
          model={modelFromDesktopState(state.desktop.update, { current: state.desktop.version, checkedAt: state.desktop.update.checkedAt ?? null })}
          actions={{
            check: { run: () => open('updates'), disabled: ['checking', 'downloading', 'installing'].includes(state.desktop.update.phase) },
            ...(update ? { update: { run: () => open('updates') } } : {}),
            close: () => setPanel(null),
          }}
        />
      </PopoverContent>
    </Popover>
  </div>;
}
