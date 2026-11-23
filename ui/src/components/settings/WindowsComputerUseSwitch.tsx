import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { CircleAlert, Info, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Switch } from '@/components/ui/switch';

/** How long a change may take to show up in the status before the switch gives it up. */
export const COMPUTER_USE_SWITCH_SETTLE_MS = 15_000;

/** `confirmFrom`: the first read that may confirm; `null` until the native call settles. */
type Pending = { id: number; target: boolean; startedAt: number; confirmFrom: number | null };

const noteClass = 'flex max-w-2xl items-start gap-1.5 text-[12px] leading-relaxed';

/**
 * Windows Computer Use on/off (ui/design: settings/computer-use/windows-*):
 * the switch for the Windows card's header and the note, if any, for its body.
 *
 * The switch shows the opt-in the server reports (`enabled`), never one
 * inferred from the readiness status, and is absent while that is unknown. A
 * change is confirmed only when a read that began after its native call
 * settled reports the target; until then the switch holds the target,
 * disabled, for at most {@link COMPUTER_USE_SWITCH_SETTLE_MS}. A rejected or
 * unconfirmed change returns to the server's value with a note that stays
 * until the next change. `enable`/`disable` are `null` where this page cannot
 * perform them (a browser, an older shell).
 */
export function useWindowsComputerUseSwitch({
  enabled,
  readSeq,
  nextReadSeq,
  enable,
  disable,
  refresh,
}: {
  enabled: boolean | null;
  /** Start order of the read that produced `enabled`. */
  readSeq: number;
  /** The start order the next status read will get. */
  nextReadSeq: () => number;
  enable: (() => Promise<void>) | null;
  disable: (() => Promise<void>) | null;
  /** Re-read the status now; the page keeps polling regardless. */
  refresh: () => void;
}): { control: ReactNode; notice: ReactNode; canTurnOn: boolean; changing: boolean } {
  const { t } = useTranslation();
  const [change_, setPending] = useState<Pending | null>(null);
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);
  // The change whose native call may still settle it; an older call's answer is ignored.
  const active = useRef<number | null>(null);
  const changes = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  // A change ends when a read that began after its native call reports the target.
  const confirmed = change_ !== null && change_.confirmFrom !== null
    && readSeq >= change_.confirmFrom && enabled === change_.target;
  // A confirmed change is over: drop it, so a later read (a tray change) is not taken for it.
  if (confirmed) setPending(null);
  const pending = confirmed ? null : change_;

  // An unconfirmed change gives up after the bound.
  useEffect(() => {
    if (!pending) return;
    const left = pending.startedAt + COMPUTER_USE_SWITCH_SETTLE_MS - Date.now();
    const timer = window.setTimeout(() => {
      if (!mounted.current) return;
      active.current = null;
      setPending(null);
      setFailed(true);
    }, Math.max(0, left));
    return () => window.clearTimeout(timer);
  }, [pending]);

  const change = (next: boolean) => {
    const action = next ? enable : disable;
    if (!action || pending) return;
    const id = ++changes.current;
    active.current = id;
    setFailed(false);
    setPending({ id, target: next, startedAt: Date.now(), confirmFrom: null });
    void action().then(
      () => {
        if (!mounted.current || active.current !== id) return;
        // Reads already under way may predate the change; only a later one confirms it.
        const confirmFrom = nextReadSeq();
        setPending((current) => (current?.id === id ? { ...current, confirmFrom } : current));
        refresh();
      },
      () => {
        if (!mounted.current || active.current !== id) return;
        active.current = null;
        setPending(null);
        setFailed(true);
        refresh();
      },
    );
  };

  const known = enabled !== null;
  const checked = pending ? pending.target : enabled === true;
  const action = checked ? disable : enable;
  const trayOnly = !pending && enabled === true && enable !== null && disable === null;

  const control = known || pending ? (
    <span className="flex items-center gap-2">
      {pending && (
        <LoaderCircle
          className="size-3.5 shrink-0 animate-spin text-muted"
          aria-label={t(pending.target ? 'settings.computerUse.windows.switch.turningOn' : 'settings.computerUse.windows.switch.turningOff')}
          role="img"
          data-testid="computer-use-switch-pending"
        />
      )}
      <Switch
        checked={checked}
        onCheckedChange={change}
        disabled={pending !== null || action === null}
        label={t('settings.computerUse.title')}
        data-testid="computer-use-switch"
        data-switch-state={pending ? 'pending' : enabled ? 'on' : 'off'}
      />
    </span>
  ) : (
    // No authoritative opt-in: no control, so nothing claims on or off.
    // The placeholder keeps the header's layout and is hidden from assistive tech.
    <span aria-hidden="true" data-testid="computer-use-switch-unknown" className="block h-6 w-11" />
  );

  const notice = failed ? (
    <p role="alert" data-testid="computer-use-switch-failed" className={`${noteClass} text-destructive-ink`}>
      <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <span>{t('settings.computerUse.windows.switch.failed')}</span>
    </p>
  ) : !known && !pending ? (
    <p data-testid="computer-use-switch-state-unknown" className={`${noteClass} text-muted`}>
      <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <span>{t('settings.computerUse.windows.switch.stateUnknown')}</span>
    </p>
  ) : trayOnly ? (
    <p data-testid="computer-use-switch-tray-only" className={`${noteClass} text-muted`}>
      <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <span>{t('settings.computerUse.windows.switch.trayOnly')}</span>
    </p>
  ) : null;

  return { control, notice, canTurnOn: !pending && enabled === false && enable !== null, changing: pending !== null };
}
