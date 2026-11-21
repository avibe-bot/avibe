// Setup's optional last step, remote access (ui/design: setup/remote-access/*). The same claim as
// Settings · Devices while remote access is off, in Setup's own shell: one behaviour everywhere.
// A text link skips it. A view: props only.
import { ArrowRight, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { ClaimFields } from '../devices/claim';
import { claimProblemText } from '../devices/texts';
import type { ClaimProblem, NameState } from '../devices/types';
import { Button } from '../ui/button';
import { KeptTail } from '../ui/kept-tail';
import { SetupHeader } from '../Wizard';

export type SetupRemoteAccessViewProps = (
  /** A plain browser on a standalone Runtime: the first claim passes avibe.bot once. */
  | { surface: 'browser'; busy?: boolean; problem?: ClaimProblem }
  /** The Desktop App: its own sign-in, then the name here (not wired to a page yet). */
  | { surface: 'desktop'; account: string | null; name: string; nameState?: NameState }
) & {
  onTurnOn?: () => void;
  onSignIn?: () => void;
  onNameChange?: (name: string) => void;
  onSkip?: () => void;
};

export function SetupRemoteAccessView(props: SetupRemoteAccessViewProps) {
  const { t } = useTranslation();
  const claim = props.surface === 'desktop' && props.account !== null ? props : null;
  const signIn = props.surface === 'desktop' && props.account === null;
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
                  {claim ? (
                    <ClaimFields account={claim.account ?? ''} name={claim.name} nameState={claim.nameState} onNameChange={props.onNameChange} />
                  ) : (
                    <p className="px-5 py-4 text-[13px] leading-relaxed text-muted">
                      <KeptTail text={t(signIn ? 'devices.reach.signInHint' : 'devices.setup.browserHint')} />
                    </p>
                  )}
                  {browser?.problem && (
                    <p role="alert" className={`border-t border-border px-5 py-3 text-[12px] leading-relaxed ${noAction ? 'text-foreground' : 'text-destructive-ink'}`}>
                      <KeptTail text={claimProblemText(t, browser.problem)} />
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
                disabled={Boolean(claim && (claim.nameState === 'taken' || !claim.name)) || browser?.busy}
                onClick={signIn ? props.onSignIn : props.onTurnOn}
              >
                {t(signIn ? 'devices.reach.signInAndTurnOn' : 'devices.reach.turnOn')}
                {browser?.busy ? <LoaderCircle size={16} className="motion-safe:animate-spin" /> : <ArrowRight size={16} />}
              </Button>
            )}
            <Button type="button" variant="link" className="onboarding-action-w" onClick={props.onSkip}>
              {t(noAction ? 'devices.setup.goOn' : 'devices.setup.skip')}
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}
