// 「解除绑定」 on 连接方式 (ui/design: settings/devices-unbind/*): one confirmation that says what is
// lost, then the instance does it by itself. The way back is 「开启远程访问」. A view: props only.
import { Check, LoaderCircle, Minus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { longDay } from './format';
import { unbindNameKeptText, withComputer } from './texts';
import type { UnbindConfirmState, UnbindState } from './types';

/** What the row under the domain says about unbinding, where it says anything. */
export function UnbindLine({ state }: { state: UnbindState }) {
  const { t } = useTranslation();
  if (state.state === 'unbinding') {
    return (
      <div role="status" className="mt-1 flex items-center gap-1.5 text-[11px] leading-relaxed text-muted">
        <LoaderCircle className="size-3.5 shrink-0 motion-safe:animate-spin" />
        {t('devices.unbind.unbinding')}
      </div>
    );
  }
  if (state.state === 'notChanged') {
    return <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink"><KeptTail text={t('devices.unbind.notChanged')} /></div>;
  }
  if (state.state === 'needsRepair') {
    // The page's own sentence: at this computer `vibe remote rebind` clears only this computer, as
    // `vibe remote reset` does, while an unbinding is unfinished. No new sentence.
    return <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink"><KeptTail text={t('devices.rebind.needsTerminal')} /></div>;
  }
  return null;
}

/** What ends, what is removed for good, and what stays: one wording wherever the binding at
 *  avibe.bot is deleted (unbinding, binding as a new computer). `computer`: named from another
 *  device. `nameLine`: how long the name stays, and with whom. */
export function UnbindConsequences({ healthConnected, computer, nameLine }: { healthConnected: boolean; computer?: string; nameLine?: string }) {
  const { t } = useTranslation();
  const lost = [
    t('devices.unbind.lostRemote'),
    t('devices.unbind.lostAccess'),
    ...(healthConnected ? [computer ? withComputer(t, 'devices.unbind.lostHealthNamed', { computer }) : t('devices.unbind.lostHealth')] : []),
  ];
  return (
    <ul className="flex flex-col gap-2 text-[13px] leading-relaxed text-foreground">
      {lost.map((line) => (
        <li key={line} className="flex gap-2.5">
          <Minus className="mt-1 size-3.5 shrink-0 text-destructive-ink" />
          <span><KeptTail fill text={line} /></span>
        </li>
      ))}
      {nameLine && (
        <li className="flex gap-2.5">
          <Check className="mt-1 size-3.5 shrink-0 text-mint-ink" />
          <span><KeptTail fill text={nameLine} /></span>
        </li>
      )}
      <li className="flex gap-2.5">
        <Check className="mt-1 size-3.5 shrink-0 text-mint-ink" />
        <span><KeptTail fill text={computer ? withComputer(t, 'devices.unbind.keptNamed', { computer }) : t('devices.unbind.kept')} /></span>
      </li>
    </ul>
  );
}

/** The one confirmation: what ends, what is removed for good, what is kept, and the way back. */
export function UnbindConfirm({ host, confirm, onConfirm, onCancel }: {
  host: string; confirm: UnbindConfirmState; onConfirm?: () => void; onCancel?: () => void;
}) {
  const { t, i18n } = useTranslation();
  // From another device, 「这台电脑」 would be the phone in hand: the computer is named, or said as
  // the computer running Avibe where its name is not known.
  const computer = confirm.remote ? (confirm.computer || t('devices.unbind.thatComputer')) : undefined;
  // Once pressed, it is the instance's: nothing closes the dialog until the page moves on.
  const closable = !confirm.busy;
  const date = confirm.releaseAt ? longDay(confirm.releaseAt, i18n.language) : undefined;
  return (
    <Dialog open onOpenChange={(open) => { if (!open && closable) onCancel?.(); }}>
      <DialogContent
        className={`max-w-md content-start ${closable ? '' : '[&>button.absolute]:hidden'}`}
        mobileSheetHeight="tall"
        keepOnOutsideClick={!closable}
        closeLabel={t('common.close')}
      >
        <DialogHeader>
          <DialogTitle className="text-balance">{t('devices.unbind.confirmTitle', { host })}</DialogTitle>
          <DialogDescription><KeptTail text={t('devices.unbind.confirmLead')} /></DialogDescription>
        </DialogHeader>
        <UnbindConsequences healthConnected={confirm.healthConnected} computer={computer} nameLine={unbindNameKeptText(t, host, date, confirm.holdDays, computer)} />
        <p className={`rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed ${confirm.remote ? 'border border-gold/40 bg-gold/10 text-foreground' : 'bg-field/60 text-muted'}`}>
          <KeptTail
            fill
            text={computer ? withComputer(t, 'devices.unbind.wayBackRemoteNamed', { computer }) : t('devices.unbind.wayBack')}
          />
        </p>
        <DialogFooter>
          <Button variant="ghost" disabled={!closable} onClick={onCancel}>{t('common.cancel')}</Button>
          <Button variant="destructive" disabled={confirm.busy} onClick={onConfirm}>
            {confirm.busy && <LoaderCircle className="motion-safe:animate-spin" />}
            {t('devices.unbind.action')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
