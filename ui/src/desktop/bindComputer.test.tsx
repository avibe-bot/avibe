// @vitest-environment jsdom
// AV-8: binding this computer from the Account page. Native owns every decision; these tests hold
// the page to its half: what it calls on its own (two reads), what only a press calls, and what it
// draws for each native answer.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';
import { desktopEn as en } from '../i18n/desktop';
import { AccountApp } from './AccountApp';
import { DESKTOP_CLIENT_STATE_EVENT, type DesktopClientRequest, type DesktopClientState } from './bridge';
import states from './__fixtures__/instance-client-v1/states.json';
import replies from './__fixtures__/instance-client-v1/replies.json';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); vi.unstubAllGlobals(); vi.useRealTimers(); });

const copy = en.desktopAccount.bind;
const signedIn = states.signed_in as unknown as DesktopClientState;
type Action = { type: string } & Record<string, unknown>;
type Answer = unknown | ((action: Action) => unknown);
/** Throwing a `{ code }` is how a Tauri command refuses. */
const refuse = (code: string) => () => { throw { code }; };

/** Every action that can reach avibe.bot with a decision, or resume a sign-in. */
const DECIDING = ['confirm', 'send_proof_code', 'submit_proof_code', 'submit_factor_code', 'submit_factor_passkey', 'finish_totp_setup', 'resume'];

const review = (screen: Record<string, unknown>, revision = 'rev-1') => ({
  revision, account_email: 'xiaoming@example.test', device_name: '小明的 Mac', screen,
});
const NEW = (prefix: string | null, extra: Record<string, unknown> = {}) => ({ state: 'new', prefilled_prefix: 'xiaoming', prefix, taken: false, names: null, ...extra });

function mount(answers: Record<string, Answer>, state: DesktopClientState = signedIn, documentView = 'account') {
  window.history.replaceState(null, '', `/account/index.html?view=${documentView}`);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', state);
  const actions: Action[] = [];
  const invoke = vi.fn(async (command: string, { request }: { request: { action: Action } & Partial<DesktopClientRequest> }) => {
    if (command === 'desktop_client_request') return { ...replies.success, state, request_id: request.request_id };
    const action = request.action;
    actions.push(action);
    const key = command === 'desktop_account_recovery' ? `recovery:${action.type}` : action.type;
    const answer = key in answers ? answers[key] : key === 'recovery:inspect' ? { state: 'unavailable' } : key === 'observe_current_binding' ? { state: 'no_published_binding' } : null;
    return typeof answer === 'function' ? (answer as (a: Action) => unknown)(action) : answer;
  });
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
  const view = render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  return { actions, types: () => actions.map((each) => each.type), view };
}

it('mounting, re-showing and reconnecting only read; nothing decides without a press', async () => {
  const deciding = Object.fromEntries(DECIDING.map((type) => [type, () => { throw new Error(`${type} without a press`); }]));
  const page = mount({ ...deciding, 'recovery:inspect': { state: 'available', receipt: 'rec-1' } });
  await screen.findByRole('button', { name: en.common.continue });
  expect(page.types().sort()).toEqual(['inspect', 'observe_current_binding']);
  // A newer snapshot of the same account (a reconnect, a broadcast) reads nothing again.
  await act(async () => { window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: { ...signedIn, revision: signedIn.revision + 1 } })); });
  // Showing the page again is a new document: it reads again, and still decides nothing.
  cleanup();
  const again = mount({ ...deciding, 'recovery:inspect': { state: 'available', receipt: 'rec-1' } });
  await screen.findByRole('button', { name: en.common.continue });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect([...page.types(), ...again.types()].filter((type) => DECIDING.includes(type))).toEqual([]);
  expect(again.types().filter((type) => type === 'open' || type === 'read')).toEqual([]);
});

it.each([
  ['owner_observed', 'bound'],
  ['no_published_binding', 'unbound'],
  ['bound_owner_unconfirmed', 'unconfirmed'],
  ['legacy_wait', 'pending'],
  ['legacy_recovery_required', 'repair'],
])('shows what native observed: %s', async (answer, drawn) => {
  mount({ observe_current_binding: { state: answer } });
  await waitFor(() => expect(document.querySelector(`[data-computer-binding="${drawn}"]`)).toBeTruthy());
});

it('a failed read of the binding is unknown, never unbound', async () => {
  mount({ observe_current_binding: refuse('network_unavailable') });
  await screen.findByText(copy.unknown);
  expect(screen.queryByRole('button', { name: copy.action })).toBeNull();
});

it('binds the exact name the person saw, only when they press Bind', async () => {
  const page = mount({
    open: { receipt: 'receipt-1' },
    read: review(NEW(null)),
    check: (action: Action) => review(NEW(action.prefix as string), 'rev-2'),
    confirm: { state: 'bound' },
  });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  await screen.findByText('The domain is xiaoming-app.avibe.bot.');
  expect(page.types().filter((type) => type !== 'observe_current_binding' && type !== 'inspect')).toEqual(['open', 'read', 'check']);
  fireEvent.click(screen.getByRole('button', { name: copy.confirm }));
  await screen.findByRole('heading', { name: copy.boundTitle });
  expect(page.actions.find((each) => each.type === 'confirm')).toEqual({ type: 'confirm', receipt: 'receipt-1', revision: 'rev-2' });
  expect(screen.getByText('xiaoming-app.avibe.bot')).toBeTruthy();
});

it('a name someone else took stays editable and gets no number added', async () => {
  mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: { state: 'name_taken', revision: 'rev-edit' } });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByText(en.devices.claim.taken);
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('xiaoming');
  expect((screen.getByRole('button', { name: copy.confirm }) as HTMLButtonElement).disabled).toBe(true);
});

it('sends the code only on its own press, keeps a wrong code in place, and binds after verification', async () => {
  let codes = 0;
  const page = mount({
    open: { receipt: 'r' }, read: review(NEW('xiaoming')),
    confirm: { state: 'confirmation_required', offer: 'offer-1' },
    send_proof_code: { state: 'mail_accepted', delivery: 'delivery-1' },
    submit_proof_code: () => { codes += 1; if (codes === 1) throw { code: 'invalid_or_expired' }; return { state: 'verification_accepted', submission: 'sub-1' }; },
    review_after_proof: { state: 'bound' },
  });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByRole('heading', { name: copy.proofTitle });
  expect(page.types()).not.toContain('send_proof_code');
  fireEvent.click(screen.getByRole('button', { name: copy.sendCode }));
  const field = await screen.findByLabelText(en.mobileApp.code.label);
  fireEvent.change(field, { target: { value: '111111' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(copy.problem.codeInvalid);
  // Still the code step, with the same email's delivery; not a restart.
  fireEvent.change(screen.getByLabelText(en.mobileApp.code.label), { target: { value: '222222' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByRole('heading', { name: copy.boundTitle });
  const submits = page.actions.filter((each) => each.type === 'submit_proof_code');
  expect(submits.map((each) => [each.delivery, each.code])).toEqual([['delivery-1', '111111'], ['delivery-1', '222222']]);
  expect(page.types().filter((type) => type === 'open' || type === 'confirm' || type === 'send_proof_code')).toEqual(['open', 'confirm', 'send_proof_code']);
  expect(page.actions.find((each) => each.type === 'review_after_proof')).toEqual({ type: 'review_after_proof', receipt: 'r', submission: 'sub-1' });
});

it('after verification, a changed review says what changed and waits for another press', async () => {
  mount({
    open: { receipt: 'r' }, read: review(NEW('xiaoming')),
    confirm: { state: 'confirmation_required', offer: 'o' },
    send_proof_code: { state: 'mail_accepted', delivery: 'd' },
    submit_proof_code: { state: 'verification_accepted', submission: 's' },
    review_after_proof: review(NEW('xiaoming', { taken: true }), 'rev-3'),
  });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  fireEvent.click(await screen.findByRole('button', { name: copy.sendCode }));
  fireEvent.change(await screen.findByLabelText(en.mobileApp.code.label), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(copy.changed.replace('{{fields}}', copy.field.availability));
  await screen.findByText(en.devices.claim.taken);
});

it('another account owning this computer is told, with nothing more called', async () => {
  const page = mount({ open: { receipt: 'r' }, read: review({ state: 'owner_conflict' }) });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  await screen.findByRole('heading', { name: copy.conflictTitle });
  expect(page.types().filter((type) => DECIDING.includes(type) || type === 'check')).toEqual([]);
});

it('an error is one sentence and one next step, never a code', async () => {
  mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: refuse('authentication_required') });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByText(copy.problem.signedOut);
  expect(screen.queryByText(/authentication_required/)).toBeNull();
  expect(screen.getByRole('button', { name: en.common.back })).toBeTruthy();
});

it('resumes an accepted sign-in only when the person presses Continue', async () => {
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-1' }, 'recovery:resume': { state: 'saved' } });
  const button = await screen.findByRole('button', { name: en.common.continue });
  expect(page.types()).not.toContain('resume');
  fireEvent.click(button);
  await waitFor(() => expect(page.actions.find((each) => each.type === 'resume')).toEqual({ type: 'resume', receipt: 'rec-1' }));
});

/** Opens binding and verifies a code, up to the moment avibe.bot accepted it. */
async function verify() {
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  fireEvent.click(await screen.findByRole('button', { name: copy.sendCode }));
  fireEvent.change(await screen.findByLabelText(en.mobileApp.code.label), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
}
const proofAnswers = {
  open: { receipt: 'r' }, read: review(NEW('xiaoming')),
  confirm: { state: 'confirmation_required', offer: 'o' },
  send_proof_code: { state: 'mail_accepted', delivery: 'd' },
  submit_proof_code: { state: 'verification_accepted', submission: 's' },
};
const broadcast = (state: DesktopClientState) => act(async () => {
  window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: state }));
});

it('waits for the accepted sign-in to be published before reading the ticket again', async () => {
  let published = false;
  const page = mount({ ...proofAnswers, review_after_proof: () => (published ? { state: 'bound' } : { state: 'publishing' }) });
  await verify();
  await screen.findByText(copy.saving);
  expect(screen.queryByText(copy.problem.expired)).toBeNull();
  published = true;
  await broadcast({ ...signedIn, revision: signedIn.revision + 5 });
  await screen.findByRole('heading', { name: copy.boundTitle });
  expect(page.types().filter((type) => type === 'submit_proof_code' || type === 'send_proof_code')).toEqual(['send_proof_code', 'submit_proof_code']);
});

it('a published sign-in that still owes a second factor asks for it, with the same submission', async () => {
  let published = false;
  const page = mount({ ...proofAnswers,
    review_after_proof: () => (published ? refuse('authentication_required')() : { state: 'publishing' }),
    submit_factor_code: { state: 'verification_accepted', submission: 's2' } });
  await verify();
  await screen.findByText(copy.saving);
  published = true;
  await broadcast({ ...signedIn, revision: signedIn.revision + 5,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp'] } });
  const field = await screen.findByLabelText(en.mobileApp.twoFactor.totpLabel, undefined, { timeout: 5000 });
  fireEvent.change(field, { target: { value: '654321' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await waitFor(() => expect(page.actions.find((each) => each.type === 'submit_factor_code'))
    .toEqual({ type: 'submit_factor_code', receipt: 'r', submission: 's', method: 'totp', code: '654321' }));
});

it.each(['a', 'ab--cd', `${'a'.repeat(47)}`, '-ab', 'ab-'])('keeps %s in the field and never checks it', async (typed) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  const field = await screen.findByRole('textbox');
  const checks = page.types().filter((type) => type === 'check').length;
  fireEvent.change(field, { target: { value: typed } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(page.types().filter((type) => type === 'check')).toHaveLength(checks);
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe(typed);
  expect(screen.getByText(copy.invalid)).toBeTruthy();
});

it('a resumed sign-in that still needs saving keeps its new receipt for the next press', async () => {
  let resumes = 0;
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-1' },
    'recovery:resume': () => (++resumes === 1 ? { state: 'available', receipt: 'rec-2' } : { state: 'saved' }) });
  fireEvent.click(await screen.findByRole('button', { name: en.common.continue }));
  await waitFor(() => expect(resumes).toBe(1));
  fireEvent.click(await screen.findByRole('button', { name: en.common.continue }));
  await waitFor(() => expect(page.actions.filter((each) => each.type === 'resume')).toEqual([
    { type: 'resume', receipt: 'rec-1' }, { type: 'resume', receipt: 'rec-2' }]));
});

it('another document never sends claim commands; its Account card opens a real Account document', async () => {
  const assign = vi.fn();
  const location = new Proxy({} as Location, { get: (_t, key) => key === 'assign' ? assign : Reflect.get(window.location, key) });
  const original = window;
  const receiver: Window = new Proxy(original, { get(target, key) {
    if (key === 'location') return location;
    if (key === 'top') return receiver;
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' && !String(key).startsWith('HTML') ? value.bind(target) : value;
  } });
  const page = mount({}, signedIn, 'instances');
  vi.stubGlobal('window', receiver);
  fireEvent.click(await screen.findByRole('button', { name: signedIn.account.identity!.email, exact: true }));
  expect(assign).toHaveBeenCalledWith('avibe://desktop/account?locale=en');
  expect(page.actions).toEqual([]);
});

it.each(['decision_changed', 'claim_in_progress'])('treats %s after verification as final, asking no more', async (code) => {
  const page = mount({ ...proofAnswers, review_after_proof: refuse(code) });
  await verify();
  await screen.findByText(code === 'claim_in_progress' ? copy.problem.busy : copy.problem.expired);
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1700)); });
  expect(page.types().filter((type) => type === 'review_after_proof')).toHaveLength(1);
});

it('asks again only while native says publishing, one request at a time', async () => {
  let inFlight = 0; let most = 0; let asks = 0;
  const page = mount({ ...proofAnswers, review_after_proof: async () => {
    inFlight += 1; most = Math.max(most, inFlight); asks += 1;
    await new Promise((resolve) => setTimeout(resolve, 5));
    inFlight -= 1;
    return asks < 3 ? { state: 'publishing' } : { state: 'bound' };
  } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 7 });
  await broadcast({ ...signedIn, revision: signedIn.revision + 8 });
  await screen.findByRole('heading', { name: copy.boundTitle }, { timeout: 5000 });
  expect(most).toBe(1);
  expect(page.types().filter((type) => type === 'submit_proof_code')).toHaveLength(1);
});

it('a factor accepted while an earlier snapshot still owes one waits for this sign-in, then reads the ticket once', async () => {
  const owing = { ...signedIn, account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp'] } } as DesktopClientState;
  let saved = false;
  let afterSave = 0;
  const page = mount({ ...proofAnswers,
    review_after_proof: (action: Action) => {
      if (action.submission === 's') return refuse('authentication_required')();
      if (!saved) return { state: 'publishing' };
      afterSave += 1;
      return { state: 'bound' };
    },
    submit_factor_code: { state: 'verification_accepted', submission: 's2' } });
  await verify();
  // The email code's sign-in is published and owes a factor.
  await broadcast({ ...owing, revision: signedIn.revision + 3 });
  fireEvent.change(await screen.findByLabelText(en.mobileApp.twoFactor.totpLabel), { target: { value: '654321' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(copy.saving);
  // The ordinary save lags: broadcasts still carry the snapshot that owes a factor.
  await broadcast({ ...owing, revision: signedIn.revision + 4 });
  await broadcast({ ...owing, revision: signedIn.revision + 5 });
  expect(screen.queryByLabelText(en.mobileApp.twoFactor.totpLabel)).toBeNull();
  saved = true;
  await broadcast({ ...signedIn, revision: signedIn.revision + 6 });
  await screen.findByRole('heading', { name: copy.boundTitle }, { timeout: 5000 });
  expect(page.types().filter((type) => type === 'submit_factor_code')).toHaveLength(1);
  expect(afterSave).toBe(1);
});

it.each(['passkey_cancelled', 'passkey_unavailable', 'invalid_passkey_assertion'])('a passkey that did not finish (%s) stays for another try with the same submission', async (code) => {
  let tries = 0;
  const page = mount({ ...proofAnswers,
    review_after_proof: (action: Action) => (action.submission === 's' ? refuse('authentication_required')() : { state: 'bound' }),
    submit_factor_passkey: () => (++tries === 1 ? refuse(code)() : { state: 'verification_accepted', submission: 's2' }) });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['passkey', 'totp'] } });
  fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.twoFactor.usePasskey }, { timeout: 5000 }));
  await screen.findByText(en.mobileApp.twoFactor.passkeyFailed);
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.usePasskey }));
  await screen.findByRole('heading', { name: copy.boundTitle }, { timeout: 5000 });
  expect(page.actions.filter((each) => each.type === 'submit_factor_passkey').map((each) => each.submission)).toEqual(['s', 's']);
});

it('an unverifiable passkey challenge is final and starts again', async () => {
  mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    submit_factor_passkey: refuse('invalid_passkey_challenge') });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['passkey'] } });
  fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.twoFactor.usePasskey }, { timeout: 5000 }));
  await screen.findByRole('button', { name: copy.restart });
});

it('a real 429 on a second factor waits in place with its own time', async () => {
  mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    submit_factor_passkey: () => { throw { code: 'rate_limited', retry_after_seconds: 120 }; } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['passkey', 'totp'] } });
  fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.twoFactor.usePasskey }, { timeout: 5000 }));
  await screen.findByText(en.mobileApp.twoFactor.rateLimited.replace('{{count}}', '2'));
  expect(screen.getByRole('button', { name: en.mobileApp.twoFactor.usePasskey })).toBeTruthy();
});

it('a new generation while an action is on its way leaves the page usable', async () => {
  mount({ open: () => new Promise(() => {}) });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  await screen.findByRole('heading', { name: copy.title });
  await broadcast({ ...signedIn, generation: signedIn.generation + 1, revision: signedIn.revision + 1 });
  const bind = await screen.findByRole('button', { name: copy.action });
  expect((bind as HTMLButtonElement).disabled).toBe(false);
});

it('a review that changed in no field it shows still says it changed, and waits for a press', async () => {
  const page = mount({ ...proofAnswers, review_after_proof: review(NEW('xiaoming', { prefilled_prefix: 'xiaoming-mac' }), 'rev-9') });
  await verify();
  await screen.findByText(copy.changedPlain);
  expect(page.types().filter((type) => type === 'confirm')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: copy.confirm }));
  await waitFor(() => expect(page.actions.filter((each) => each.type === 'confirm').at(-1)).toEqual({ type: 'confirm', receipt: 'r', revision: 'rev-9' }));
});

it('a 429 on the email code waits in place', async () => {
  mount({ ...proofAnswers, submit_proof_code: () => { throw { code: 'rate_limited', retry_after_seconds: 30 }; } });
  await verify();
  await screen.findByText(en.mobileApp.code.rateLimited);
  expect(screen.getByLabelText(en.mobileApp.code.label)).toBeTruthy();
});

it('a 429 keeps the step locked until its wait ends', async () => {
  let tries = 0;
  const page = mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    submit_factor_code: () => { tries += 1; if (tries === 1) throw { code: 'rate_limited', retry_after_seconds: 1 }; return { state: 'verification_accepted', submission: 's2' }; } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp'] } });
  fireEvent.change(await screen.findByLabelText(en.mobileApp.twoFactor.totpLabel, undefined, { timeout: 5000 }), { target: { value: '111111' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(/Try again in 1 minute/);
  expect((screen.getByLabelText(en.mobileApp.twoFactor.totpLabel) as HTMLInputElement).disabled).toBe(true);
  await waitFor(() => expect((screen.getByLabelText(en.mobileApp.twoFactor.totpLabel) as HTMLInputElement).disabled).toBe(false), { timeout: 3000 });
  expect(page.types().filter((type) => type === 'submit_factor_code')).toHaveLength(1);
});

it('keeps one name check on its way at a time while the person types', async () => {
  let inFlight = 0; let most = 0;
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')),
    check: async (action: Action) => {
      inFlight += 1; most = Math.max(most, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 600));
      inFlight -= 1;
      return review(NEW(action.prefix as string), `rev-${action.prefix as string}`);
    } });
  fireEvent.click(await screen.findByRole('button', { name: copy.action }));
  const field = await screen.findByRole('textbox');
  fireEvent.change(field, { target: { value: 'maxa' } });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 450)); });
  fireEvent.change(field, { target: { value: 'maxab' } });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 450)); });
  fireEvent.change(field, { target: { value: 'maxabc' } });
  await screen.findByText('The domain is maxabc-app.avibe.bot.', undefined, { timeout: 5000 });
  expect(most).toBe(1);
  expect(page.actions.filter((each) => each.type === 'check').at(-1)?.prefix).toBe('maxabc');
});

it('a publication that stopped after verification offers the account recovery, not a new code', async () => {
  let inspects = 0;
  const page = mount({ ...proofAnswers, review_after_proof: refuse('decision_changed'),
    'recovery:inspect': () => (++inspects === 1 ? { state: 'unavailable' } : { state: 'available', receipt: 'rec-9' }), 'recovery:resume': { state: 'saved' } });
  await verify();
  const go = await screen.findByRole('button', { name: en.common.continue }, { timeout: 5000 });
  expect(screen.getByText(copy.recovery)).toBeTruthy();
  fireEvent.click(go);
  await waitFor(() => expect(page.actions.find((each) => each.type === 'resume')).toEqual({ type: 'resume', receipt: 'rec-9' }));
  expect(page.types().filter((type) => type === 'send_proof_code')).toHaveLength(1);
});
