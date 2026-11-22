import type { SignInAdapter, SignInOutcome, SignInStep } from '@/sign-in/types';

import {
  refreshMobileSession,
  requestMobileOtp,
  signInMobileWithPasskey,
  signInMobileWithPassword,
  signInMobileWithProvider,
  type MobileSessionState,
  verifyMobileOtp,
  verifyMobilePasskey,
  verifyMobileSecondFactor,
} from './bridge';
import { EMAIL_ONLY } from './useSignInMethods';

/** The flow's step for the App's session. */
export const mobileSignInStep = (session: MobileSessionState): SignInStep => {
  if (session.status === 'signedOut') return { status: 'signedOut' };
  if (session.status === 'twoFactorRequired') {
    return { status: 'secondFactor', email: session.email, gate: session.gate, methods: session.methods };
  }
  return { status: 'signedIn' };
};

const outcome = (session: MobileSessionState): SignInOutcome<MobileSessionState> => ({
  step: mobileSignInStep(session),
  session,
});

/**
 * The iPhone's half of the shared sign-in flow: each call is one closed `avibeMobile` method,
 * whose native coordinator owns the account generation, the WebKit store and late results.
 */
export const mobileSignInAdapter: SignInAdapter<MobileSessionState> = {
  platform: 'ios',
  // Email only by itself: the page passes what `useMobileSignInMethods` read from native.
  methods: EMAIL_ONLY,
  requestCode: (email) => requestMobileOtp(email),
  verifyCode: async (email, code) => outcome(await verifyMobileOtp(email, code)),
  signInWithPassword: async (email, password) => {
    const { weak_password: weakPassword, ...session } = await signInMobileWithPassword(email, password);
    return {
      ...outcome(session as MobileSessionState),
      ...(weakPassword ? { weakPassword: weakPassword.reasons } : {}),
    };
  },
  signInWithProvider: async (provider) => outcome(await signInMobileWithProvider(provider)),
  signInWithPasskey: async () => outcome(await signInMobileWithPasskey()),
  verifySecondFactor: async (method, code) => outcome(await verifyMobileSecondFactor(method, code)),
  verifySecondFactorPasskey: async () => outcome(await verifyMobilePasskey()),
  refresh: async () => outcome(await refreshMobileSession()),
};
