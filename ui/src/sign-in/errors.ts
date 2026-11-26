import type { TranslationKey } from '@/i18n/types';

import type { SignInError, SignInProvider, SignInStep } from './types';

export type SignInOperation = 'request' | 'verify' | 'password' | 'totp' | 'recovery' | 'passkey';

/** What a step says when its call failed. */
export type SignInErrorKey =
  | 'mobileApp.email.invalid'
  | 'mobileApp.code.invalid'
  | 'mobileApp.code.rateLimited'
  | 'mobileApp.password.invalid'
  | 'mobileApp.password.unconfirmed'
  | 'mobileApp.password.rateLimited'
  | 'mobileApp.twoFactor.passkeyFailed'
  | 'mobileApp.twoFactor.codeInvalid'
  | 'mobileApp.twoFactor.recoveryInvalid'
  | 'mobileApp.twoFactor.rateLimited'
  | 'mobileApp.errors.network'
  | 'mobileApp.errors.localRecords'
  | ProviderErrorKey
  | PasskeySignInErrorKey;
/** What a Google, GitHub or Apple sign-in that did not finish says on the first step. */
export type ProviderErrorKey =
  | 'desktopBootstrap.providerError.accountConflict'
  | 'desktopBootstrap.providerError.serverError'
  | 'desktopBootstrap.providerError.denied'
  | 'desktopBootstrap.providerError.timedOut'
  | 'desktopBootstrap.providerError.failed'
  | 'mobileApp.provider.unavailable'
  | 'mobileApp.provider.googleEmailUnverified'
  | 'mobileApp.provider.githubEmailUnverified'
  | 'mobileApp.provider.googleFailed'
  | 'mobileApp.provider.githubFailed'
  | 'mobileApp.provider.appleFailed';
/** What passkey sign-in (no email first) says when it did not finish. */
export type PasskeySignInErrorKey =
  | 'signIn.passkey.incomplete'
  | 'signIn.passkey.failed'
  | 'signIn.passkey.unsupported';

type SecondFactorStepSource = Extract<SignInStep, { status: 'secondFactor' }>;
export type SecondFactorStep = 'passkey' | 'totp' | 'recovery' | 'setup';

export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const CODE_PATTERN = /^\d{6}$/;
export const DEFAULT_COOLDOWN_SECONDS = 60;
export const FAILED_COOLDOWN_SECONDS = 15;
// Sign in waits this long after a password rate limit that names no Retry-After.
export const PASSWORD_RETRY_SECONDS = 60;
// avibe.bot counts second-factor attempts over this window.
export const SECOND_FACTOR_RETRY_MINUTES = 15;
const PASSKEY_FAILURES = new Set(['passkey_cancelled', 'passkey_unavailable', 'invalid']);

export const providerCooldownSeconds = (error: SignInError): number => {
  if (typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0) {
    return Math.max(1, Math.ceil(error.retry_after_seconds));
  }
  return error.code === 'rate_limited' ? FAILED_COOLDOWN_SECONDS : 0;
};

/** Passkey first when the account has one; an account that must turn it on sets it up. */
export const firstSecondFactorStep = (step: SecondFactorStepSource): SecondFactorStep => {
  if (step.gate === 'enroll') return 'setup';
  if (step.methods.includes('passkey')) return 'passkey';
  return step.methods.includes('totp') ? 'totp' : 'recovery';
};

export const secondFactorRetrySeconds = (error: SignInError): number =>
  typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0
    ? error.retry_after_seconds
    : SECOND_FACTOR_RETRY_MINUTES * 60;

export const secondFactorRetryMinutes = (error: SignInError): number =>
  Math.ceil(secondFactorRetrySeconds(error) / 60);

/** True when an edit, not a re-render, makes a six-digit code complete. */
export const completesCode = (next: string, previous: string): boolean =>
  CODE_PATTERN.test(next) && next !== previous;

/** A failure's line; `other` is any call outside sign-in that shares these answers. */
export const pageErrorKey = (
  error: SignInError,
  operation: SignInOperation | 'other',
): SignInErrorKey => {
  if (error.code === 'local_state_unavailable' || error.code === 'local_state_write_failed') {
    return 'mobileApp.errors.localRecords';
  }
  if (operation === 'password') {
    if (error.code === 'invalid_credentials') return 'mobileApp.password.invalid';
    if (error.code === 'email_not_confirmed') return 'mobileApp.password.unconfirmed';
    if (error.code === 'rate_limited') return 'mobileApp.password.rateLimited';
    return 'mobileApp.errors.network';
  }
  if (
    (error.code === 'invalid_email' || error.code === 'email_address_invalid')
    && operation === 'request'
  ) {
    return 'mobileApp.email.invalid';
  }
  if (error.code === 'invalid_or_expired' && operation === 'verify') {
    return 'mobileApp.code.invalid';
  }
  if (operation === 'totp' || operation === 'recovery' || operation === 'passkey') {
    if (error.code === 'rate_limited') return 'mobileApp.twoFactor.rateLimited';
    if (operation === 'passkey' && PASSKEY_FAILURES.has(error.code)) {
      return 'mobileApp.twoFactor.passkeyFailed';
    }
    if (error.code === 'invalid') {
      return operation === 'totp'
        ? 'mobileApp.twoFactor.codeInvalid'
        : 'mobileApp.twoFactor.recoveryInvalid';
    }
  }
  if (error.code === 'rate_limited') {
    return 'mobileApp.code.rateLimited';
  }
  return 'mobileApp.errors.network';
};

/**
 * A provider sign-in's failure on the first step, or `null` when the person closed it (nothing to say).
 * Copy approved by Product 2026-11-18; the email check names the provider.
 */
export const providerErrorKey = (
  error: SignInError,
  provider: SignInProvider,
): SignInErrorKey | null => {
  // The account signed in but this iPhone could not keep its records: retrying cannot help.
  if (error.code === 'local_state_unavailable' || error.code === 'local_state_write_failed') {
    return 'mobileApp.errors.localRecords';
  }
  switch (error.code) {
    // A closed window says nothing; so does an attempt that an account change (a sign-out
    // elsewhere) ended, since the page follows that change.
    case 'provider_cancelled':
    case 'stale_session':
      return null;
    case 'provider_account_conflict':
      return 'desktopBootstrap.providerError.accountConflict';
    case 'provider_server_error':
      return 'desktopBootstrap.providerError.serverError';
    case 'provider_denied':
      return 'desktopBootstrap.providerError.denied';
    case 'provider_timeout':
      return 'desktopBootstrap.providerError.timedOut';
    case 'provider_failed':
      return 'desktopBootstrap.providerError.failed';
    case 'provider_unavailable':
      return 'mobileApp.provider.unavailable';
    case 'provider_email_unverified':
      if (provider === 'google') return 'mobileApp.provider.googleEmailUnverified';
      if (provider === 'github') return 'mobileApp.provider.githubEmailUnverified';
      return 'mobileApp.provider.appleFailed';
    case 'rate_limited':
      return 'mobileApp.code.rateLimited';
    case 'invalid_or_expired':
      return provider === 'google'
        ? 'mobileApp.provider.googleFailed'
        : provider === 'github'
          ? 'mobileApp.provider.githubFailed'
          : 'mobileApp.provider.appleFailed';
    default:
      return provider === 'apple' ? 'mobileApp.provider.appleFailed' : 'mobileApp.errors.network';
  }
};

/**
 * Passkey sign-in with no email first, in avibe.bot's words for its own passkey card. A closed or
 * timed-out prompt is not a failure of the account, so it says the sign-in did not finish.
 */
export const passkeySignInErrorKey = (error: SignInError): SignInErrorKey | null => {
  switch (error.code) {
    // An attempt an account change ended says nothing: the page follows that change.
    case 'stale_session':
      return null;
    case 'passkey_cancelled':
      return 'signIn.passkey.incomplete';
    case 'passkey_unavailable':
      return 'signIn.passkey.unsupported';
    // avibe.bot has passkey sign-in switched off (its 503).
    case 'provider_unavailable':
      return 'mobileApp.provider.unavailable';
    case 'invalid':
      return 'signIn.passkey.failed';
    case 'rate_limited':
      return 'mobileApp.code.rateLimited';
    default:
      return 'mobileApp.errors.network';
  }
};

export const translationKey = (key: SignInErrorKey): TranslationKey => key as TranslationKey;
