import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, RefreshCw, WifiOff } from 'lucide-react';
import { SidebarStatusRow, StatusDot, StatusGlyph } from '../components/SidebarStatusRow';
import { VersionChip } from '../components/VersionChip';
import { Button } from '../components/ui/button';
import type { DesktopClientProjection, DesktopClientView } from './bridge';

export type DesktopAccountRowViewProps = {
  state: DesktopClientProjection;
  onPresent: (view: DesktopClientView | 'updates') => void;
  localUpdater: ReactNode;
};

/** ui/design: chrome/bottom-row. Shared layout with the A5 direct account entry;
 * the container owns native state and navigation. */
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
  // The glyph and the label are one group centred in the trigger, with the same inset on both
  // sides of the hover wash; a long instance name fills it and truncates. No hover tip, not even
  // for a truncated name (Max, 2026-11-20): the label is the button's name.
  const accountTrigger = <Button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-center gap-1.5 rounded-md border border-transparent px-2 text-xs [--press-scale:1]">
    <StatusGlyph label={label}>
      {offline ? <WifiOff data-status-indicator className="size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
        : busy ? <RefreshCw data-status-indicator className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
        : <StatusDot tone="running" label={t('desktopClient.connected')} />}
    </StatusGlyph>
    <span data-account-label className="min-w-0 truncate">{label}</span>
  </Button>;
  const shown = `${t('dashboard.versionPrefix')}${state.desktop.version}`;
  const version = remote ? <VersionChip attention={update} updateDot={update} aria-label={t('desktopClient.localVersion', { version: shown })} onClick={() => onPresent('updates')}>
    {update && <Download size={12} aria-hidden="true" />}
    {shown}
  </VersionChip> : localUpdater;
  return <div data-desktop-account-row className="relative min-w-0">
    <SidebarStatusRow status={accountTrigger} version={version} />
  </div>;
}
