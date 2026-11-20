// Setup's optional last step, remote access (ui/design: setup/remote-access/*). The same block as
// Settings · Devices while remote access is off (`ClaimFields`), in Setup's own shell: one
// behaviour everywhere. One tap skips it. In the Desktop App it takes the place of the
// 「本机已准备好」 screen that follows Setup. A view: props only.
import { ArrowRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { ClaimFields } from '../devices/DevicesPageView';
import type { NameState } from '../devices/types';
import { Button } from '../ui/button';
import { SetupHeader } from '../Wizard';

export type SetupRemoteAccessViewProps = (
  /** A plain browser on a standalone Runtime: the first claim passes avibe.bot once. */
  | { surface: 'browser' }
  /** The Desktop App: its own sign-in, then the name here. */
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
                      {t(signIn ? 'devices.reach.signInHint' : 'devices.setup.browserHint')}
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="onboarding-setup-footer">
            <Button
              type="button"
              variant="brand"
              className="group onboarding-action-w onboarding-primary-action"
              disabled={Boolean(claim && (claim.nameState === 'taken' || !claim.name))}
              onClick={signIn ? props.onSignIn : props.onTurnOn}
            >
              {t(signIn ? 'devices.reach.signInAndTurnOn' : 'devices.reach.turnOn')}<ArrowRight size={16} />
            </Button>
            <Button type="button" variant="ghost" className="onboarding-action-w onboarding-back-action" onClick={props.onSkip}>
              {t('devices.setup.skip')}
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}
