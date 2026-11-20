import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw, WifiOff } from 'lucide-react';
import { SidebarStatusRow, StatusDot, StatusGlyph } from '../components/SidebarStatusRow';
import { Button } from '../components/ui/button';
import type { DesktopClientProjection, DesktopClientView } from './bridge';

export type DesktopAccountRowViewProps = {
  state: DesktopClientProjection;
  onPresent: (view: DesktopClientView | 'updates') => void;
  /** This computer's updater, shown while the app shows this computer's own instance. */
  localUpdater: ReactNode;
  /** The shown instance's version chip, while it is on another computer (`InstanceVersionChip`). */
  instanceVersion?: ReactNode;
};

/** ui/design: chrome/bottom-row. Shared layout with the A5 direct account entry;
 * the container owns native state and navigation. */
export function DesktopAccountRowView({ state, onPresent, localUpdater, instanceVersion }: DesktopAccountRowViewProps) {
  const { t } = useTranslation();
  const remote = state.connection.committed.kind === 'remote';
  const signedIn = state.account.status === 'signed_in';
  const busy = state.connection.status === 'switching';
  const offline = state.connection.status === 'remote_unreachable';
  const instance = state.connection.committed.kind === 'local'
    ? state.local.slug ?? t('desktopClient.thisDevice') : state.connection.committed.name;
  const label = signedIn ? instance : t('desktopClient.signIn');
  // The trigger's edge lines up with the Apps button above, and so does its glyph: the same 1px
  // edge and px-4 inset, so the hover wash never ends at the dot.
  const accountTrigger = <Button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-start gap-1.5 rounded-md border border-transparent pl-4 pr-1.5 text-left text-xs [--press-scale:1]" title={label}>
    <StatusGlyph label={label}>
      {/* The icon fills its box where the dot leaves 2 px free, so it sits 2 px left: the same clear space
          before the name in every state, and the name does not move. */}
      {offline ? <WifiOff data-status-indicator className="-ml-0.5 size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
        : busy ? <RefreshCw data-status-indicator className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
        : <StatusDot tone="running" label={t('desktopClient.connected')} />}
    </StatusGlyph>
    <span data-account-label className="min-w-0 truncate">{label}</span>
  </Button>;
  // The version beside a name is that instance's: this computer's updater beside this computer's instance,
  // the other instance's own version beside its name (ui/design: update/instance-and-app).
  const version = remote ? instanceVersion : localUpdater;
  return <div data-desktop-account-row className="relative min-w-0">
    <SidebarStatusRow status={accountTrigger} version={version} />
  </div>;
}
