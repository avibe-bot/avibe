import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Download, Globe, Laptop, LogOut, RefreshCw, UserRound, WifiOff } from 'lucide-react';
import { SidebarStatusRow, StatusDot, StatusGlyph } from '../components/SidebarStatusRow';
import { VersionChip } from '../components/VersionChip';
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
};

/** Shared product footer, the desktop app's bottom row (ui/design: chrome/bottom-row): the computer
 *  it shows on the left, the desktop app's own version on the right. The container owns native
 *  state and navigation. */
export function DesktopAccountRowView({ state, menuOpen, onMenuOpenChange, onPresent, localUpdater }: DesktopAccountRowViewProps) {
  const { t } = useTranslation();
  const remote = state.connection.committed.kind === 'remote';
  const signedIn = state.account.status === 'signed_in';
  const busy = state.connection.status === 'switching';
  const offline = state.connection.status === 'remote_unreachable';
  const update = state.desktop.update.phase === 'available';
  const instance = state.connection.committed.kind === 'local'
    ? t('desktopClient.localName', { name: state.local.name }) : state.connection.committed.name;
  const label = busy ? t('desktopClient.switchLabel', { from: instance, to: state.connection.pending?.name ?? '' })
    : signedIn ? instance : t('desktopClient.signIn');
  // The status leads, as the Web's running dot does; the chevron says this opens a menu.
  const accountTrigger = <Button data-account-trigger type="button" onClick={signedIn ? undefined : () => onPresent('account')} variant="ghost" className="h-8 min-w-0 justify-start gap-1.5 rounded-md pl-0 pr-1.5 text-left text-xs [--press-scale:1]" title={label}>
          <StatusGlyph>
            {offline ? <WifiOff data-status-indicator className="size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
              : busy ? <RefreshCw data-status-indicator className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
              : <StatusDot tone="running" label={t('desktopClient.connected')} />}
          </StatusGlyph>
          <span data-account-label className="min-w-0 truncate">{label}</span>
          {signedIn && <ChevronDown data-account-chevron className={cn('size-3 shrink-0 text-muted', menuOpen ? 'rotate-0' : 'rotate-180')} />}
        </Button>;
  const status = signedIn ? <Popover open={menuOpen} onOpenChange={onMenuOpenChange}>
      <PopoverTrigger asChild>
        {accountTrigger}
      </PopoverTrigger>
      <PopoverContent data-instance-menu side="top" align="start" sideOffset={8} collisionPadding={16} className="w-80 max-w-[calc(100vw-2rem)] rounded-xl p-2">
        <div className="flex items-center gap-3 px-3 py-3"><UserRound className="size-5 shrink-0 text-muted" /><div className="min-w-0"><p className="break-all text-sm font-semibold">{state.account.identity?.email ?? t('desktopClient.signIn')}</p><p className="mt-1 text-xs text-muted">{t('desktopClient.accountService')}</p></div></div>
        <div className="border-y border-edge py-1"><Button variant="secondary" className="h-auto w-full justify-start gap-3 px-3 py-3 [--press-scale:1]" onClick={() => onPresent('instances')}><Globe className="size-4" /><span className="flex-1 text-left">{t('desktopClient.switchInstance')}</span></Button></div>
        <div className="flex items-start gap-3 px-3 py-3"><Laptop className="mt-0.5 size-4 shrink-0 text-muted" /><div><p className="text-sm">{t('desktopClient.localRemoteAccess')}</p><p className="mt-1 text-xs text-muted">{t('desktopClient.localStatus', { name: state.local.name, status: t(`desktopClient.remoteAccess.${state.local.remote_access}`) })}</p></div></div>
        {state.remote_auxiliary === 'disabled_incomplete' && <p className="px-3 pb-3 text-xs leading-5 text-muted">{t('desktopClient.auxiliaryIncomplete')}</p>}
        {signedIn && <Button variant="secondary" className="w-full justify-start gap-3 [--press-scale:1]" onClick={() => onPresent('sign-out')}><LogOut className="size-4" />{t('desktopClient.signOut')}</Button>}
      </PopoverContent>
    </Popover> : accountTrigger;
  // While it shows another computer, the desktop app's own version is the same chip as everywhere.
  // The accessible name carries the visible "v3.2.7" (label in name).
  const shown = `${t('dashboard.versionPrefix')}${state.desktop.version}`;
  const version = remote ? <VersionChip attention={update} updateDot={update} aria-label={t('desktopClient.localVersion', { version: shown })} onClick={() => onPresent('updates')}>
    {update && <Download size={12} aria-hidden="true" />}
    {shown}
  </VersionChip> : localUpdater;
  return <div data-desktop-account-row className="relative min-w-0">
    <SidebarStatusRow status={status} version={version} />
  </div>;
}
