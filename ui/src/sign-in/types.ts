/**
 * The one sign-in flow both apps draw (#3946). Each shell supplies a `SignInAdapter` to its
 * native side; credentials, ceremonies and cookies never reach this page.
 */

/** The other accounts both apps sign in with, in avibe.bot's login card order, then Apple. */
export type SignInProvider = 'google' | 'github' | 'apple';

export const SIGN_IN_PROVIDERS: readonly SignInProvider[] = ['google', 'github', 'apple'];

export type SecondFactorMethod = 'passkey' | 'totp';

/** Why avibe.bot still accepted a password that no longer meets its rules. */
export type WeakPasswordReason = 'length' | 'characters' | 'pwned';

/** Where sign-in stands, as the shell's native side reports it. */
export type SignInStep =
  | { status: 'signedOut' }
  /** An email code was sent to `email` and native holds that attempt. */
  | { status: 'codeSent'; email: string }
  | {
      /** The first factor passed; avibe.bot's second factor is still owed. */
      status: 'secondFactor';
      /** The account's email when native knows it. */
      email: string | null;
      /** `enroll`: the account must turn two-step verification on first. */
      gate: 'challenge' | 'enroll';
      /** The account's method kinds, passkey first; empty for `enroll`. */
      methods: SecondFactorMethod[];
    }
  | { status: 'signedIn' };

/** What a sign-in call ended in: the flow's step plus the shell's own session record. */
export type SignInOutcome<Session> = {
  step: SignInStep;
  session: Session;
  /** A password sign-in avibe.bot reported as weak. */
  weakPassword?: WeakPasswordReason[];
};

/**
 * The closed set of failures the flow tells apart. It is the iPhone bridge's vocabulary; another
 * adapter maps its native codes onto it. Anything else is `network`.
 */
export type SignInErrorCode =
  | 'invalid_email'
  | 'email_address_invalid'
  | 'invalid_or_expired'
  | 'invalid_credentials'
  | 'email_not_confirmed'
  | 'invalid'
  | 'rate_limited'
  | 'passkey_cancelled'
  | 'passkey_unavailable'
  | 'provider_cancelled'
  | 'provider_unavailable'
  | 'provider_email_unverified'
  | 'stale_session'
  | 'local_state_unavailable'
  | 'local_state_write_failed'
  | 'network'
  | (string & {});

export type SignInError = {
  code: SignInErrorCode;
  message: string;
  retry_after_seconds?: number;
};

/** The ways in this build can sign in; a way the native side cannot run is not offered. */
export type SignInMethods = {
  providers: readonly SignInProvider[];
  /** Passkey sign-in with no email first (not the passkey second factor). */
  passkey: boolean;
};

/**
 * One method per thing a person does. Each resolves to the next step or rejects with a
 * `SignInError`. Native owns ordering, generations and late results; the flow only drops a
 * result that an account change already retired.
 */
export interface SignInAdapter<Session> {
  readonly platform: 'ios' | 'desktop';
  readonly methods: SignInMethods;
  requestCode(email: string): Promise<{ cooldown_seconds: number }>;
  verifyCode(email: string, code: string): Promise<SignInOutcome<Session>>;
  signInWithPassword(email: string, password: string): Promise<SignInOutcome<Session>>;
  signInWithProvider(provider: SignInProvider): Promise<SignInOutcome<Session>>;
  signInWithPasskey(): Promise<SignInOutcome<Session>>;
  verifySecondFactor(method: 'totp' | 'recovery', code: string): Promise<SignInOutcome<Session>>;
  verifySecondFactorPasskey(): Promise<SignInOutcome<Session>>;
  /** Reads the session again: after the account turned two-step verification on elsewhere. */
  refresh?(): Promise<SignInOutcome<Session>>;
  /**
   * Ends a provider sign-in that waits in another app (Desktop's default browser). The waiting
   * call then settles as `stale_session`, which says nothing. Absent where the system sheet
   * carries its own cancel.
   */
  abortPending?(): void;
}

export const normalizeSignInError = (error: unknown): SignInError => {
  if (
    typeof error === 'object'
    && error !== null
    && typeof Reflect.get(error, 'code') === 'string'
    && typeof Reflect.get(error, 'message') === 'string'
  ) {
    const retryAfter = Reflect.get(error, 'retry_after_seconds');
    return {
      code: Reflect.get(error, 'code') as string,
      message: Reflect.get(error, 'message') as string,
      ...(typeof retryAfter === 'number' ? { retry_after_seconds: retryAfter } : {}),
    };
  }
  return { code: 'failed', message: 'The account operation failed.' };
};
