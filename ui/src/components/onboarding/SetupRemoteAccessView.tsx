// Setup's optional last step, remote access (ui/design: setup/remote-access/*). The same claim as
// Settings · Devices while remote access is off, in Setup's own shell: one behaviour everywhere.
// A text link skips it. A view: props only.
import { ArrowRight, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { claimProblemText } from '../devices/texts';
import type { ClaimProblem } from '../devices/types';
import { Button } from '../ui/button';
import { KeptTail } from '../ui/kept-tail';
import { SetupHeader } from '../Wizard';

export type SetupRemoteAccessViewProps = (
  /** A plain browser on a standalone Runtime: the first claim passes avibe.bot once. */
  | { surface: 'browser'; busy?: boolean; problem?: ClaimProblem; /** `vibe remote rebind` exists on this instance. */ terminal?: boolean }
  /** The Desktop App: one action, which asks the shell for its claim dialog (the App's own
   *  sign-in if needed, then the name). `busy`: that request was just sent. */
  | { surface: 'desktop'; busy?: boolean }
) & {
  onTurnOn?: () => void;
  onSkip?: () => void;
};

export function SetupRemoteAccessView(props: SetupRemoteAccessViewProps) {
  const { t } = useTranslation();
  const browser = props.surface === 'browser' ? props : null;
  // An address that cannot start a claim has nothing to press here: the sentence, then go on.
  const noAction = browser?.problem?.code === 'link_required';
  return (
    <div className="onboarding-shell">
      <SetupHeader />
      <main className="onboarding-shell-content">
        <div className="onboarding-step" data-setup-screen="remote-access">
          <div className="onboarding-screens">
            <div data-setup-screen-root="remote-access">
              <div className="onboarding-setup">
                <header className="onboarding-heading">
                  <h1 tabIndex={-1}>{t('devices.setup.title')}</h1>
                  <p>{t('devices.setup.lead')}</p>
                </header>
                <div className="mx-auto mb-10 w-full max-w-[560px] overflow-hidden rounded-xl border border-edge bg-surface text-left shadow-elevation-0">
                  <p className="px-5 py-4 text-[13px] leading-relaxed text-muted">
                    <KeptTail text={t(browser ? 'devices.setup.browserHint' : 'devices.setup.desktopHint')} />
                  </p>
                  {browser?.problem && (
                    <p role="alert" className={`border-t border-border px-5 py-3 text-[12px] leading-relaxed ${noAction ? 'text-foreground' : 'text-destructive-ink'}`}>
                      <KeptTail text={claimProblemText(t, browser.problem, browser.terminal ?? true)} />
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="onboarding-setup-footer">
            {!noAction && (
              <Button
                type="button"
                variant="brand"
                className="group onboarding-action-w onboarding-primary-action"
                disabled={props.busy}
                onClick={props.onTurnOn}
              >
                {t('devices.reach.turnOn')}
                {browser?.busy ? <LoaderCircle size={16} className="motion-safe:animate-spin" /> : <ArrowRight size={16} />}
              </Button>
            )}
            {/* While the request is on its way the page is about to leave for avibe.bot: no skipping past it. */}
            <Button type="button" variant="link" className="onboarding-action-w" disabled={browser?.busy} onClick={props.onSkip}>
              {t(noAction ? 'devices.setup.goOn' : 'devices.setup.skip')}
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}
