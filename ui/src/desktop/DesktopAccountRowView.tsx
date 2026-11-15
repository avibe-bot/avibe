import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, Globe, Laptop, RefreshCw, UserRound, WifiOff } from 'lucide-react';
import { Button } from '../components/ui/button';
import type { DesktopClientProjection, DesktopClientView } from './bridge';

export type DesktopAccountRowViewProps = {
  state: DesktopClientProjection;
  onPresent: (view: DesktopClientView | 'updates') => void;
  localUpdater: ReactNode;
};

/** Shared product footer; the container owns native state and navigation. */
export function DesktopAccountRowView({ state, onPresent, localUpdater }: DesktopAccountRowViewProps) {
  const { t } = useTranslation();
  const remote = state.connection.committed.kind === 'remote';
  const signedIn = state.account.status === 'signed_in';
  const busy = state.connection.status === 'switching';
  const offline = state.connection.status === 'remote_unreachable';
  const update = state.desktop.update.phase === 'available';
  const instance = state.connection.committed.kind === 'local'
    ? state.local.slug ?? t('desktopClient.thisDevice') : state.connection.committed.name;
  const label = signedIn ? instance : t('desktopClient.signIn');
  const accountTrigger = <Button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md px-2 text-left text-xs [--press-scale:1]" title={label}>
          {signedIn ? remote ? <Globe data-account-avatar className="size-3.5 shrink-0 text-muted" /> : <Laptop data-account-avatar className="size-3.5 shrink-0 text-muted" /> : <UserRound data-account-avatar className="size-3.5 shrink-0 text-muted" />}
          <span data-account-group className="flex min-w-0 flex-1 items-center gap-1.5">
            <span data-account-label className="min-w-0 truncate">{label}</span>
            {offline ? <WifiOff className="size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
              : busy ? <RefreshCw className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
              : <span data-status-indicator className="size-1.5 shrink-0 rounded-full bg-mint" aria-label={t('desktopClient.connected')} />}

          </span>
        </Button>;
  return <div data-desktop-account-row className="relative flex min-w-0 items-center gap-1">
    {accountTrigger}
    <div data-version-trigger className="shrink-0">
      {remote ? <Button variant="ghost" size="sm" className="h-8 gap-1 px-1.5 text-2xs text-muted" aria-label={t('desktopClient.localVersion', { version: state.desktop.version })} onClick={() => onPresent('updates')}>
        {update ? <><Download className="size-3 text-gold-ink" /><span className="size-1.5 rounded-full bg-gold" /></> : state.desktop.version}
      </Button> : localUpdater}
    </div>
  </div>;
}
