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
  // The trigger's edge lines up with the Apps button above, and so does its glyph: the same 1px
  // edge and px-4 inset, so the hover wash never ends at the dot.
  const accountTrigger = <Button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md border border-transparent pl-4 pr-1.5 text-left text-xs [--press-scale:1]" title={label}>
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
