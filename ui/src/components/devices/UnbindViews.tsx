// 「解除绑定」 on 连接方式 (ui/design: settings/devices-unbind/*): one confirmation that says what is
// lost, then the instance does it by itself. The way back is 「开启远程访问」. A view: props only.
import { Check, LoaderCircle, Minus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { longDay } from './format';
import { unbindNameKeptText } from './texts';
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
    return (
      <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink">
        <KeptTail text={t('devices.unbind.notChanged', { reason: t(state.reason === 'ineligible' ? 'devices.unbind.reasonIneligible' : 'devices.unbind.reasonRefused') })} />
      </div>
    );
  }
  if (state.state === 'needsRepair') {
    // The page's own sentence for a binding record it cannot handle; no new one.
    return <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink"><KeptTail text={t('devices.rebind.needsTerminal')} /></div>;
  }
  return null;
}

/** The one confirmation: what ends, what is removed for good, what is kept, and the way back. */
export function UnbindConfirm({ host, confirm, onConfirm, onCancel }: {
  host: string; confirm: UnbindConfirmState; onConfirm?: () => void; onCancel?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const lost = [
    t('devices.unbind.lostRemote'),
    t('devices.unbind.lostAccess'),
    ...(confirm.healthConnected ? [t('devices.unbind.lostHealth')] : []),
  ];
  const date = confirm.releaseAt ? longDay(confirm.releaseAt, i18n.language) : undefined;
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel?.(); }}>
      <DialogContent className="max-w-md content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle className="text-balance">{t('devices.unbind.confirmTitle', { host })}</DialogTitle>
          <DialogDescription><KeptTail text={t('devices.unbind.confirmLead')} /></DialogDescription>
        </DialogHeader>
        <ul className="flex flex-col gap-2 text-[13px] leading-relaxed text-foreground">
          {lost.map((line) => (
            <li key={line} className="flex gap-2.5">
              <Minus className="mt-1 size-3.5 shrink-0 text-destructive-ink" />
              <span><KeptTail fill text={line} /></span>
            </li>
          ))}
          <li className="flex gap-2.5">
            <Check className="mt-1 size-3.5 shrink-0 text-mint-ink" />
            <span><KeptTail fill text={unbindNameKeptText(t, host, date, confirm.holdDays)} /></span>
          </li>
          <li className="flex gap-2.5">
            <Check className="mt-1 size-3.5 shrink-0 text-mint-ink" />
            <span><KeptTail fill text={t('devices.unbind.kept')} /></span>
          </li>
        </ul>
        <p className={`rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed ${confirm.remote ? 'border border-gold/40 bg-gold/10 text-foreground' : 'bg-field/60 text-muted'}`}>
          <KeptTail fill text={t(confirm.remote ? 'devices.unbind.wayBackRemote' : 'devices.unbind.wayBack')} />
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button variant="destructive" disabled={confirm.busy} onClick={onConfirm}>
            {confirm.busy && <LoaderCircle className="motion-safe:animate-spin" />}
            {t('devices.unbind.action')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
