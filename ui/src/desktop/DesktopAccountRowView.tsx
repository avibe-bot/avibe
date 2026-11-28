import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw, WifiOff } from 'lucide-react';
import { SidebarStatusRow, StatusDot } from '../components/SidebarStatusRow';
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
  // Below the sidebar footer's line (Max, 2026-11-22 12:51; `AppShellFooter`): no fill until the pointer is
  // on it, then the Settings button's wash over a 28 px area with 8 px corners, as on the version beside
  // it; the 32 px row centres it. From the left, the status dot, then the name. The dot is centred in the
  // button and as far from its left edge as from its top and bottom, 10 px each (Max, 2026-11-22 13:15).
  // A long name truncates; no hover tip, not even for a truncated name (Max, 2026-11-20): the label is
  // the button's name.
  // One click, Enter or Space goes straight to the page: Instances when signed in (owner, 2026-11-28, as
  // on the iPhone), sign-in otherwise. Nothing opens in between, so the button promises no menu: no
  // chevron and no expansion state. The account and its sign-out are on the Instances page.
  const accountTrigger = <button data-account-trigger type="button" onClick={() => onPresent(signedIn ? 'instances' : 'account')}
    className="flex h-7 min-w-0 flex-1 items-center gap-1.5 rounded-md pl-2.5 pr-3 text-left text-xs font-medium text-foreground transition-colors hover:bg-field-hover active:bg-field-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background">
    {/* A fixed 10 px slot centred on the button, not on the name's letters. The icon fills its box where
        the dot leaves 2 px free, so it sits 2 px left: the same clear space before the name in every
        state, and the name does not move. */}
    <span data-status-glyph className="flex w-2.5 shrink-0 items-center [&_svg]:size-2.5">
      {offline ? <WifiOff data-status-indicator className="-ml-0.5 size-3 shrink-0 text-gold-ink" aria-label={t('desktopClient.disconnected')} />
        : busy ? <RefreshCw data-status-indicator className="size-3 shrink-0 animate-spin text-cyan-ink" aria-label={t('desktopClient.connecting')} />
        : <StatusDot tone="running" label={t('desktopClient.connected')} />}
    </span>
    <span data-account-label className="min-w-0 truncate">{label}</span>
  </button>;
  // The version beside a name is that instance's: this computer's updater beside this computer's instance,
  // the other instance's own version beside its name (ui/design: update/instance-and-app).
  const version = remote ? instanceVersion : localUpdater;
  return <div data-desktop-account-row className="relative min-w-0">
    <SidebarStatusRow status={accountTrigger} version={version} />
  </div>;
}
