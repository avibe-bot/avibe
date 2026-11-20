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
  // The glyph and the label are one group centred in the trigger, with the same inset on both
  // sides of the hover wash; a long instance name fills it and truncates. No hover tip, not even
  // for a truncated name (Max, 2026-11-20): the label is the button's name.
  const accountTrigger = <Button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')} variant="ghost" className="h-8 min-w-0 flex-1 justify-center gap-1.5 rounded-md border border-transparent px-2 text-xs [--press-scale:1]">
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
