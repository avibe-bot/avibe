import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Download, Globe, Laptop, LogOut, RefreshCw, UserRound, WifiOff } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/popover';
import { cn } from '../lib/utils';
import type { DesktopClientProjection, DesktopClientView } from './bridge';

export type DesktopAccountRowViewProps = {
  state: DesktopClientProjection;
  menuOpen: boolean;
  onMenuOpenChange: (open: boolean) => void;
  onPresent: (view: DesktopClientView | 'updates') => void;
  localUpdater: ReactNode;
  localRemoteUrl?: string | null;
};

/** Shared product footer; the container owns native state and navigation. */
export function DesktopAccountRowView({ state, menuOpen, onMenuOpenChange, onPresent, localUpdater, localRemoteUrl }: DesktopAccountRowViewProps) {
  const { t } = useTranslation();
  const remote = state.connection.committed.kind === 'remote';
  const signedIn = state.account.status === 'signed_in';
  const busy = state.connection.status === 'switching';
  const offline = state.connection.status === 'remote_unreachable';
  const update = state.desktop.update.phase === 'available';
  const instance = state.connection.committed.kind === 'local'
    ? state.local.name.replace(/\.local$/i, '') : state.connection.committed.name;
  const label = signedIn ? instance : t('desktopClient.signIn');
  const accountTrigger = <Button data-account-trigger type="button" onClick={signedIn ? undefined : () => onPresent('account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md px-2 text-left text-xs [--press-scale:1]" title={signedIn ? (remote ? state.connection.committed.name : state.local.name) : label}>
          {signedIn ? remote ? <Globe data-account-avatar className="size-3.5 shrink-0 text-muted" /> : <Laptop data-account-avatar className="size-3.5 shrink-0 text-muted" /> : <UserRound data-account-avatar className="size-3.5 shrink-0 text-muted" />}
          <span data-account-group className="flex min-w-0 flex-1 items-center gap-1.5">
            <span data-account-label className="min-w-0 truncate">{label}</span>
            {offline ? <WifiOff className="size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
              : busy ? <RefreshCw className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
              : <span data-status-indicator className="size-1.5 shrink-0 rounded-full bg-mint" aria-label={t('desktopClient.connected')} />}
            {signedIn && <ChevronDown data-account-chevron className={cn('size-3 shrink-0 text-muted transition-transform duration-200 motion-reduce:transition-none', menuOpen ? 'rotate-0' : 'rotate-180')} />}
          </span>
        </Button>;
  return <div data-desktop-account-row className="relative flex min-w-0 items-center gap-1">
    {signedIn ? <Popover open={menuOpen} onOpenChange={onMenuOpenChange}>
      <PopoverTrigger asChild>
        {accountTrigger}
      </PopoverTrigger>
      <PopoverContent data-instance-menu side="top" align="start" sideOffset={8} collisionPadding={16} className="w-80 max-w-[calc(100vw-2rem)] rounded-xl p-2">
        <div className="flex items-start gap-3 border border-transparent px-3 py-3"><UserRound className="size-4 shrink-0 text-muted" /><div className="min-w-0"><p className="break-words text-sm font-semibold [overflow-wrap:anywhere]">{state.account.identity?.email ?? t('desktopClient.signIn')}</p>
          {state.local.remote_access === 'on' && localRemoteUrl && <a className="mt-1 block break-all text-xs text-muted underline underline-offset-4 hover:text-foreground" href={localRemoteUrl} target="_blank" rel="noreferrer">{localRemoteUrl}</a>}
        </div></div>
        <div className="space-y-1 border-t border-edge pt-2">
          <Button variant="secondary" className="h-auto w-full justify-start gap-3 px-3 py-3 [--press-scale:1]" onClick={() => onPresent('instances')}><Globe className="size-4 shrink-0" /><span className="flex-1 text-left">{t('desktopClient.switchInstance')}</span></Button>
          <Button variant="secondary" className="h-auto w-full justify-start gap-3 px-3 py-3 [--press-scale:1]" onClick={() => onPresent('sign-out')}><LogOut className="size-4 shrink-0" />{t('desktopClient.signOut')}</Button>
        </div>
        {state.remote_auxiliary === 'disabled_incomplete' && <p className="px-3 pt-3 text-xs leading-5 text-muted">{t('desktopClient.auxiliaryIncomplete')}</p>}
      </PopoverContent>
    </Popover> : accountTrigger}
    <div data-version-trigger className="shrink-0">
      {remote ? <Button variant="ghost" size="sm" className="h-8 gap-1 px-1.5 text-2xs text-muted" aria-label={t('desktopClient.localVersion', { version: state.desktop.version })} onClick={() => onPresent('updates')}>
        {update ? <><Download className="size-3 text-gold-ink" /><span className="size-1.5 rounded-full bg-gold" /></> : state.desktop.version}
      </Button> : localUpdater}
    </div>
  </div>;
}
