import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react';

import {
  CODE_PATTERN,
  DEFAULT_COOLDOWN_SECONDS,
  EMAIL_PATTERN,
  FAILED_COOLDOWN_SECONDS,
  PASSWORD_RETRY_SECONDS,
  SECOND_FACTOR_RETRY_MINUTES,
  completesCode,
  firstSecondFactorStep,
  pageErrorKey,
  passkeySignInErrorKey,
  providerCooldownSeconds,
  providerErrorKey,
  secondFactorRetryMinutes,
  secondFactorRetrySeconds,
  type SecondFactorStep,
  type SignInErrorKey,
} from './errors';
import type { OperationOwner, OperationToken } from './operationOwner';
import {
  CodeEntry,
  EmailEntry,
  type EmailMode,
  ForgotPassword,
  PasskeySignIn,
  SecondFactor,
  SignInStart,
} from './screens';
import {
  normalizeSignInError,
  type SignInAdapter,
  type SignInError,
  type SignInMethods,
  type SignInOutcome,
  type SignInProvider,
  type SignInStep,
  type WeakPasswordReason,
} from './types';

/** The flow's own steps; signed in belongs to the shell. */
export type SignInScreen = 'start' | 'email' | 'forgot' | 'passkey' | 'code' | 'secondFactor';

/** The operations the flow runs, as the steps show them. */
export type SignInBusyAction =
  | 'send'
  | 'verify'
  | 'resend'
  | 'signIn'
  | 'provider'
  | 'passkey'
  | 'primaryPasskey'
  | 'refresh';

type CooldownSource = 'client' | 'provider' | null;
type CooldownScope = 'send' | 'all' | null;
type SecondFactorSession = Extract<SignInStep, { status: 'secondFactor' }>;

/** How a session the flow obtained reached the shell. */
export type SignInSessionOrigin = {
  /** `secondFactor` finished an owed second factor; `refresh` re-read it. */
  kind: 'firstFactor' | 'secondFactor' | 'refresh';
  weakPassword?: WeakPasswordReason[];
};

export type SignInFlowOptions<Session, Action extends string> = {
  adapter: SignInAdapter<Session>;
  /** The ways in to offer, as the shell read avibe.bot's switches; the adapter's own otherwise. */
  methods?: SignInMethods;
  /** The page's one operation owner: the shell's own calls share it. */
  owner: OperationOwner<Action | SignInBusyAction>;
  /**
   * The flow's result for the shell: it records the session and, when signed in, takes over.
   * What it returns runs once the operation has ended (a call of its own, such as opening the
   * instance a launch restored).
   */
  onSession: (session: Session, origin: SignInSessionOrigin) => (() => void) | void;
  /** A new first factor starts: anything the shell kept for the previous attempt ends. */
  onBeginSignIn?: () => void;
  /** Leaves an owed second factor for another account (iPhone: sign out; Desktop: cancel). */
  onDifferentAccount: () => void;
  /** Opens avibe.bot's Account security page. */
  onOpenSecurity: () => void;
  /** The shell's own step for an account that must turn two-step verification on first. */
  renderEnroll?: (step: SecondFactorSession) => ReactNode;
};

/**
 * The sign-in flow both apps run: the same steps, order, copy, cooldowns and error handling.
 * State is the flow's; the shell's native side is reached only through `adapter`.
 */
export function useSignInFlow<Session, Action extends string>({
  adapter,
  methods = adapter.methods,
  owner,
  onSession,
  onBeginSignIn,
  onDifferentAccount,
  onOpenSecurity,
  renderEnroll,
}: SignInFlowOptions<Session, Action>) {
  const { beginOperation, busy, finishOperation, isOperationLocked, locked, ownsOperation } = owner;
  // avibe.bot's second-factor throttle window; a completed code waits for the button.
  const secondFactorRetryAt = useRef(0);
  const [screen, setScreen] = useState<SignInScreen>('start');
  const [secondFactor, setSecondFactor] = useState<SecondFactorSession | null>(null);
  const [email, setEmail] = useState('');
  const [emailMode, setEmailMode] = useState<EmailMode>('code');
  const [code, setCode] = useState('');
  const [errorKey, setErrorKey] = useState<SignInErrorKey | null>(null);
  const [cooldown, setCooldown] = useState(0);
  const [cooldownSource, setCooldownSource] = useState<CooldownSource>(null);
  const [cooldownScope, setCooldownScope] = useState<CooldownScope>(null);
  const [resent, setResent] = useState(false);
  const [factorStep, setFactorStep] = useState<SecondFactorStep>('passkey');
  const [factorCode, setFactorCode] = useState('');
  const [factorRetryMinutes, setFactorRetryMinutes] = useState(SECOND_FACTOR_RETRY_MINUTES);
  // Seconds before Sign in is offered again after a password rate limit.
  const [passwordRetry, setPasswordRetry] = useState(0);
  const providerCooldownActive = cooldownScope === 'all' && cooldown > 0;

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  useEffect(() => {
    if (passwordRetry <= 0) return;
    const timer = window.setTimeout(
      () => setPasswordRetry((value) => Math.max(0, value - 1)),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [passwordRetry]);

  /** The flow's step for a step the native side reports; setters only, so it is stable. */
  const showStep = useCallback((next: SignInStep) => {
    if (next.status === 'secondFactor') {
      setSecondFactor(next);
      setFactorStep(firstSecondFactorStep(next));
      setFactorCode('');
      setScreen('secondFactor');
    } else if (next.status === 'codeSent') {
      setEmail(next.email);
      setScreen('code');
    } else if (next.status === 'signedOut') {
      setScreen('start');
    }
  }, []);

  const applyOutcome = (
    token: OperationToken,
    outcome: SignInOutcome<Session>,
    allowSignedOut: boolean,
    origin: SignInSessionOrigin,
  ): (() => void) | void => {
    if (!ownsOperation(token)) return;
    if (outcome.step.status === 'signedOut' && !allowSignedOut) {
      setCode('');
      setErrorKey('mobileApp.errors.network');
      return;
    }
    showStep(outcome.step);
    setErrorKey(null);
    return onSession(outcome.session, origin);
  };

  const failure = (raw: unknown): SignInError => normalizeSignInError(raw);

  const requestOtp = async (resend: boolean) => {
    if (cooldown > 0) return;
    const normalizedEmail = email.trim();
    if (!EMAIL_PATTERN.test(normalizedEmail)) {
      setErrorKey('mobileApp.email.invalid');
      return;
    }
    const token = beginOperation(resend ? 'resend' : 'send');
    if (token === null) return;
    setEmail(normalizedEmail);
    setErrorKey(null);
    setResent(false);
    onBeginSignIn?.();
    try {
      const result = await adapter.requestCode(normalizedEmail);
      if (!ownsOperation(token)) return;
      const providerCooldown = Math.max(0, result.cooldown_seconds ?? 0);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('send');
      } else if (resend) {
        setCooldown(DEFAULT_COOLDOWN_SECONDS);
        setCooldownSource('client');
        setCooldownScope('send');
      } else {
        setCooldown(0);
        setCooldownSource(null);
        setCooldownScope(null);
      }
      setResent(resend);
      // A new email sign-in owes its own second factor.
      secondFactorRetryAt.current = 0;
      setScreen('code');
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = failure(rawError);
      setErrorKey(pageErrorKey(error, 'request'));
      const providerCooldown = providerCooldownSeconds(error);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('all');
      } else if (resend) {
        setCooldown(FAILED_COOLDOWN_SECONDS);
        setCooldownSource('client');
        setCooldownScope('send');
      }
    } finally {
      finishOperation(token);
    }
  };

  const verifyOtp = async (candidate: string = code) => {
    if (providerCooldownActive || !CODE_PATTERN.test(candidate)) return;
    const token = beginOperation('verify');
    if (token === null) return;
    setErrorKey(null);
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.verifyCode(email, candidate);
      after = applyOutcome(token, next, false, { kind: 'firstFactor' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = failure(rawError);
      const nextErrorKey = pageErrorKey(error, 'verify');
      setErrorKey(nextErrorKey);
      if (nextErrorKey === 'mobileApp.errors.network') setCode('');
      const providerCooldown = providerCooldownSeconds(error);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('all');
      }
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  const signInWithPassword = async (password: string) => {
    if (isOperationLocked() || passwordRetry > 0 || password.length === 0) return;
    // Switching to password mode never asks for the email; Sign in checks it.
    const normalizedEmail = email.trim();
    if (!EMAIL_PATTERN.test(normalizedEmail)) {
      setErrorKey('mobileApp.email.invalid');
      return;
    }
    const token = beginOperation('signIn');
    if (token === null) return;
    setEmail(normalizedEmail);
    setErrorKey(null);
    onBeginSignIn?.();
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.signInWithPassword(normalizedEmail, password);
      if (!ownsOperation(token)) return;
      // A new password sign-in owes its own second factor.
      if (next.step.status !== 'signedOut') secondFactorRetryAt.current = 0;
      after = applyOutcome(token, next, false, {
        kind: 'firstFactor',
        ...(next.step.status !== 'signedOut' && next.weakPassword ? { weakPassword: next.weakPassword } : {}),
      });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = failure(rawError);
      setErrorKey(pageErrorKey(error, 'password'));
      if (error.code === 'rate_limited') {
        setPasswordRetry(
          typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0
            ? Math.ceil(error.retry_after_seconds)
            : PASSWORD_RETRY_SECONDS,
        );
      }
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  /** Google, GitHub or Apple, natively; closing their window or sheet says nothing. */
  const signInWithProvider = async (provider: SignInProvider) => {
    const token = beginOperation('provider');
    if (token === null) return;
    setErrorKey(null);
    onBeginSignIn?.();
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.signInWithProvider(provider);
      if (!ownsOperation(token)) return;
      // A new sign-in owes its own second factor.
      if (next.step.status !== 'signedOut') secondFactorRetryAt.current = 0;
      after = applyOutcome(token, next, false, { kind: 'firstFactor' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setErrorKey(providerErrorKey(failure(rawError), provider));
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  /** Passkey sign-in with no email first; native runs the system prompt. */
  const signInWithPasskey = async () => {
    const token = beginOperation('primaryPasskey');
    if (token === null) return;
    setErrorKey(null);
    onBeginSignIn?.();
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.signInWithPasskey();
      if (!ownsOperation(token)) return;
      if (next.step.status !== 'signedOut') secondFactorRetryAt.current = 0;
      after = applyOutcome(token, next, false, { kind: 'firstFactor' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setErrorKey(passkeySignInErrorKey(failure(rawError)));
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  const refresh = async () => {
    if (!adapter.refresh) return;
    const token = beginOperation('refresh');
    if (token === null) return;
    setErrorKey(null);
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.refresh();
      after = applyOutcome(token, next, true, { kind: 'refresh' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setErrorKey(pageErrorKey(failure(rawError), 'other'));
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  const recordSecondFactorFailure = (error: SignInError) => {
    if (error.code === 'rate_limited') {
      secondFactorRetryAt.current = Date.now() + secondFactorRetrySeconds(error) * 1000;
    }
    setFactorRetryMinutes(secondFactorRetryMinutes(error));
  };

  const verifySecondFactorCode = async (candidate: string = factorCode) => {
    if (factorStep !== 'totp' && factorStep !== 'recovery') return;
    const method = factorStep;
    if (method === 'totp' ? !CODE_PATTERN.test(candidate) : candidate.trim().length === 0) {
      return;
    }
    const token = beginOperation('verify');
    if (token === null) return;
    setErrorKey(null);
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.verifySecondFactor(method, candidate);
      after = applyOutcome(token, next, true, { kind: 'secondFactor' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = failure(rawError);
      recordSecondFactorFailure(error);
      setErrorKey(pageErrorKey(error, method));
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  const verifyPasskey = async () => {
    const token = beginOperation('passkey');
    if (token === null) return;
    setErrorKey(null);
    let after: (() => void) | void = undefined;
    try {
      const next = await adapter.verifySecondFactorPasskey();
      after = applyOutcome(token, next, true, { kind: 'secondFactor' });
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = failure(rawError);
      recordSecondFactorFailure(error);
      setErrorKey(pageErrorKey(error, 'passkey'));
    } finally {
      finishOperation(token);
    }
    after?.();
  };

  const showStart = () => {
    if (isOperationLocked()) return;
    setErrorKey(null);
    setScreen('start');
  };

  const showEmail = () => {
    if (isOperationLocked()) return;
    setCode('');
    setErrorKey(null);
    setResent(false);
    if (cooldownSource === 'client') {
      setCooldown(0);
      setCooldownSource(null);
      setCooldownScope(null);
    }
    setEmailMode('code');
    setScreen('email');
  };

  /** The forgot page names the account and sends its code, so it needs a valid email. */
  const showForgot = () => {
    if (isOperationLocked()) return;
    const normalizedEmail = email.trim();
    if (!EMAIL_PATTERN.test(normalizedEmail)) {
      setErrorKey('mobileApp.email.invalid');
      return;
    }
    setEmail(normalizedEmail);
    setErrorKey(null);
    setScreen('forgot');
  };

  const showPasskey = () => {
    if (isOperationLocked()) return;
    setErrorKey(null);
    setScreen('passkey');
  };

  /** Back to the first step with nothing of the old attempt kept. */
  const reset = useCallback(() => {
    setEmail('');
    setCode('');
    setErrorKey(null);
    setCooldown(0);
    setCooldownSource(null);
    setCooldownScope(null);
    setScreen('start');
  }, []);

  /**
   * Follows a step the shell's native side reports without the flow asking (a launch that still
   * owes a second factor, another window's cancel).
   */
  const follow = useCallback((next: SignInStep) => {
    if (next.status === 'signedOut') {
      reset();
      return;
    }
    showStep(next);
    setErrorKey(null);
  }, [reset, showStep]);

  /** One sign-in step; `start` wraps the ways in with the shell's own first page. */
  const render = (start: (actions: ReactNode) => ReactNode): ReactNode => {
    if (screen === 'start') {
      return start(
        <SignInStart
          locked={locked}
          errorKey={errorKey}
          methods={methods}
          onContinue={showEmail}
          onProvider={(provider) => void signInWithProvider(provider)}
          onPasskey={showPasskey}
          waiting={busy === 'provider' && adapter.abortPending
            ? { onCancel: () => adapter.abortPending?.() }
            : null}
        />,
      );
    }
    if (screen === 'email') {
      return (
        <EmailEntry
          mode={emailMode}
          email={email}
          busy={busy}
          locked={locked}
          cooldown={cooldown}
          errorKey={errorKey}
          retryIn={passwordRetry}
          onBack={showStart}
          onEmailChange={(value) => {
            if (isOperationLocked()) return;
            setEmail(value);
            setErrorKey(null);
          }}
          onSubmit={() => void requestOtp(false)}
          onMode={(next) => {
            if (isOperationLocked()) return;
            setErrorKey(null);
            setEmailMode(next);
          }}
          onSignIn={(password) => void signInWithPassword(password)}
          onPasswordEdit={() => setErrorKey(null)}
          onForgot={showForgot}
          onUseCode={() => void requestOtp(false)}
        />
      );
    }
    if (screen === 'forgot') {
      return (
        <ForgotPassword
          email={email}
          busy={busy === 'send'}
          locked={locked}
          cooldown={cooldown}
          errorKey={errorKey}
          onBack={() => {
            if (isOperationLocked()) return;
            setErrorKey(null);
            setScreen('email');
          }}
          onSendCode={() => void requestOtp(false)}
          onOpenSecurity={onOpenSecurity}
        />
      );
    }
    if (screen === 'passkey') {
      return (
        <PasskeySignIn
          busy={busy}
          locked={locked}
          errorKey={errorKey}
          onPasskey={() => void signInWithPasskey()}
          onBack={showStart}
        />
      );
    }
    if (screen === 'code') {
      return (
        <CodeEntry
          email={email}
          code={code}
          busy={busy}
          locked={locked}
          errorKey={errorKey}
          cooldown={cooldown}
          providerCooldownActive={providerCooldownActive}
          resent={resent}
          onBack={showEmail}
          onCodeChange={(value) => {
            if (isOperationLocked()) return;
            const complete = completesCode(value, code);
            setCode(value);
            setErrorKey(null);
            if (complete) void verifyOtp(value);
          }}
          onVerify={() => void verifyOtp()}
          onResend={() => void requestOtp(true)}
        />
      );
    }
    if (!secondFactor) return null;
    if (secondFactor.gate === 'enroll' && renderEnroll) return renderEnroll(secondFactor);
    return (
      <SecondFactor
        session={secondFactor}
        step={factorStep}
        code={factorCode}
        busy={busy}
        locked={locked}
        errorKey={errorKey}
        retryMinutes={factorRetryMinutes}
        onStep={(step) => {
          if (isOperationLocked()) return;
          setFactorStep(step);
          setFactorCode('');
          setErrorKey(null);
        }}
        onCodeChange={(value) => {
          if (isOperationLocked()) return;
          // A recovery code is free text and waits for the button.
          const complete = factorStep === 'totp'
            && completesCode(value, factorCode)
            && Date.now() >= secondFactorRetryAt.current;
          setFactorCode(value);
          setErrorKey(null);
          if (complete) void verifySecondFactorCode(value);
        }}
        onVerify={() => void verifySecondFactorCode()}
        onPasskey={() => void verifyPasskey()}
        onOpenSecurity={onOpenSecurity}
        onSetupDone={() => void refresh()}
        onDifferentAccount={onDifferentAccount}
      />
    );
  };

  return { screen, email, errorKey, setErrorKey, follow, reset, render };
}
