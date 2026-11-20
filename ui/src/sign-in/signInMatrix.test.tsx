// @vitest-environment jsdom
/**
 * AUTH-SETUP-182: one sign-in flow, run against both apps' adapters. Every case drives the same
 * steps through the iPhone adapter (the real `avibeMobile` bridge over a fake native coordinator)
 * and the Desktop adapter (`DesktopSignIn` over a fake native client that publishes its state the
 * way the runtime host does), and expects the same screens and the same words. The fakes stand in
 * for native: they prove the wiring, not a real account, provider or device.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance, type i18n as I18n } from 'i18next';
import { useCallback, useEffect, useState } from 'react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { DesktopSignIn } from '@/desktop/DesktopSignIn';
import { isNewerClientState, type DesktopClientAction, type DesktopClientReply, type DesktopClientState } from '@/desktop/bridge';
import en from '@/i18n/en.json';
import { mobileSignInAdapter } from '@/mobile/signInAdapter';
import type { MobileSessionState } from '@/mobile/bridge';
import { advertisedSignInMethods, EMAIL_ONLY } from '@/mobile/useSignInMethods';

import { SignInChrome, type SignInFrameProps } from './chromeContext';
import { useOperationOwner } from './operationOwner';
import { type SignInBusyAction, useSignInFlow } from './useSignInFlow';

const EMAIL = 'max@example.test';
const CODE = '123456';
const PASSWORD = 'correct horse';
const TOTP = '654321';
const RECOVERY = 'k7m2p-9xq4r';

/** What the account service answers; both fakes read it, so both apps meet the same service. */
type Service = {
  twoFactor: boolean;
  sendRetryAfter: number | null;
  factorRetryAfter: number | null;
  passkey: 'cancel' | 'ok';
};
const service = (overrides: Partial<Service> = {}): Service => ({
  twoFactor: false,
  sendRetryAfter: null,
  factorRetryAfter: null,
  passkey: 'ok',
  ...overrides,
});

type Deferred = { release: () => void };

/** A native call held open until the case releases it. */
const holds: { verify: Deferred | null } = { verify: null };
const hold = () => new Promise<void>((resolve) => {
  holds.verify = { release: resolve };
});

// --- iPhone: the real bridge over a fake `avibeMobile` coordinator ---------------------------

function installIosNative(account: Service, options: { holdVerify?: boolean; signIn?: NativeSignIn | null } = {}) {
  // What native read from avibe.bot's public switches (`null`: the fields are missing).
  const switches = options.signIn === undefined ? ALL_WAYS : options.signIn;
  let state: 'signedOut' | 'twoFactor' | 'signedIn' = 'signedOut';
  let generation = 0;
  // Each signed-out answer carries what native read of avibe.bot's switches (#3945's `providers`).
  const session = () => state === 'signedIn'
    ? { status: 'signedIn', account: { id: 'acct', email: EMAIL }, instances: [], selected_instance_id: null }
    : state === 'twoFactor'
      ? { status: 'twoFactorRequired', email: EMAIL, gate: 'challenge', methods: ['passkey', 'totp'] }
      : { status: 'signedOut', ...(switches ?? {}) };
  const firstFactor = () => {
    state = account.twoFactor ? 'twoFactor' : 'signedIn';
    return session();
  };
  const call = vi.fn(async (method: string, params: Record<string, unknown> = {}) => {
    switch (method) {
      case 'auth.requestOtp':
        if (account.sendRetryAfter) {
          throw { code: 'rate_limited', message: 'limited', retry_after_seconds: account.sendRetryAfter };
        }
        return { cooldown_seconds: 0 };
      case 'auth.verifyOtp': {
        const started = generation;
        if (options.holdVerify) await hold();
        // Native fences the attempt: an account change while it ran ends it.
        if (started !== generation) throw { code: 'stale_session', message: 'stale' };
        if (params.code !== CODE) throw { code: 'invalid_or_expired', message: 'invalid' };
        return firstFactor();
      }
      case 'auth.signInWithPassword':
        if (params.password !== PASSWORD) throw { code: 'invalid_credentials', message: 'invalid' };
        return firstFactor();
      case 'auth.verifySecondFactor':
        if (account.factorRetryAfter) {
          throw { code: 'rate_limited', message: 'limited', retry_after_seconds: account.factorRetryAfter };
        }
        if (params.code !== (params.method === 'totp' ? TOTP : RECOVERY)) throw { code: 'invalid', message: 'invalid' };
        state = 'signedIn';
        return session();
      case 'session.bootstrap':
        return session();
      case 'auth.signInWithProvider':
      case 'auth.signInWithPasskey':
        return firstFactor();
      case 'auth.verifyPasskey':
        if (account.passkey === 'cancel') throw { code: 'passkey_cancelled', message: 'cancelled' };
        state = 'signedIn';
        return session();
      case 'session.logout':
        generation += 1;
        state = 'signedOut';
        return session();
      default:
        throw { code: 'unsupported', message: method };
    }
  });
  vi.stubGlobal('avibeMobile', { contract: 1, call });
  return {
    call,
    /** Another surface signs this account out while the page waits. */
    accountChange: () => {
      generation += 1;
      state = 'signedOut';
    },
  };
}

const iosFrame = ({ children, back }: SignInFrameProps) => (
  <main>
    {back ? <button type="button" disabled={back.disabled} onClick={back.onBack}>{en.common.back}</button> : null}
    {children}
  </main>
);

function IosHost({ resetSignal }: { resetSignal: number }) {
  const owner = useOperationOwner<SignInBusyAction>();
  const [signedIn, setSignedIn] = useState(false);
  // As the App's page does: the ways in come from the latest signed-out answer that names them.
  const [answer, setAnswer] = useState<MobileSessionState>({ status: 'signedOut' });
  useEffect(() => {
    void window.avibeMobile?.call<MobileSessionState>('session.bootstrap').then(setAnswer);
  }, []);
  const methods = (answer.status === 'signedOut' ? advertisedSignInMethods(answer) : null) ?? EMAIL_ONLY;
  const flow = useSignInFlow<Awaited<ReturnType<typeof mobileSignInAdapter.verifyCode>>['session'], never>({
    adapter: mobileSignInAdapter,
    methods,
    owner,
    onSession: (session) => {
      setSignedIn(session.status === 'signedIn');
      if (session.status === 'signedOut') setAnswer(session);
    },
    // The App signs out for another account; the page follows the signed-out session.
    onDifferentAccount: () => {
      void window.avibeMobile?.call<MobileSessionState>('session.logout').then((next) => {
        setAnswer(next);
        flow.reset();
      });
    },
    onOpenSecurity: () => undefined,
  });
  const { reset } = flow;
  const { retireOperation } = owner;
  useEffect(() => {
    if (resetSignal === 0) return;
    // The shell's answer to a sign-out elsewhere (iPhone: `showSignedOut`).
    retireOperation();
    reset();
  }, [reset, resetSignal, retireOperation]);
  if (signedIn) return <p>signed in</p>;
  return (
    <SignInChrome value={{ Frame: iosFrame, platform: 'ios' }}>
      {flow.render((actions) => <main>{actions}</main>)}
    </SignInChrome>
  );
}

// --- Desktop: DesktopSignIn over a fake native client ----------------------------------------

const desktopState = (generation: number, account: DesktopClientState['account']): DesktopClientState => ({
  version: 1,
  revision: generation,
  generation,
  account,
  connection: { status: 'local', committed: { kind: 'local', name: 'Studio', instance_id: null }, pending: null },
  enrollment: { status: 'unpaired', instance_id: null, error_code: null },
  catalog: { status: 'idle', instances: [] },
  local: { name: 'Studio', remote_access: 'off' },
  desktop: {
    version: '3.2.7',
    update: { phase: 'idle', latest: null, channel: 'stable', skippedVersion: null, interactive: false },
  },
  consent: null,
  remote_auxiliary: 'local',
});

const signedOutAccount: DesktopClientState['account'] = {
  status: 'signed_out', identity: null, email: null, gate: null, methods: [],
};

type NativeSignIn = NonNullable<DesktopClientState['sign_in']>;
const ALL_WAYS: NativeSignIn = { providers: ['google', 'github', 'apple'], passkey: true };

function createDesktopNative(
  account: Service,
  options: {
    holdVerify?: boolean;
    holdProvider?: boolean;
    signIn?: NativeSignIn | null;
    /** The second-factor methods native publishes (it drops passkey where the sheet cannot run). */
    factorMethods?: ('passkey' | 'totp')[];
  } = {},
) {
  let releaseProvider: (() => void) | null = null;
  let generation = 1;
  // What avibe.bot's public switches say, once native has read them (`null`: the fields are missing).
  const switches = options.signIn === undefined ? ALL_WAYS : options.signIn;
  let signIn: NativeSignIn | undefined;
  let current = desktopState(generation, signedOutAccount);
  const listeners = new Set<(state: DesktopClientState) => void>();
  const publish = (next: DesktopClientState['account'], advance: boolean) => {
    if (advance) generation += 1;
    current = desktopState(generation, next);
    current = { ...current, revision: current.revision + 1, ...(signIn ? { sign_in: signIn } : {}) };
    // The broadcast reaches the page before the reply, as the runtime host may order them.
    listeners.forEach((listener) => listener(current));
  };
  const reply = (ok: boolean, code?: string, retry?: number): DesktopClientReply => ok
    ? { version: 1, request_id: 'r', ok: true, state: current }
    : { version: 1, request_id: 'r', ok: false, state: current, error: { code: code ?? 'request_failed', ...(retry ? { retry_after_seconds: retry } : {}) } };
  const firstFactor = () => publish(account.twoFactor
    ? { status: 'second_factor_required', identity: null, email: EMAIL, gate: 'challenge', methods: options.factorMethods ?? ['passkey', 'totp'] }
    : { status: 'signed_in', identity: { id: 'acct', email: EMAIL }, email: EMAIL, gate: null, methods: [] }, true);
  const signedIn = () => publish(
    { status: 'signed_in', identity: { id: 'acct', email: EMAIL }, email: EMAIL, gate: null, methods: [] },
    true,
  );
  const send = vi.fn(async (action: DesktopClientAction) => {
    const started = generation;
    switch (action.type) {
      // Native starts every first factor from signed out, which drops a code attempt the page
      // stepped back from, and publishes that state even when the call then fails.
      case 'request_otp':
        if (account.sendRetryAfter) {
          publish(signedOutAccount, true);
          return reply(false, 'rate_limited', account.sendRetryAfter);
        }
        publish({ ...signedOutAccount, status: 'verifying', email: action.email }, true);
        return reply(true);
      case 'verify_otp':
        if (options.holdVerify) await hold();
        // A newer generation retired this call: `useAccountClient` answers `null`.
        if (started !== generation) return null;
        // Supabase's answer for a wrong email code, passed through by native.
        if (action.code !== CODE) return reply(false, 'otp_expired');
        firstFactor();
        return reply(true);
      case 'sign_in_password':
        if (action.password !== PASSWORD) {
          publish(signedOutAccount, true);
          return reply(false, 'invalid_credentials');
        }
        firstFactor();
        return reply(true);
      case 'verify_second_factor':
        if (account.factorRetryAfter) return reply(false, 'rate_limited', account.factorRetryAfter);
        if (action.code !== (action.method === 'totp' ? TOTP : RECOVERY)) return reply(false, 'invalid');
        signedIn();
        return reply(true);
      case 'verify_passkey':
        if (account.passkey === 'cancel') return reply(false, 'passkey_cancelled');
        signedIn();
        return reply(true);
      case 'sign_in_with_provider':
      case 'sign_in_with_passkey':
        if (options.holdProvider) {
          await new Promise<void>((resolve) => {
            releaseProvider = resolve;
          });
        }
        if (started !== generation) return null;
        firstFactor();
        return reply(true);
      case 'abort_sign_in':
        publish(signedOutAccount, true);
        releaseProvider?.();
        return reply(true);
      case 'refresh_sign_in_methods':
        signIn = switches ?? { providers: [], passkey: false };
        if (switches) publish(current.account, false);
        return reply(true);
      case 'cancel':
        publish(signedOutAccount, true);
        return reply(true);
      default:
        return reply(false, 'invalid_action');
    }
  });
  return {
    send,
    get state() {
      return current;
    },
    subscribe: (listener: (state: DesktopClientState) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    /** Another window cancels while the page waits. */
    accountChange: () => publish(signedOutAccount, true),
  };
}

function DesktopHost({ native }: { native: ReturnType<typeof createDesktopNative> }) {
  const [state, setState] = useState(native.state);
  useEffect(() => native.subscribe(setState), [native]);
  // As `useAccountClient` does: a reply's state counts as well as the broadcast.
  const send = useCallback(async (action: DesktopClientAction) => {
    const reply = await native.send(action);
    if (reply) setState((previous) => (isNewerClientState(reply.state, previous) ? reply.state : previous));
    return reply;
  }, [native]);
  if (state.account.status === 'signed_in') return <p>signed in</p>;
  return (
    <DesktopSignIn
      state={state}
      send={send}
      onLocal={() => undefined}
      onDifferentAccount={() => void native.send({ type: 'cancel' })}
      onOpenSecurity={() => undefined}
      renderEnroll={() => null}
    />
  );
}

// --- The matrix ---------------------------------------------------------------------------------

let i18n: I18n;
beforeAll(async () => {
  i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  holds.verify = null;
});

type Harness = {
  name: 'iPhone' | 'Desktop';
  mount: (account: Service, options?: { holdVerify?: boolean; signIn?: NativeSignIn | null }) => { accountChange: () => void };
};

const HARNESSES: Harness[] = [
  {
    name: 'iPhone',
    mount: (account, options) => {
      const native = installIosNative(account, options);
      let signal = 0;
      const view = render(<I18nextProvider i18n={i18n}><IosHost resetSignal={signal} /></I18nextProvider>);
      return {
        accountChange: () => {
          native.accountChange();
          signal += 1;
          view.rerender(<I18nextProvider i18n={i18n}><IosHost resetSignal={signal} /></I18nextProvider>);
        },
      };
    },
  },
  {
    name: 'Desktop',
    mount: (account, options) => {
      const native = createDesktopNative(account, options);
      render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
      return { accountChange: () => native.accountChange() };
    },
  },
];

const click = (name: string) => fireEvent.click(screen.getByRole('button', { name }));
const startEmail = () => {
  click(en.mobileApp.welcome.continueWithEmail);
  fireEvent.change(screen.getByLabelText(en.mobileApp.email.label), { target: { value: EMAIL } });
};
const typeCode = async (value: string) => {
  await act(async () => {
    fireEvent.change(screen.getByLabelText(en.mobileApp.code.label), { target: { value } });
  });
};

describe.each(HARNESSES)('shared sign-in on $name', ({ name, mount }) => {
  const title = (behavior: string) => `AUTH-SETUP-182 ${name}: ${behavior}`;
  it(title('signs in with an email code on the same steps'), async () => {
    mount(service());
    expect(screen.getByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
    startEmail();
    click(en.mobileApp.email.send);
    expect(await screen.findByText(en.mobileApp.code.title)).toBeTruthy();
    expect(screen.getByText(EMAIL)).toBeTruthy();
    await typeCode(CODE);
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  it(title('says a wrong code is wrong, then takes the right one'), async () => {
    mount(service());
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    await typeCode('111111');
    expect(await screen.findByText(en.mobileApp.code.invalid)).toBeTruthy();
    await typeCode(CODE);
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  it(title('holds sending for the time avibe.bot names'), async () => {
    mount(service({ sendRetryAfter: 30 }));
    startEmail();
    click(en.mobileApp.email.send);
    expect(await screen.findByText(en.mobileApp.code.rateLimited)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Try again in 30s/ })).toBeTruthy();
  });

  it(title('signs in with a password and an authenticator code'), async () => {
    mount(service({ twoFactor: true }));
    startEmail();
    click(en.mobileApp.password.entry);
    fireEvent.change(screen.getByLabelText(en.mobileApp.password.label), { target: { value: PASSWORD } });
    click(en.mobileApp.password.signIn);
    expect(await screen.findByText(en.mobileApp.twoFactor.title)).toBeTruthy();
    // Passkey first when the account has one, as on both apps.
    expect(screen.getByRole('button', { name: en.mobileApp.twoFactor.usePasskey })).toBeTruthy();
    click(en.mobileApp.twoFactor.useTotp);
    await act(async () => {
      fireEvent.change(screen.getByLabelText(en.mobileApp.twoFactor.totpLabel), { target: { value: TOTP } });
    });
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  it(title('says a wrong password is wrong'), async () => {
    mount(service());
    startEmail();
    click(en.mobileApp.password.entry);
    fireEvent.change(screen.getByLabelText(en.mobileApp.password.label), { target: { value: 'nope' } });
    click(en.mobileApp.password.signIn);
    expect(await screen.findByText(en.mobileApp.password.invalid)).toBeTruthy();
  });

  it(title('keeps a wrong password on its step after stepping back from a sent code'), async () => {
    mount(service());
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    // Back to the email step: native still holds the code attempt until a new first factor starts.
    click(en.common.back);
    click(en.mobileApp.password.entry);
    fireEvent.change(screen.getByLabelText(en.mobileApp.password.label), { target: { value: 'nope' } });
    click(en.mobileApp.password.signIn);
    expect(await screen.findByText(en.mobileApp.password.invalid)).toBeTruthy();
    expect(screen.getByLabelText(en.mobileApp.password.label)).toBeTruthy();
  });

  it(title('keeps a refused send on its step after stepping back from a sent code'), async () => {
    const account = service();
    mount(account);
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    click(en.common.back);
    account.sendRetryAfter = 30;
    click(en.mobileApp.email.send);
    expect(await screen.findByText(en.mobileApp.code.rateLimited)).toBeTruthy();
    expect(screen.getByLabelText(en.mobileApp.email.label)).toBeTruthy();
  });

  it(title('explains a closed passkey prompt, then signs in with a recovery code'), async () => {
    mount(service({ twoFactor: true, passkey: 'cancel' }));
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    await typeCode(CODE);
    await screen.findByText(en.mobileApp.twoFactor.title);
    click(en.mobileApp.twoFactor.usePasskey);
    expect(await screen.findByText(en.mobileApp.twoFactor.passkeyFailed)).toBeTruthy();
    click(en.mobileApp.twoFactor.useRecovery);
    fireEvent.change(screen.getByLabelText(en.mobileApp.twoFactor.recoveryLabel), { target: { value: RECOVERY } });
    click(en.mobileApp.twoFactor.verify);
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  it(title('names the wait after too many second-factor attempts'), async () => {
    mount(service({ twoFactor: true, factorRetryAfter: 120 }));
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    await typeCode(CODE);
    await screen.findByText(en.mobileApp.twoFactor.title);
    click(en.mobileApp.twoFactor.useTotp);
    await act(async () => {
      fireEvent.change(screen.getByLabelText(en.mobileApp.twoFactor.totpLabel), { target: { value: TOTP } });
    });
    expect(await screen.findByText('Too many attempts. Try again in 2 minutes.')).toBeTruthy();
  });

  it(title('leaves an owed second factor for another account'), async () => {
    mount(service({ twoFactor: true }));
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    await typeCode(CODE);
    await screen.findByText(en.mobileApp.twoFactor.title);
    click(en.mobileApp.twoFactor.differentAccount);
    expect(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
  });

  it(title('drops a verification an account change ended while it ran'), async () => {
    const mounted = mount(service(), { holdVerify: true });
    startEmail();
    click(en.mobileApp.email.send);
    await screen.findByText(en.mobileApp.code.title);
    await typeCode(CODE);
    await waitFor(() => expect(holds.verify).not.toBeNull());
    act(() => mounted.accountChange());
    expect(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
    await act(async () => holds.verify?.release());
    // The late answer neither signs in nor speaks on the first step.
    expect(screen.queryByText('signed in')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
  });

  it(title('signs in with Google from the first step'), async () => {
    mount(service());
    fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithGoogle }));
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  it(title('signs in with a passkey and no email'), async () => {
    mount(service());
    fireEvent.click(await screen.findByRole('button', { name: en.signIn.passkey.use }));
    expect(await screen.findByText(en.signIn.passkey.title)).toBeTruthy();
    expect(screen.queryByLabelText(en.mobileApp.email.label)).toBeNull();
    click(en.signIn.passkey.use);
    expect(await screen.findByText('signed in')).toBeTruthy();
  });

  // What native reports is avibe.bot's public switches (and, for passkey sign-in, this system's
  // passkey sheet): the page offers exactly the ways that are on, and email always.
  const ways = {
    google: en.mobileApp.welcome.continueWithGoogle,
    github: en.mobileApp.welcome.continueWithGitHub,
    apple: en.mobileApp.welcome.continueWithApple,
  } as const;
  it.each<[string, NativeSignIn | null, NativeSignIn]>([
    ['every way', ALL_WAYS, ALL_WAYS],
    ['only Google', { providers: ['google'], passkey: false }, { providers: ['google'], passkey: false }],
    ['Apple off', { providers: ['google', 'github'], passkey: false }, { providers: ['google', 'github'], passkey: false }],
    ['all off', { providers: [], passkey: false }, { providers: [], passkey: false }],
    ['missing switches', null, { providers: [], passkey: false }],
    ['only passkey', { providers: [], passkey: true }, { providers: [], passkey: true }],
  ])(title('offers %s as native reports it'), async (_case, reported, expected) => {
    mount(service(), { signIn: reported });
    const email = await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithEmail });
    // The switches arrive after the first paint; judge once the last expected way has shown.
    if (expected.passkey) await screen.findByRole('button', { name: en.signIn.passkey.use });
    for (const provider of expected.providers) await screen.findByRole('button', { name: ways[provider] });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(email).toBeTruthy();
    expect((Object.keys(ways) as (keyof typeof ways)[]).filter((provider) => screen.queryByRole('button', { name: ways[provider] })))
      .toEqual(expected.providers);
    expect(Boolean(screen.queryByRole('button', { name: en.signIn.passkey.use }))).toBe(expected.passkey);
  });
});

describe('Desktop only: the default browser and the passkey sheet', () => {
  const marks = [en.mobileApp.welcome.continueWithGoogle, en.mobileApp.welcome.continueWithGitHub, en.mobileApp.welcome.continueWithApple];

  it('AUTH-SETUP-183 Desktop: offers only the ways avibe.bot switches on', async () => {
    const native = createDesktopNative(service(), { signIn: { providers: ['google'], passkey: false } });
    render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    expect(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithGoogle })).toBeTruthy();
    expect(native.send).toHaveBeenCalledWith({ type: 'refresh_sign_in_methods' });
    expect(screen.queryByRole('button', { name: en.mobileApp.welcome.continueWithGitHub })).toBeNull();
    expect(screen.queryByRole('button', { name: en.mobileApp.welcome.continueWithApple })).toBeNull();
    expect(screen.queryByRole('button', { name: en.signIn.passkey.use })).toBeNull();
  });

  it('AUTH-SETUP-183 Desktop: missing switches offer email only', async () => {
    const native = createDesktopNative(service(), { signIn: null });
    render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    await waitFor(() => expect(native.send).toHaveBeenCalledWith({ type: 'refresh_sign_in_methods' }));
    expect(screen.getByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
    for (const mark of marks) expect(screen.queryByRole('button', { name: mark })).toBeNull();
    expect(screen.queryByRole('button', { name: en.signIn.passkey.use })).toBeNull();
  });

  it('AUTH-SETUP-183 Desktop: leaving the page during a browser sign-in ends it', async () => {
    const native = createDesktopNative(service(), { holdProvider: true });
    const view = render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithGitHub }));
    expect(await screen.findByText(en.signIn.provider.continueInBrowser)).toBeTruthy();
    view.unmount();
    expect(native.send).toHaveBeenCalledWith({ type: 'abort_sign_in' });
  });

  it('AUTH-SETUP-183 Desktop: ends a browser sign-in from the page and says nothing', async () => {
    const native = createDesktopNative(service(), { holdProvider: true });
    render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithGitHub }));
    expect(await screen.findByText(en.signIn.provider.continueInBrowser)).toBeTruthy();
    click(en.common.cancel);
    await waitFor(() => expect(screen.queryByText(en.signIn.provider.continueInBrowser)).toBeNull());
    expect(native.send).toHaveBeenCalledWith({ type: 'abort_sign_in' });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('signed in')).toBeNull();
    expect((screen.getByRole('button', { name: en.mobileApp.welcome.continueWithEmail }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('AUTH-SETUP-184 Desktop: a second factor starts where this system can, with no passkey step', async () => {
    const native = createDesktopNative(service({ twoFactor: true }), { factorMethods: ['totp'] });
    render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.welcome.continueWithEmail }));
    fireEvent.change(screen.getByLabelText(en.mobileApp.email.label), { target: { value: EMAIL } });
    click(en.mobileApp.password.entry);
    fireEvent.change(screen.getByLabelText(en.mobileApp.password.label), { target: { value: PASSWORD } });
    click(en.mobileApp.password.signIn);
    expect(await screen.findByText(en.mobileApp.twoFactor.title)).toBeTruthy();
    expect(screen.queryByRole('button', { name: en.mobileApp.twoFactor.usePasskey })).toBeNull();
    expect(screen.getByLabelText(en.mobileApp.twoFactor.totpLabel)).toBeTruthy();
    expect(native.send).not.toHaveBeenCalledWith({ type: 'verify_passkey' });
  });

  it('AUTH-SETUP-184 Desktop: signs in with a passkey and no email', async () => {
    const native = createDesktopNative(service());
    render(<I18nextProvider i18n={i18n}><DesktopHost native={native} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: en.signIn.passkey.use }));
    expect(await screen.findByText(en.signIn.passkey.title)).toBeTruthy();
    expect(screen.queryByLabelText(en.mobileApp.email.label)).toBeNull();
    click(en.signIn.passkey.use);
    expect(await screen.findByText('signed in')).toBeTruthy();
    expect(native.send).toHaveBeenCalledWith({ type: 'sign_in_with_passkey' });
  });
});
