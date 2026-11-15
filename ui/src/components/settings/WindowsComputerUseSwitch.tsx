import { useEffect, useRef, useState } from 'react';
import type { FC } from 'react';
import { CircleAlert, Info, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Switch } from '@/components/ui/switch';
import { SettingsPanel } from './SettingsPrimitives';

/** How long a change may take to show up in the status before the switch gives it up. */
export const COMPUTER_USE_SWITCH_SETTLE_MS = 15_000;

/** `confirmFrom`: the first read that may confirm; `null` until the native call settles. */
type Pending = { target: boolean; startedAt: number; confirmFrom: number | null };

/**
 * Windows Computer Use on/off (ui/design: settings/computer-use/windows-*).
 *
 * The switch shows the opt-in the server reports (`enabled`), never one
 * inferred from the readiness status. A change is confirmed only when a later
 * read reports the target; until then the switch holds the target, disabled,
 * for at most {@link COMPUTER_USE_SWITCH_SETTLE_MS}. A rejected or unconfirmed
 * change returns to the server's value with a note. `enable`/`disable` are
 * `null` where this page cannot perform them (a browser, an older shell).
 */
export const WindowsComputerUseSwitch: FC<{
  enabled: boolean | null;
  /** Start order of the read that produced `enabled`. */
  readSeq: number;
  /** The start order the next status read will get. */
  nextReadSeq: () => number;
  enable: (() => Promise<void>) | null;
  disable: (() => Promise<void>) | null;
  /** Re-read the status now; the page keeps polling regardless. */
  refresh: () => void;
}> = ({ enabled, readSeq, nextReadSeq, enable, disable, refresh }) => {
  const { t } = useTranslation();
  const [change_, setPending] = useState<Pending | null>(null);
  // A change ends when a read that began after its native call reports the target.
  const confirmed = change_ !== null && change_.confirmFrom !== null
    && readSeq >= change_.confirmFrom && enabled === change_.target;
  const pending = confirmed ? null : change_;
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  // An unconfirmed change gives up after the bound.
  useEffect(() => {
    if (!pending) return;
    const left = pending.startedAt + COMPUTER_USE_SWITCH_SETTLE_MS - Date.now();
    const timer = window.setTimeout(() => {
      if (!mounted.current) return;
      setPending(null);
      setFailed(true);
    }, Math.max(0, left));
    return () => window.clearTimeout(timer);
  }, [pending]);

  const change = (next: boolean) => {
    const action = next ? enable : disable;
    if (!action || pending) return;
    setFailed(false);
    setPending({ target: next, startedAt: Date.now(), confirmFrom: null });
    void action().then(
      () => {
        if (!mounted.current) return;
        // Reads already under way may predate the change; only a later one confirms it.
        const confirmFrom = nextReadSeq();
        setPending((current) => current && { ...current, confirmFrom });
        refresh();
      },
      () => {
        if (!mounted.current) return;
        setPending(null);
        setFailed(true);
        refresh();
      },
    );
  };

  const known = enabled !== null;
  const checked = pending ? pending.target : enabled === true;
  const action = checked ? disable : enable;
  const disabled = pending !== null || !known || action === null;
  const trayOnly = !pending && enabled === true && enable !== null && disable === null;

  // Opt-in, not readiness: the status band above says whether it is ready.
  const stateText = pending
    ? t(pending.target ? 'settings.computerUse.windows.switch.turningOn' : 'settings.computerUse.windows.switch.turningOff')
    : !known
      ? t('settings.computerUse.windows.switch.stateUnknown')
      : enabled
        ? t('settings.computerUse.windows.switch.stateOn')
        : t('settings.computerUse.windows.switch.stateOff');

  return (
    <SettingsPanel
      variant="preference"
      title={t('settings.computerUse.title')}
      description={(
        <span className="flex flex-col gap-1">
          <span>{t('settings.computerUse.windows.switch.description')}</span>
          <span
            className="flex items-center gap-1.5 font-medium text-foreground"
            data-testid="computer-use-switch-state"
          >
            {pending && <LoaderCircle className="size-3.5 shrink-0 animate-spin" aria-hidden="true" />}
            {stateText}
          </span>
        </span>
      )}
      actions={known || pending ? (
        <Switch
          checked={checked}
          onCheckedChange={change}
          disabled={disabled}
          label={t('settings.computerUse.title')}
          data-testid="computer-use-switch"
          data-switch-state={pending ? 'pending' : enabled ? 'on' : 'off'}
        />
      ) : (
        // No authoritative opt-in: no control, so nothing claims on or off.
        // The placeholder keeps the row's layout and is hidden from assistive tech.
        <span aria-hidden="true" data-testid="computer-use-switch-unknown" className="block h-6 w-11" />
      )}
    >
      {(failed || trayOnly) && (
        <p
          data-testid={failed ? 'computer-use-switch-failed' : 'computer-use-switch-tray-only'}
          className={
            failed
              ? 'flex max-w-2xl items-start gap-1.5 text-[12px] leading-relaxed text-destructive-ink'
              : 'flex max-w-2xl items-start gap-1.5 text-[12px] leading-relaxed text-muted'
          }
        >
          {failed
            ? <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            : <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />}
          <span>
            {failed
              ? t('settings.computerUse.windows.switch.failed')
              : t('settings.computerUse.windows.switch.trayOnly')}
          </span>
        </p>
      )}
    </SettingsPanel>
  );
};
