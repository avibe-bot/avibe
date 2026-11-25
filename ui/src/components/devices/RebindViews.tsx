// Binding remote access again (ui/design: settings/devices-rename/rebind-*): a quiet row at the
// bottom of 「连接方式」, offered at the computer that runs Avibe only. Binding as a new computer is
// offered only after avibe.bot refused in one of the two ways that allow it. The sentences are the
// ones decided for `vibe remote rebind` (docs/plans/av13-terminal-copy.md), with the commands
// replaced by what this page does. A view: props only.
import type { ReactNode } from 'react';
import { ExternalLink, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { longDay } from './format';
import { rebindRefusalText, unbindNameKeptText } from './texts';
import type { RebindDialog, RebindState } from './types';
import { UnbindConsequences } from './UnbindViews';

export type RebindActions = {
  onRebind?: () => void;
  onRebindConfirm?: () => void;
  /** Cancel this tab's own request while avibe.bot has not approved it. */
  onRebindCancel?: () => void;
  onRebindAsNew?: () => void;
  onRebindAsNewConfirm?: () => void;
  onRebindClose?: () => void;
  /** Inside Avibe Desktop: open this computer's own connection page in the system browser. */
  onRebindInBrowser?: () => void;
  /** After a reset: to the Devices page, where remote access is turned on. */
  onRebindTurnOn?: () => void;
};

function Line({ children, tone = 'muted' }: { children: string; tone?: 'muted' | 'warning' }) {
  return (
    <p role={tone === 'warning' ? 'alert' : undefined} className={`mt-1 text-[11px] leading-relaxed ${tone === 'warning' ? 'text-gold-ink' : 'text-muted'}`}>
      <KeptTail text={children} />
    </p>
  );
}

/** The row under 「更改域名」. What it says and offers follows the binding's state alone. */
export function RebindRow({ state, canReset, needsBrowser, open, actions }: {
  state: RebindState;
  /** This browser started the attempt avibe.bot refused, so it may confirm the reset. */
  canReset?: boolean;
  /** Inside Avibe Desktop: binding again passes avibe.bot in a browser, so the row opens this
   *  computer's own page there instead of starting it. */
  needsBrowser?: boolean;
  /** This tab's own request is open and not approved yet (or the instance is trying avibe.bot
   *  again): the row says so and offers the one thing to do, cancelling it. */
  open?: 'waiting' | 'retrying';
  actions: RebindActions;
}) {
  const { t } = useTranslation();
  if (state.state === 'unpaired') return null;
  let lines: ReactNode = null;
  let control: ReactNode = null;
  const inBrowser = (
    <Button variant="ghost" size="compact" className="shrink-0" onClick={actions.onRebindInBrowser}>
      <ExternalLink />{t('devices.rebind.openInBrowser')}
    </Button>
  );
  const bindAgain = (busy?: boolean) => (
    <Button variant="ghost" size="compact" className="shrink-0" disabled={busy} onClick={actions.onRebind}>
      {busy && <LoaderCircle className="motion-safe:animate-spin" />}
      {t('devices.rebind.action')}
    </Button>
  );
  switch (state.state) {
    case 'remote':
      lines = <Line>{t('devices.rebind.fromComputer')}</Line>;
      break;
    case 'ready':
      lines = (
        <>
          <Line>{t('devices.rebind.hint')}</Line>
          {state.rejected && <Line tone="warning">{t(state.tunnelRunning ? 'devices.rebind.rejectedTunnelRunning' : 'devices.rebind.rejected')}</Line>}
          {state.replayRefused && <Line tone="warning">{t('devices.rebind.replayRefused')}</Line>}
          {state.refusal && <Line tone="warning">{rebindRefusalText(t, state.refusal)}</Line>}
        </>
      );
      control = needsBrowser ? inBrowser : bindAgain(state.busy);
      break;
    case 'recovering':
      lines = <Line>{t('devices.claim.problem.recovering')}</Line>;
      break;
    case 'busy':
      lines = <Line>{t('devices.rebind.busy')}</Line>;
      break;
    case 'resetting':
      lines = <Line tone="warning">{t('devices.rebind.resetting')}</Line>;
      break;
    case 'needsTerminal':
      lines = <Line tone="warning">{t('devices.rebind.needsTerminal')}</Line>;
      break;
    case 'unknown':
      lines = <Line>{t('devices.rebind.unknown')}</Line>;
      break;
    case 'asNewOffered':
      lines = (
        <>
          <Line tone="warning">
            {state.reason === 'other_account'
              ? state.hint ? t('devices.rebind.otherAccount', { hint: state.hint }) : t('devices.rebind.otherAccountPlain')
              : t('devices.rebind.unrecoverable')}
          </Line>
          {state.reason === 'other_account' && <Line>{t('devices.rebind.otherAccountAsNew')}</Line>}
          {state.refusal && <Line tone="warning">{rebindRefusalText(t, state.refusal)}</Line>}
          {!canReset && !needsBrowser && <Line>{t('devices.rebind.asNewNeedsAttempt')}</Line>}
        </>
      );
      control = needsBrowser ? inBrowser : (
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          {bindAgain(state.busy)}
          {canReset && (
            <Button variant="ghost" size="compact" className="text-destructive-ink" disabled={state.busy} onClick={actions.onRebindAsNew}>
              {t('devices.rebind.asNewAction')}
            </Button>
          )}
        </div>
      );
      break;
  }
  if (open && state.state !== 'remote') {
    lines = <Line>{t(open === 'retrying' ? 'devices.reach.retrying' : 'devices.reach.waiting')}</Line>;
    control = <Button variant="ghost" size="compact" className="shrink-0" onClick={actions.onRebindCancel}>{t('common.cancel')}</Button>;
  }
  return (
    <div id="rebind" className="flex flex-col gap-2 border-t border-border px-5 py-3 md:flex-row md:items-center md:gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-foreground">{t('devices.rebind.title')}</div>
        {lines}
        {needsBrowser && !open && (state.state === 'ready' || state.state === 'asNewOffered') && <Line>{t('devices.rebind.inBrowser')}</Line>}
      </div>
      {control}
    </div>
  );
}

function List({ title, items }: { title: string; items: Array<string | null> }) {
  return (
    <div>
      <div className="text-[12px] font-semibold text-foreground">{title}</div>
      <ul className="mt-1 flex list-disc flex-col gap-1 pl-4 text-[12px] leading-relaxed text-muted">
        {items.filter((each): each is string => each !== null).map((item) => <li key={item}><KeptTail text={item} /></li>)}
      </ul>
    </div>
  );
}

export function RebindDialogs({ dialog, state, host, busy, actions }: {
  dialog: RebindDialog;
  state: RebindState;
  /** The domain this computer is bound to now, when known. */
  host?: string;
  busy?: boolean;
  actions: RebindActions;
}) {
  const { t, i18n } = useTranslation();
  const close = (open: boolean) => { if (!open) actions.onRebindClose?.(); };
  const domain = host ?? t('devices.rebind.currentDomain');
  if (dialog.kind === 'confirm') {
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-md" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t('devices.rebind.confirmTitle')}</DialogTitle>
            <DialogDescription>
              <KeptTail text={t('devices.rebind.confirmBody')} />
            </DialogDescription>
          </DialogHeader>
          <p className="text-[13px] leading-relaxed text-foreground"><KeptTail text={t('devices.rebind.copiedData')} /></p>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRebindClose}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={busy} onClick={actions.onRebindConfirm}>{t('devices.rebind.action')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  if (dialog.kind === 'asNewCloud') {
    const other = state.state === 'asNewOffered' && state.reason === 'other_account';
    const hint = state.state === 'asNewOffered' ? state.hint : undefined;
    const date = dialog.releaseAt ? longDay(dialog.releaseAt, i18n.language) : undefined;
    // The same consequences as unbinding: the binding at avibe.bot is deleted first. The name stays
    // with the account that held it, which is another one's when it is another account's Avibe.
    const nameLine = other
      ? (date
        ? t('devices.rebind.asNewCloud.loseName', { host: domain, date })
        : typeof dialog.holdDays === 'number'
          ? t('devices.rebind.asNewCloud.loseNameDays', { host: domain, count: dialog.holdDays })
          : t('devices.rebind.asNewCloud.loseNamePlain', { host: domain }))
      : unbindNameKeptText(t, domain, date, dialog.holdDays);
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-md content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t('devices.rebind.asNewCloud.title', { host: domain })}</DialogTitle>
            <DialogDescription>
              {other && <><KeptTail text={hint ? t('devices.rebind.asNew.whose', { hint }) : t('devices.rebind.asNew.whosePlain')} /><br /></>}
              <KeptTail text={t('devices.rebind.asNewCloud.intro', { host: domain })} />
            </DialogDescription>
          </DialogHeader>
          <UnbindConsequences healthConnected={dialog.healthConnected} nameLine={nameLine} />
          <p className="rounded-lg bg-field/60 px-3.5 py-2.5 text-[12px] leading-relaxed text-muted">
            <KeptTail fill text={t('devices.rebind.asNewCloud.next')} />
          </p>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRebindClose}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={busy} onClick={actions.onRebindAsNewConfirm}>{t('devices.rebind.asNewAction')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  if (dialog.kind === 'asNew') {
    const other = state.state === 'asNewOffered' && state.reason === 'other_account';
    const hint = state.state === 'asNewOffered' ? state.hint : undefined;
    // After avibe.bot did not accept deleting the old binding, why only this computer is cleared comes first.
    const refused = dialog.afterRefusal === true;
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-xl content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t(refused ? 'devices.rebind.asNew.localAction' : 'devices.rebind.asNewAction')}</DialogTitle>
            <DialogDescription>
              {/* Whose Avibe it is comes first when it is another account's: the masked hint, nothing more. */}
              {refused && <><KeptTail text={t('devices.rebind.asNew.refusedLead')} /><br /></>}
              {!refused && other && <><KeptTail text={hint ? t('devices.rebind.asNew.whose', { hint }) : t('devices.rebind.asNew.whosePlain')} /><br /></>}
              <KeptTail text={t(refused ? 'devices.rebind.asNew.introLocal' : 'devices.rebind.asNew.intro')} />
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3 rounded-lg bg-field px-4 py-3">
            <List
              title={t('devices.rebind.asNew.lose')}
              items={[
                t('devices.rebind.asNew.loseAccess', { host: domain }),
                t('devices.rebind.asNew.loseDevices'),
                t('devices.rebind.asNew.losePeople'),
                t('devices.rebind.asNew.loseLinks'),
                dialog.healthConnected ? t('devices.rebind.asNew.loseHealth') : null,
                t('devices.rebind.asNew.loseName', { host: domain }),
              ]}
            />
            <List title={t('devices.rebind.asNew.keep')} items={[t('devices.rebind.asNew.keepAll')]} />
            <List title={t('devices.rebind.asNew.unchanged')} items={[t('devices.rebind.asNew.unchangedAvibe'), t('devices.rebind.asNew.unchangedCredentials')]} />
          </div>
          <p className="text-[13px] leading-relaxed text-foreground"><KeptTail text={t('devices.rebind.asNew.copiedData')} /></p>
          <p className="text-[13px] font-medium text-foreground">{t('devices.rebind.asNew.irreversible')}</p>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRebindClose}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={busy} onClick={actions.onRebindAsNewConfirm}>
              {t(refused ? 'devices.rebind.asNew.localAction' : 'devices.rebind.asNewAction')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  if (dialog.kind === 'resetDone') {
    const old = dialog.oldHost ?? t('devices.rebind.oldName');
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t('devices.rebind.resetDoneTitle')}</DialogTitle>
            <DialogDescription><KeptTail text={t('devices.rebind.resetDone')} /></DialogDescription>
          </DialogHeader>
          <div className="rounded-lg bg-field px-4 py-3 text-[12px] leading-relaxed text-muted">
            <div className="font-semibold text-foreground">{t('devices.rebind.nameBack.title')}</div>
            <p className="mt-1"><KeptTail text={t('devices.rebind.nameBack.kept')} /></p>
            <p className="mt-1"><KeptTail text={t('devices.rebind.nameBack.until')} /></p>
            <p className="mt-1"><KeptTail text={t('devices.rebind.nameBack.how', { old })} /></p>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRebindClose}>{t('common.close')}</Button>
            <Button variant="default" onClick={actions.onRebindTurnOn}>{t('devices.rebind.toDevices')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  return (
    <Dialog open onOpenChange={close}>
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle className="text-balance">{t('devices.rebind.boundTitle')}</DialogTitle>
          <DialogDescription>
            <KeptTail
              text={dialog.disposition === 'new' && dialog.host
                ? t('devices.rebind.boundNew', { host: dialog.host })
                : dialog.disposition === 'existing' ? t('devices.rebind.boundExisting') : t('devices.rebind.boundPlain')}
            />
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="default" onClick={actions.onRebindClose}>{t('common.done')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
