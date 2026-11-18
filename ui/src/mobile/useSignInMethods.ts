import { SIGN_IN_PROVIDERS, type SignInMethods } from '@/sign-in/types';

import type { MobileSessionState } from './bridge';

/** Email only: before avibe.bot's switches are read, and when they could not be. */
export const EMAIL_ONLY: SignInMethods = { providers: [], passkey: false };

/**
 * The ways in a session answer offers: native reads avibe.bot's public configuration once per
 * signed-out answer and names the providers its mobile broker accepts (`providers`) and whether
 * passkey sign-in is on (`passkey`). An answer that names nothing, or names it malformed, offers
 * email alone; so does any answer that is not signed out. A later answer always replaces it.
 */
export const mobileSignInMethods = (session: MobileSessionState): SignInMethods => {
  if (session.status !== 'signedOut') return EMAIL_ONLY;
  const named: unknown[] = Array.isArray(session.providers) ? session.providers : [];
  return {
    providers: SIGN_IN_PROVIDERS.filter((provider) => named.includes(provider)),
    passkey: session.passkey === true,
  };
};
