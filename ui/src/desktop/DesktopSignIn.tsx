import { ArrowLeft } from 'lucide-react';
import { createContext, type ReactNode, useContext, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { SignInChrome, type SignInChromeValue, type SignInFrameProps } from '@/sign-in/chromeContext';
import { useOperationOwner } from '@/sign-in/operationOwner';
import { SIGN_IN_PROVIDERS, type SignInMethods, type SignInStep } from '@/sign-in/types';
import { type SignInBusyAction, useSignInFlow } from '@/sign-in/useSignInFlow';
import { SignInWelcome } from '@/sign-in/welcome';

import type { DesktopClientState } from './bridge';
import { createDesktopSignInAdapter, desktopSignInMethods, desktopSignInStep, type DesktopSend } from './signInAdapter';

/** The way back to this computer's own instance, under every step. */
const LocalExit = createContext<() => void>(() => undefined);

/** The account window's column around one shared step; stable, so a step keeps its fields. */
function DesktopSignInFrame({ children, back }: SignInFrameProps) {
  const { t } = useTranslation();
  const onLocal = useContext(LocalExit);
  return (
    <div data-account-login className="mx-auto my-auto flex w-full max-w-md shrink-0 flex-col py-4">
      {back ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="-ml-2 mb-2 self-start"
          disabled={back.disabled}
          onClick={back.onBack}
        >
          <ArrowLeft className="size-4" />
          {/* Back one step within sign-in (Max, 2026-11-20); the header's "Back to Workbench" leaves it. */}
          {t('desktopClient.previousStep')}
        </Button>
      ) : null}
      {children}
      <div className="mt-6 text-center">
        <Button variant="ghost" onClick={onLocal}>{t('desktopClient.continueLocal')}</Button>
      </div>
    </div>
  );
}

const DESKTOP_SIGN_IN_CHROME: SignInChromeValue = { Frame: DesktopSignInFrame, platform: 'desktop' };

const stepKey = (step: SignInStep): string => step.status === 'codeSent'
  ? `codeSent:${step.email}`
  : step.status === 'secondFactor'
    ? `secondFactor:${step.email ?? ''}:${step.gate}:${step.methods.join(',')}`
    : step.status;

export type DesktopSignInProps = {
  state: DesktopClientState;
  send: DesktopSend;
  /** The ways to offer; by default what avibe.bot's public switches turn on (`state.sign_in`). */
  methods?: SignInMethods;
  /** Leaves sign-in for this computer's own instance (`return_local`). */
  onLocal: () => void;
  /** Leaves an owed second factor (`cancel`): native drops that attempt. */
  onDifferentAccount: () => void;
  /** Opens avibe.bot's Account security page. */
  onOpenSecurity: () => void;
  /** The in-app authenticator setup an account that must turn two-step verification on runs. */
  renderEnroll: () => ReactNode;
};

/**
 * The Desktop window's sign-in: the shared flow (#3946) in the account window's column. Native
 * state stays the authority: a step it reports without this page asking (a cancel, another
 * window, a relaunch mid-attempt) retires the running call and moves the flow there.
 * ui/design: desktop/sign-in.
 */
export function DesktopSignIn({
  state,
  send,
  methods,
  onLocal,
  onDifferentAccount,
  onOpenSecurity,
  renderEnroll,
}: DesktopSignInProps) {
  const { t, i18n } = useTranslation();
  const owner = useOperationOwner<SignInBusyAction>();
  const offered = methods ?? desktopSignInMethods(state);
  // One adapter per set of ways in (the switches arrive after the first paint), not per render.
  const providers = offered.providers.join(',');
  const passkey = offered.passkey;
  const locale = i18n.resolvedLanguage?.startsWith('zh') ? 'zh' : 'en';
  const adapter = useMemo(() => createDesktopSignInAdapter(send, {
    providers: SIGN_IN_PROVIDERS.filter((provider) => providers.split(',').includes(provider)),
    passkey,
  }, locale), [send, providers, passkey, locale]);
  const flow = useSignInFlow<DesktopClientState, never>({
    adapter,
    owner,
    // The native broadcast already carries the session; signed in leaves this view.
    onSession: () => undefined,
    onDifferentAccount,
    onOpenSecurity,
    renderEnroll: () => renderEnroll(),
  });
  const step = desktopSignInStep(state);
  const key = stepKey(step);
  const followed = useRef<string | null>(null);
  const { follow } = flow;
  const { retireOperation } = owner;
  // Read avibe.bot's public switches whenever the first step can show: on opening, and on
  // coming back from a second factor. A second factor or an enrollment shows none of them.
  const firstFactor = state.account.status !== 'second_factor_required';
  useEffect(() => {
    if (firstFactor) void send({ type: 'refresh_sign_in_methods' });
  }, [send, firstFactor]);
  // Leaving the page ends a sign-in still waiting in the browser: nothing would show its result.
  const latest = useRef({ adapter, waiting: false });
  useEffect(() => {
    latest.current = { adapter, waiting: owner.busy === 'provider' };
  });
  useEffect(() => () => {
    if (latest.current.waiting) latest.current.adapter.abortPending?.();
  }, []);
  useEffect(() => {
    if (followed.current === key) return;
    const first = followed.current === null;
    followed.current = key;
    // At first sight only a step past signed out moves the flow; later, every change does.
    if (first && step.status === 'signedOut') return;
    // Native commits a first factor only when it succeeds, so a change the page did not ask for
    // is someone else's: a cancel, another window, a relaunch.
    retireOperation();
    follow(step);
  }, [follow, key, retireOperation, step]);

  return (
    <LocalExit.Provider value={onLocal}>
    <SignInChrome value={DESKTOP_SIGN_IN_CHROME}>
      {flow.render((actions) => (
        // The iPhone's first page, smaller and without its brand row, with the Desktop's own
        // subtitle (Max, 2026-11-20); "continue on this computer" stays, quieter, below it.
        <div data-account-login className="mx-auto my-auto flex w-full max-w-sm shrink-0 flex-col py-4">
          <SignInWelcome compact subtitle={[t('desktopClient.welcomeLine1'), t('desktopClient.welcomeLine2')]}>
            {actions}
          </SignInWelcome>
          <div className="mt-2 text-center">
            <Button variant="ghost" size="sm" className="text-muted" onClick={onLocal}>
              {t('desktopClient.continueLocal')}
            </Button>
          </div>
        </div>
      ))}
    </SignInChrome>
    </LocalExit.Provider>
  );
}
