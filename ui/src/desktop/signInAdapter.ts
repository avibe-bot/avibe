import {
  SIGN_IN_PROVIDERS,
  type SignInAdapter,
  type SignInError,
  type SignInErrorCode,
  type SignInMethods,
  type SignInOutcome,
  type SignInStep,
} from '@/sign-in/types';

import type { DesktopClientAction, DesktopClientReply, DesktopClientState } from './bridge';

/** `useAccountClient().send`: a reply, a local invocation failure, or `null` once retired. */
export type DesktopSend = (
  action: DesktopClientAction,
) => Promise<DesktopClientReply | { kind: 'client_failure'; code: string; generation: number } | null>;

/** The flow's step for the native account state. */
export const desktopSignInStep = (state: DesktopClientState): SignInStep => {
  const account = state.account;
  switch (account.status) {
    case 'signed_in':
      return { status: 'signedIn' };
    case 'second_factor_required':
      return {
        status: 'secondFactor',
        email: account.email ?? account.identity?.email ?? null,
        gate: account.gate ?? 'challenge',
        methods: account.methods,
      };
    case 'verifying':
      return account.email ? { status: 'codeSent', email: account.email } : { status: 'signedOut' };
    default:
      return { status: 'signedOut' };
  }
};

type Operation = 'request' | 'password' | 'verify' | 'totp' | 'recovery' | 'passkey' | 'provider' | 'primaryPasskey';

/**
 * Native codes onto the flow's closed set. Native passes some upstream codes through
 * (`otp_expired` from the email code, `invalid` from avibe.bot's second factor).
 */
const canonicalCode = (code: string, operation: Operation): SignInErrorCode => {
  switch (code) {
    case 'stale_generation':
    case 'account_changed':
    case 'signed_out':
      return 'stale_session';
    case 'invalid_email':
    case 'invalid_credentials':
    case 'email_not_confirmed':
    case 'rate_limited':
    case 'passkey_cancelled':
    case 'passkey_unavailable':
      return code;
    case 'invalid_passkey_challenge':
      return 'passkey_unavailable';
    case 'provider_unavailable':
    case 'provider_email_unverified':
      return code;
    case 'invalid_or_expired':
    case 'provider_timeout':
      return operation === 'provider' ? 'invalid_or_expired' : 'network';
    case 'otp_expired':
    case 'invalid_otp':
    case 'invalid_code':
    case 'invalid':
    case 'expired':
      if (operation === 'verify') return 'invalid_or_expired';
      if (operation === 'totp' || operation === 'recovery' || operation === 'passkey' || operation === 'primaryPasskey') {
        return 'invalid';
      }
      return 'network';
    default:
      return 'network';
  }
};

const failure = (code: SignInErrorCode, retryAfter?: number): SignInError => ({
  code,
  message: 'The desktop account action failed.',
  ...(typeof retryAfter === 'number' ? { retry_after_seconds: retryAfter } : {}),
});

/**
 * The ways in avibe.bot's public switches turn on, as native last read them (`sign_in`): a
 * provider whose `*_login_enabled` is not true, or `passkey_login_enabled` missing or false, is
 * not offered. Native runs the offered ones (default browser and loopback PKCE; the system
 * passkey sheet).
 */
export const desktopSignInMethods = (state: DesktopClientState): SignInMethods => ({
  providers: SIGN_IN_PROVIDERS.filter((provider) => state.sign_in?.providers?.includes(provider) === true),
  passkey: state.sign_in?.passkey === true,
});

/**
 * The Desktop app's half of the shared sign-in flow. Every call is one `desktop_client_request`
 * action; native owns the account generation, the cookie jar and the keychain, and its state
 * broadcast stays the authority for what the window shows.
 */
export function createDesktopSignInAdapter(
  send: DesktopSend,
  methods: SignInMethods,
  /** This page's language: the browser's last page after a provider sign-in speaks it. */
  locale: 'en' | 'zh' = 'en',
): SignInAdapter<DesktopClientState> {
  const run = async (
    action: DesktopClientAction,
    operation: Operation,
  ): Promise<SignInOutcome<DesktopClientState>> => {
    const reply = await send(action);
    if (reply === null) throw failure('stale_session');
    if ('kind' in reply) throw failure(canonicalCode(reply.code, operation));
    if (!reply.ok) throw failure(canonicalCode(reply.error.code, operation), reply.error.retry_after_seconds);
    return { step: desktopSignInStep(reply.state), session: reply.state };
  };
  return {
    platform: 'desktop',
    methods,
    requestCode: async (email) => {
      await run({ type: 'request_otp', email }, 'request');
      return { cooldown_seconds: 0 };
    },
    verifyCode: (_email, code) => run({ type: 'verify_otp', code }, 'verify'),
    signInWithPassword: (email, password) => run({ type: 'sign_in_password', email, password }, 'password'),
    signInWithProvider: (provider) => methods.providers.includes(provider)
      ? run({ type: 'sign_in_with_provider', provider, locale }, 'provider')
      : Promise.reject(failure('provider_unavailable')),
    signInWithPasskey: () => methods.passkey
      ? run({ type: 'sign_in_with_passkey' }, 'primaryPasskey')
      : Promise.reject(failure('passkey_unavailable')),
    verifySecondFactor: (method, code) => run({ type: 'verify_second_factor', method, code }, method),
    verifySecondFactorPasskey: () => run({ type: 'verify_passkey' }, 'passkey'),
    // The browser has no cancel this app can see; native drops the attempt and its port.
    abortPending: () => {
      void send({ type: 'abort_sign_in' });
    },
  };
}
