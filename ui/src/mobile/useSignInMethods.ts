import { SIGN_IN_PROVIDERS, type SignInMethods } from '@/sign-in/types';

/** Email only: until avibe.bot's switches were read once. */
export const EMAIL_ONLY: SignInMethods = { providers: [], passkey: false };

/**
 * The ways in an answer names. Native reads avibe.bot's public configuration and answers the
 * providers its mobile broker accepts (`providers`) and whether passkey sign-in is on (`passkey`),
 * read now when that answers in time, otherwise the last it advertised; signed-out session answers
 * and `auth.providers` both carry them. One read yields both, so an answer names both or neither.
 * Only exact provider names and `true` count. `null` when it names none (never read): the page
 * keeps what it offers.
 */
export const advertisedSignInMethods = (
  answer: { providers?: unknown; passkey?: unknown },
): SignInMethods | null => {
  if (!Array.isArray(answer.providers)) return null;
  const named: unknown[] = answer.providers;
  return {
    providers: SIGN_IN_PROVIDERS.filter((provider) => named.includes(provider)),
    passkey: answer.passkey === true,
  };
};
