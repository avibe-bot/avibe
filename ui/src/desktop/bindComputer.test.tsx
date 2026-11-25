// @vitest-environment jsdom
// AV-8: binding this computer in the shell's claim view, which the Workbench's 「开启远程访问」
// opens. Native owns every decision; these tests hold the page to its half: what it calls on its
// own (observe, then one review only when nothing is bound), what only a press calls, and what it
// draws for each native answer. The Account page only reads.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';
import { desktopEn as en } from '../i18n/desktop';
import { AccountApp } from './AccountApp';
import { factorCode } from './useDesktopClaim';
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

function mount(answers: Record<string, Answer>, state: DesktopClientState = signedIn, documentView = 'claim') {
  window.history.replaceState(null, '', `/account/index.html?view=${documentView}`);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_STATE__', state);
  const actions: Action[] = [];
  const leaves: string[] = [];
  const invoke = vi.fn(async (command: string, { request }: { request: { action: Action } & Partial<DesktopClientRequest> }) => {
    if (command === 'desktop_client_request') {
      if (['cancel', 'return_local', 'sign_out'].includes(request.action.type)) leaves.push(request.action.type);
      return { ...replies.success, state, request_id: request.request_id };
    }
    const action = request.action;
    actions.push(action);
    const key = command === 'desktop_account_recovery' ? `recovery:${action.type}` : action.type;
    const answer = key in answers ? answers[key] : key === 'recovery:inspect' ? { state: 'unavailable' } : key === 'observe_current_binding' ? { state: 'no_published_binding' } : null;
    return typeof answer === 'function' ? (answer as (a: Action) => unknown)(action) : answer;
  });
  vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
  const view = render(<I18nextProvider i18n={i18n}><AccountApp /></I18nextProvider>);
  return { actions, leaves: () => [...leaves], types: () => actions.map((each) => each.type), view };
}

/** The claim view opens its review itself once it has observed nothing bound: wait for the dialog. */
const opened = async () => { await screen.findByRole('dialog'); };
const broadcast = (state: DesktopClientState) => act(async () => {
  window.dispatchEvent(new CustomEvent(DESKTOP_CLIENT_STATE_EVENT, { detail: state }));
});

it('the claim view opens one review by itself, once; re-showing and reconnecting decide nothing', async () => {
  const deciding = Object.fromEntries(DECIDING.map((type) => [type, () => { throw new Error(`${type} without a press`); }]));
  const page = mount({ ...deciding, open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await screen.findByText(en.domainField.free);
  // Observe first; open only because nothing is bound. The prefilled name is the read's own.
  expect(page.types()).toEqual(['inspect', 'observe_current_binding', 'open', 'read']);
  // A newer snapshot of the same account (a reconnect, a broadcast) opens nothing again.
  await broadcast({ ...signedIn, revision: signedIn.revision + 1 });
  await broadcast({ ...signedIn, generation: signedIn.generation + 1, revision: signedIn.revision + 2 });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
  expect(page.types().filter((type) => DECIDING.includes(type))).toEqual([]);
});

it('an accepted sign-in waiting to be saved comes first in the claim view, and nothing resumes without a press', async () => {
  const deciding = Object.fromEntries(DECIDING.map((type) => [type, () => { throw new Error(`${type} without a press`); }]));
  const page = mount({ ...deciding, 'recovery:inspect': { state: 'available', receipt: 'rec-1' } });
  await screen.findByRole('button', { name: en.common.continue });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(page.types()).toEqual(['inspect']);
});

it.each([
  ['owner_observed', () => screen.findByRole('heading', { name: copy.boundTitle })],
  ['bound_owner_unconfirmed', () => screen.findByText(copy.otherAccount)],
  ['legacy_review_required', () => screen.findByRole('link', { name: 'support@avibe.bot' })],
  ['legacy_recovery_required', () => screen.findByText(copy.repair)],
])('the claim view opens no review over what native observed: %s', async (answer, shown) => {
  const page = mount({ observe_current_binding: { state: answer } });
  await shown();
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(page.types().filter((type) => type === 'open')).toEqual([]);
});

it('the claim view opens a review only when native observed exactly no_published_binding', async () => {
  const page = mount({ observe_current_binding: { state: 'no_published_binding' }, open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
});

it.each(['legacy_wait', 'legacy_publication_pending'])('an older attempt (%s) is waited out: Check again, no review', async (answer) => {
  let observes = 0;
  const page = mount({ observe_current_binding: () => { observes += 1; return { state: observes === 1 ? answer : 'no_published_binding' }; },
    open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await screen.findByText(copy.problem.busy);
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(page.types()).not.toContain('open');
  fireEvent.click(screen.getByRole('button', { name: copy.checkAgain }));
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
});

it('the claim view sends its one review once: after a failure the way on is closing', async () => {
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: refuse('network_unavailable') });
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: copy.restart })).toBeNull();
  expect(screen.getAllByRole('button', { name: en.common.close }).length).toBe(2);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
});

it('a failed read of the binding is unknown, never unbound: Check again, no review', async () => {
  let observes = 0;
  const page = mount({ observe_current_binding: () => { observes += 1; if (observes === 1) throw { code: 'network_unavailable' }; return { state: 'no_published_binding' }; },
    open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await screen.findByText(copy.unknown);
  expect(page.types()).not.toContain('open');
  fireEvent.click(screen.getByRole('button', { name: copy.checkAgain }));
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
});

it('signed out, the claim dialog starts with the App sign-in, then opens the name once', async () => {
  const signedOut = states.signed_out as unknown as DesktopClientState;
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')) }, signedOut);
  const dialog = await screen.findByRole('dialog');
  expect(dialog.querySelector('[data-account-login]')).toBeTruthy();
  // The dialog's own heading over the same ways in; not the App's welcome.
  expect(within(dialog).getByRole('heading', { name: copy.signInTitle })).toBeTruthy();
  expect(within(dialog).queryByRole('img')).toBeNull();
  expect(within(dialog).queryByText(en.mobileApp.welcome.titleLine1)).toBeNull();
  expect(within(dialog).getByRole('button', { name: en.mobileApp.welcome.continueWithEmail })).toBeTruthy();
  expect(within(dialog).getByRole('button', { name: en.common.cancel })).toBeTruthy();
  expect(within(dialog).queryByRole('button', { name: en.desktopAccount.continueLocal })).toBeNull();
  expect(page.types().filter((type) => type !== 'refresh_sign_in_methods')).toEqual([]);
  await broadcast({ ...signedIn, revision: signedOut.revision + 1 });
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
});

it('closing the claim dialog cancels its review and returns to the Workbench', async () => {
  const page = mount({ open: { receipt: 'r-close' }, read: review(NEW('xiaoming')) });
  await screen.findByText(en.domainField.free);
  fireEvent.click(screen.getAllByRole('button', { name: en.common.cancel })[0]);
  await waitFor(() => expect(page.actions.find((each) => each.type === 'cancel')).toEqual({ type: 'cancel', receipt: 'r-close' }));
  await waitFor(() => expect(page.leaves()).toEqual(['cancel']));
});

it.each(['header', 'Escape'])('leaving a claim document from its update view (%s) still cancels the review', async (how) => {
  const page = mount({ open: { receipt: 'r-up' }, read: review({ state: 'local_runtime_upgrade_required' }) });
  fireEvent.click(await screen.findByRole('button', { name: en.update.action.check }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  if (how === 'header') fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.backWorkbench }));
  else fireEvent.keyDown(window, { key: 'Escape' });
  await waitFor(() => expect(page.actions.find((each) => each.type === 'cancel')).toEqual({ type: 'cancel', receipt: 'r-up' }));
  await waitFor(() => expect(page.leaves()).toEqual(['cancel']));
});

it('the Account page follows the address native projects after observing it bound', async () => {
  mount({ observe_current_binding: { state: 'owner_observed' } }, { ...signedIn, local: { ...signedIn.local, instance_id: 'mac-1', remote_access: 'off', remote_access_url: null } }, 'account');
  await waitFor(() => expect(document.querySelector('[data-computer-binding="bound"]')).toBeTruthy());
  expect(screen.queryByRole('link', { name: 'xiaoming-app.avibe.bot' })).toBeNull();
  // The connector starts: native publishes remote access on, with its URL.
  await broadcast({ ...signedIn, revision: signedIn.revision + 1,
    local: { ...signedIn.local, instance_id: 'mac-1', remote_access: 'on', remote_access_url: 'https://xiaoming-app.avibe.bot' } });
  expect((await screen.findByRole('link', { name: 'xiaoming-app.avibe.bot' })).getAttribute('href')).toBe('https://xiaoming-app.avibe.bot');
  // Turned off: the address stays, the link goes.
  await broadcast({ ...signedIn, revision: signedIn.revision + 2,
    local: { ...signedIn.local, instance_id: 'mac-1', remote_access: 'off', remote_access_url: 'https://xiaoming-app.avibe.bot' } });
  await waitFor(() => expect(screen.queryByRole('link', { name: 'xiaoming-app.avibe.bot' })).toBeNull());
  expect(screen.getByText('xiaoming-app.avibe.bot')).toBeTruthy();
});

it('a typed second factor native cannot take is corrected in place, never sent', () => {
  expect(factorCode('totp', ' 123 456 ')).toBe('123456');
  expect(factorCode('totp', '12345')).toBeNull();
  expect(factorCode('recovery', 'abcde-12345')).toBe('abcde-12345');
  // Several saved recovery codes pasted at once: longer than native's 32 UTF-16 units.
  expect(factorCode('recovery', 'abcde-12345 fghij-67890 klmno-13579')).toBeNull();
  expect(factorCode('recovery', '   ')).toBeNull();
});

it('the Account page only reads, and shows this computer only when bound', async () => {
  const page = mount({ observe_current_binding: { state: 'owner_observed' } }, signedIn, 'account');
  await waitFor(() => expect(document.querySelector('[data-computer-binding="bound"]')).toBeTruthy());
  expect(page.types()).toEqual(['observe_current_binding']);
  expect(screen.queryByRole('button', { name: copy.action })).toBeNull();
  cleanup();
  for (const state of ['no_published_binding', 'bound_owner_unconfirmed', 'legacy_review_required', 'legacy_recovery_required']) {
    const other = mount({ observe_current_binding: { state }, 'recovery:inspect': { state: 'available', receipt: 'rec' } }, signedIn, 'account');
    await waitFor(() => expect(other.types()).toEqual(['observe_current_binding']));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(document.querySelector('[data-computer-binding]')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(other.types()).toEqual(['observe_current_binding']);
    cleanup();
  }
});

it('binds the exact name the person saw, only when they press Bind', async () => {
  const page = mount({
    open: { receipt: 'receipt-1' },
    read: review(NEW(null)),
    check: (action: Action) => review(NEW(action.prefix as string), 'rev-2'),
    confirm: { state: 'bound' },
  });
  await opened();
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type !== 'observe_current_binding' && type !== 'inspect')).toEqual(['open', 'read', 'check']);
  fireEvent.click(screen.getByRole('button', { name: copy.confirm }));
  await screen.findByRole('heading', { name: copy.boundTitle });
  expect(page.actions.find((each) => each.type === 'confirm')).toEqual({ type: 'confirm', receipt: 'receipt-1', revision: 'rev-2' });
  expect(document.querySelector('[data-bind-dialog] [data-domain-field]')?.textContent).toBe('xiaoming-app.avibe.bot');
});

it('a name someone else took stays editable and gets no number added', async () => {
  mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: { state: 'name_taken', revision: 'rev-edit' } });
  await opened();
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByText(en.domainField.taken);
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
  await opened();
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
  await opened();
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  fireEvent.click(await screen.findByRole('button', { name: copy.sendCode }));
  fireEvent.change(await screen.findByLabelText(en.mobileApp.code.label), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(copy.changed.replace('{{fields}}', copy.field.availability));
  await screen.findByText(en.domainField.taken);
});

it('another account owning this computer is told, with nothing more called', async () => {
  const page = mount({ open: { receipt: 'r' }, read: review({ state: 'owner_conflict' }) });
  await opened();
  await screen.findByRole('heading', { name: copy.conflictTitle });
  expect(page.types().filter((type) => DECIDING.includes(type) || type === 'check')).toEqual([]);
});

it('an error is one sentence and one next step, never a code', async () => {
  mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: refuse('authentication_required') });
  await opened();
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByText(copy.problem.signedOut);
  expect(screen.queryByText(/authentication_required/)).toBeNull();
  expect(screen.getAllByRole('button', { name: en.common.close }).length).toBeGreaterThan(0);
});

it('an accepted sign-in waiting to be saved shows as Continue, resumes only on the press, then opens the name', async () => {
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-1' }, 'recovery:resume': { state: 'saved' },
    open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  const button = await screen.findByRole('button', { name: en.common.continue });
  expect(screen.getByText(copy.recovery)).toBeTruthy();
  expect(page.types()).not.toContain('resume');
  fireEvent.click(button);
  await waitFor(() => expect(page.actions.find((each) => each.type === 'resume')).toEqual({ type: 'resume', receipt: 'rec-1' }));
  await screen.findByText(en.domainField.free);
  expect(page.types().filter((type) => type === 'open')).toEqual(['open']);
  // Saved, it observed again before opening.
  expect(page.types().slice(page.types().indexOf('resume'))).toEqual(['resume', 'observe_current_binding', 'open', 'read']);
});

it('an accepted sign-in saved onto a computer already bound opens no review', async () => {
  let observes = 0;
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-2' }, 'recovery:resume': { state: 'saved' },
    observe_current_binding: () => { observes += 1; return { state: 'owner_observed' }; } });
  fireEvent.click(await screen.findByRole('button', { name: en.common.continue }));
  await screen.findByRole('heading', { name: copy.boundTitle });
  expect(observes).toBe(1);
  expect(page.types()).not.toContain('open');
});

/** Opens binding and verifies a code, up to the moment avibe.bot accepted it. */
async function verify() {
  await opened();
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

it('a recovery code native cannot take is marked wrong in place: nothing sent, the claim stays', async () => {
  const page = mount({ ...proofAnswers, review_after_proof: refuse('authentication_required') });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp'] } });
  fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.twoFactor.useRecovery }, { timeout: 5000 }));
  // Several saved codes pasted at once: over native's 32 UTF-16 units.
  fireEvent.change(await screen.findByLabelText(en.mobileApp.twoFactor.recoveryLabel), { target: { value: 'abcde-12345 fghij-67890 klmno-13579' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await screen.findByText(en.mobileApp.twoFactor.recoveryInvalid);
  expect(page.types()).not.toContain('submit_factor_code');
  expect(page.types()).not.toContain('cancel');
  // Still the same claim: a code native takes goes out with its submission.
  fireEvent.change(screen.getByLabelText(en.mobileApp.twoFactor.recoveryLabel), { target: { value: 'abcde-12345' } });
  fireEvent.click(screen.getByRole('button', { name: en.mobileApp.twoFactor.verify }));
  await waitFor(() => expect(page.actions.find((each) => each.type === 'submit_factor_code'))
    .toEqual({ type: 'submit_factor_code', receipt: 'r', submission: 's', method: 'recovery', code: 'abcde-12345' }));
});

it.each<[string, keyof typeof en.domainField]>([
  ['a', 'short'], ['ab--cd', 'doubleHyphen'], [`${'a'.repeat(47)}`, 'long'], ['-ab', 'hyphenEnds'], ['ab-', 'hyphenEnds'], ['max well', 'invalid'],
])('keeps %s in the field, says why, and never checks it', async (typed, reason) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await opened();
  const field = await screen.findByRole('textbox');
  const checks = page.types().filter((type) => type === 'check').length;
  fireEvent.change(field, { target: { value: typed } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(page.types().filter((type) => type === 'check')).toHaveLength(checks);
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe(typed);
  expect(screen.getByText(en.domainField[reason])).toBeTruthy();
  expect((screen.getByRole('button', { name: copy.confirm }) as HTMLButtonElement).disabled).toBe(true);
});

it('an emptied name says nothing, is never checked, and cannot be bound', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const page = mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')) });
  await opened();
  await screen.findByText(en.domainField.free);
  const checks = page.types().filter((type) => type === 'check').length;
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(page.types().filter((type) => type === 'check')).toHaveLength(checks);
  expect(document.querySelector('[data-domain-field] [role="status"]')?.textContent).toBe('');
  expect((screen.getByRole('button', { name: copy.confirm }) as HTMLButtonElement).disabled).toBe(true);
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

it('an accepted sign-in stays reachable while its account still owes a second factor', async () => {
  const owing = { ...signedIn, account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['totp'] } } as DesktopClientState;
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-3' } }, owing);
  await screen.findByRole('button', { name: en.common.continue });
  expect(page.types()).toContain('inspect');
  expect(page.types()).not.toContain('observe_current_binding');
});

it('an accepted sign-in stays reachable when the binding cannot be observed', async () => {
  mount({ observe_current_binding: refuse('claim_in_progress'), 'recovery:inspect': { state: 'available', receipt: 'rec-4' } });
  await screen.findByRole('button', { name: en.common.continue });
});

it('recovery codes from a binding hold the document until they are saved', async () => {
  const page = mount({ ...proofAnswers,
    review_after_proof: (action: Action) => (action.submission === 's' ? refuse('authentication_required')() : { state: 'bound' }),
    prepare_totp_setup: { state: 'totp_setup', setup: 'setup-1', manual_key: 'KEY-FIXTURE', qr_payload: 'otpauth://totp/fixture' },
    finish_totp_setup: { state: 'enrollment_accepted', submission: 's2', recovery: { state: 'issued', codes: ['•••••-••001', '•••••-••002'] } } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'enroll', methods: [] } });
  fireEvent.click(await screen.findByRole('button', { name: en.desktopAccount.addAuthenticator }, { timeout: 5000 }));
  fireEvent.change(await screen.findByLabelText(en.desktopAccount.authenticatorCode), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.finishFactor }));
  await screen.findByText('•••••-••001');
  expect(screen.queryByRole('button', { name: en.desktopAccount.backWorkbench })).toBeNull();
  fireEvent.keyDown(window, { key: 'Escape' });
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  expect(screen.getByText('•••••-••001')).toBeTruthy();
  expect(page.actions.some((each) => each.type === 'cancel')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.savedCodes }));
  await screen.findByRole('heading', { name: copy.boundTitle }, { timeout: 5000 });
});

it('an authenticator setup finishing holds the document until its recovery codes are drawn', async () => {
  let answer: (value: unknown) => void = () => {};
  const page = mount({ ...proofAnswers,
    review_after_proof: (action: Action) => (action.submission === 's' ? refuse('authentication_required')() : { state: 'bound' }),
    prepare_totp_setup: { state: 'totp_setup', setup: 'setup-1', manual_key: 'KEY-FIXTURE', qr_payload: 'otpauth://totp/fixture' },
    finish_totp_setup: () => new Promise((resolve) => { answer = resolve; }) });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'enroll', methods: [] } });
  fireEvent.click(await screen.findByRole('button', { name: en.desktopAccount.addAuthenticator }, { timeout: 5000 }));
  fireEvent.change(await screen.findByLabelText(en.desktopAccount.authenticatorCode), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.finishFactor }));
  await waitFor(() => expect(page.types()).toContain('finish_totp_setup'));
  // On its way: no way out, by key, by the dialog or by the header.
  await waitFor(() => expect(screen.queryByRole('button', { name: en.desktopAccount.backWorkbench })).toBeNull());
  fireEvent.keyDown(window, { key: 'Escape' });
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  // The corner close is hidden by style; even pressed, it closes nothing.
  for (const close of screen.queryAllByRole('button', { name: en.common.close })) fireEvent.click(close);
  expect(screen.getByLabelText(en.desktopAccount.authenticatorCode)).toBeTruthy();
  expect(page.actions.some((each) => each.type === 'cancel')).toBe(false);
  expect(page.leaves()).toEqual([]);
  await act(async () => { answer({ state: 'enrollment_accepted', submission: 's2', recovery: { state: 'issued', codes: ['•••••-••003'] } }); });
  await screen.findByText('•••••-••003');
  expect(page.leaves()).toEqual([]);
});

it('an authenticator code that is not six digits stays in place and is never sent', async () => {
  const page = mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    prepare_totp_setup: { state: 'totp_setup', setup: 'setup-1', manual_key: 'KEY-FIXTURE', qr_payload: 'otpauth://totp/fixture' } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'enroll', methods: [] } });
  fireEvent.click(await screen.findByRole('button', { name: en.desktopAccount.addAuthenticator }, { timeout: 5000 }));
  fireEvent.change(await screen.findByLabelText(en.desktopAccount.authenticatorCode), { target: { value: '12 34' } });
  fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.finishFactor }));
  await screen.findByText(copy.problem.codeInvalid);
  expect(screen.getByText('KEY-FIXTURE')).toBeTruthy();
  expect(page.types()).not.toContain('finish_totp_setup');
});

it('a binding not under this account offers signing in with that account, or checking again', async () => {
  mount({ observe_current_binding: { state: 'bound_owner_unconfirmed' } });
  await screen.findByRole('button', { name: copy.signInOther });
  expect(screen.getByRole('button', { name: copy.checkAgain })).toBeTruthy();
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

it('an unverifiable passkey challenge is final: the claim view closes rather than opening again', async () => {
  mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    submit_factor_passkey: refuse('invalid_passkey_challenge') });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'challenge', methods: ['passkey'] } });
  fireEvent.click(await screen.findByRole('button', { name: en.mobileApp.twoFactor.usePasskey }, { timeout: 5000 }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: copy.restart })).toBeNull();
  expect(screen.getAllByRole('button', { name: en.common.close }).length).toBe(2);
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

it('a new generation while an action is on its way leaves the dialog usable', async () => {
  let answer: (value: unknown) => void = () => {};
  mount({ open: () => new Promise((resolve) => { answer = resolve; }) });
  await opened();
  await screen.findByRole('heading', { name: copy.title });
  await broadcast({ ...signedIn, generation: signedIn.generation + 1, revision: signedIn.revision + 1 });
  await act(async () => { answer({ receipt: 'late' }); });
  // The old review's reply is dropped; its one review was sent, so the way on is closing.
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: copy.restart })).toBeNull();
  expect(screen.getAllByRole('button', { name: en.common.close }).every((each) => !(each as HTMLButtonElement).disabled)).toBe(true);
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
  await opened();
  const field = await screen.findByRole('textbox');
  fireEvent.change(field, { target: { value: 'maxa' } });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 450)); });
  fireEvent.change(field, { target: { value: 'maxab' } });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 450)); });
  fireEvent.change(field, { target: { value: 'maxabc' } });
  await waitFor(() => expect(screen.getByText(en.domainField.free)).toBeTruthy(), { timeout: 5000 });
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('maxabc');
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

it('a computer avibe.bot no longer accepts is a dead end that names the support address as a mail link', async () => {
  const page = mount({ open: { receipt: 'r' }, read: refuse('binding_unavailable') });
  await opened();
  const link = await screen.findByRole('link', { name: 'support@avibe.bot' });
  expect(link.getAttribute('href')).toBe('mailto:support@avibe.bot');
  expect(screen.queryByRole('button', { name: copy.restart })).toBeNull();
  expect(page.types().filter((type) => type === 'read')).toHaveLength(1);
});

it('offers held names other than the one already typed', async () => {
  const names = { holding: { limit: 10, held_count: 3, active_count: 1, at_limit: false, reason: null, earliest_release_at: null },
    eligible: [{ prefix: 'maxlab', hostname: 'maxlab-app.avibe.bot', release_at: null }, { prefix: 'maxhome', hostname: 'maxhome-app.avibe.bot', release_at: null }] };
  mount({ open: { receipt: 'r' }, read: review(NEW('maxlab', { prefilled_prefix: 'maxlab', names })) });
  await opened();
  await screen.findByText(en.domainField.reclaim);
  expect(screen.queryByRole('button', { name: 'maxlab' })).toBeNull();
  expect(screen.getByRole('button', { name: 'maxhome' })).toBeTruthy();
});

it.each(['local_instance_conflict', 'pairing_recovery_required'])('an older attempt still holding this computer (%s) says to wait, not to start again', async (code) => {
  mount({ open: { receipt: 'r' }, read: refuse(code) });
  await opened();
  await screen.findByText(copy.problem.busy);
  expect(screen.queryByRole('button', { name: copy.restart })).toBeNull();
});

it('a sign-in that no longer matches the account asks to sign in again', async () => {
  mount({ open: { receipt: 'r' }, read: review(NEW('xiaoming')), confirm: refuse('subject_mismatch') });
  await opened();
  fireEvent.click(await screen.findByRole('button', { name: copy.confirm }));
  await screen.findByText(copy.problem.signedOut);
});

it('an older record that ended without a result is a dead end on the card: support, no Bind', async () => {
  const page = mount({ observe_current_binding: { state: 'legacy_review_required' } });
  const link = await screen.findByRole('link', { name: 'support@avibe.bot' });
  expect(link.getAttribute('href')).toBe('mailto:support@avibe.bot');
  expect(screen.queryByRole('button', { name: copy.action })).toBeNull();
  expect(page.types().filter((type) => type === 'open')).toEqual([]);
});

it('a 429 while adding an authenticator keeps its key and QR, locked until the wait ends', async () => {
  let finishes = 0;
  mount({ ...proofAnswers,
    review_after_proof: refuse('authentication_required'),
    prepare_totp_setup: { state: 'totp_setup', setup: 'setup-1', manual_key: 'KEY-FIXTURE', qr_payload: 'otpauth://totp/fixture' },
    finish_totp_setup: () => { finishes += 1; throw { code: 'rate_limited', retry_after_seconds: 1 }; } });
  await verify();
  await broadcast({ ...signedIn, revision: signedIn.revision + 3,
    account: { ...signedIn.account, status: 'second_factor_required', gate: 'enroll', methods: [] } });
  fireEvent.click(await screen.findByRole('button', { name: en.desktopAccount.addAuthenticator }, { timeout: 5000 }));
  fireEvent.change(await screen.findByLabelText(en.desktopAccount.authenticatorCode), { target: { value: '123456' } });
  fireEvent.click(screen.getByRole('button', { name: en.desktopAccount.finishFactor }));
  await screen.findByText(/Try again in 1 minute/);
  expect(screen.getByText('KEY-FIXTURE')).toBeTruthy();
  expect((screen.getByLabelText(en.desktopAccount.authenticatorCode) as HTMLInputElement).disabled).toBe(true);
  await waitFor(() => expect((screen.getByLabelText(en.desktopAccount.authenticatorCode) as HTMLInputElement).disabled).toBe(false), { timeout: 3000 });
  expect(finishes).toBe(1);
});

it('an accepted sign-in shown as Continue and cancelled is offered again in the next claim view', async () => {
  const page = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-5' } });
  await screen.findByRole('button', { name: en.common.continue });
  fireEvent.click(screen.getAllByRole('button', { name: en.common.cancel })[0]);
  await waitFor(() => expect(page.leaves()).toEqual(['cancel']));
  cleanup();
  const again = mount({ 'recovery:inspect': { state: 'available', receipt: 'rec-5' } });
  await screen.findByRole('button', { name: en.common.continue });
  expect([...page.types(), ...again.types()]).not.toContain('open');
  expect([...page.types(), ...again.types()]).not.toContain('resume');
});
