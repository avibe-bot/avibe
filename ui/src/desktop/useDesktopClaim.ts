import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { hasDesktopClient, type DesktopClientState } from './bridge';
import type { AccountRecovery, BindField, BindProblem, BindStep, ComputerBinding, EligibleName } from './BindComputerView';

/**
 * Binding this computer from the Account page (AV-8). Native owns every decision; this hook only
 * carries its opaque handles between the person's presses and draws what native answered.
 *
 * What runs without a press: reading this computer's binding (`observe_current_binding`) and asking
 * whether an accepted verification is waiting to be saved (`inspect`, offline), each once per
 * showing of the Account page. Everything that can reach avibe.bot with a decision (confirm, send a
 * code, submit a code or factor, resume a sign-in) runs only inside the handler of the button for
 * it; mounting, re-showing or reconnecting never calls them.
 */

type Reply = { ok: true; value: Record<string, unknown> | null } | { ok: false; code: string; retryAfter?: number };
type Command = 'desktop_claim_review' | 'desktop_account_recovery';

async function invoke(command: Command, generation: number, action: Record<string, unknown>): Promise<Reply> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke: (command: string, args: { request: unknown }) => Promise<unknown> };
  }).__TAURI_INTERNALS__;
  if (!hasDesktopClient() || !internals?.invoke) return { ok: false, code: 'bridge_unavailable' };
  try {
    const value = await internals.invoke(command, { request: { expected_generation: generation, action } });
    return { ok: true, value: value && typeof value === 'object' ? value as Record<string, unknown> : null };
  } catch (error) {
    const code = (error as { code?: unknown } | null)?.code;
    const retryAfter = (error as { retry_after_seconds?: unknown } | null)?.retry_after_seconds;
    return { ok: false, code: typeof code === 'string' ? code : 'request_failed', ...(typeof retryAfter === 'number' ? { retryAfter } : {}) };
  }
}

const str = (value: unknown): string | null => typeof value === 'string' ? value : null;
const obj = (value: unknown): Record<string, unknown> | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;

/** Native's grammar for a new name (machine/hosted/projection.rs `new_prefix`): 2 to 46 lowercase
 *  letters, digits and hyphens, no hyphen first or last, and not `--` in third and fourth place. A
 *  name that fails it stays in the field and is never sent: a refused Check ends the review. */
export const validPrefix = (name: string) => /^[a-z0-9][a-z0-9-]{0,44}[a-z0-9]$/.test(name) && name.slice(2, 4) !== '--';
const hostFor = (prefix: string) => `${prefix}-app.avibe.bot`;

const PROBLEMS: Record<string, BindProblem> = {
  network_unavailable: 'unreachable', control_plane_unreachable: 'unreachable', request_failed: 'unreachable',
  provider_unavailable: 'unreachable', provider_timeout: 'unreachable', invalid_response: 'unreachable',
  response_too_large: 'unreachable', code_delivery_unknown: 'unreachable', factor_delivery_unknown: 'unreachable',
  bridge_unavailable: 'unreachable',
  authentication_required: 'signedOut', account_changed: 'signedOut', signed_out: 'signedOut',
  rate_limited: 'rateLimited',
  expired: 'expired', decision_changed: 'expired', stale_generation: 'expired', invalid_or_expired: 'expired',
  claim_in_progress: 'busy', busy: 'busy', operation_in_progress: 'busy',
  local_runtime_unavailable: 'notResponding', local_admission_failed: 'notResponding', invalid_local_descriptor: 'notResponding',
  fresh_claim_required: 'lost',
  reauthentication_failed: 'reauth',
};
const problemFor = (code: string): BindStep => code === 'local_runtime_upgrade_required'
  ? { kind: 'upgrade' } : { kind: 'problem', problem: PROBLEMS[code] ?? 'unknown' };
/** avibe.bot said this exact code is wrong or expired: the person may type it again, in place. */
const WRONG_CODE = new Set(['invalid_or_expired', 'invalid_code', 'invalid_otp', 'verification_invalid']);
/** An authenticator being added keeps its setup only for a wrong code, not an expired one. */
const WRONG_SETUP_CODE = new Set(['invalid_code', 'invalid_otp', 'verification_invalid']);
const SAVE_TRIES = 20;
const SAVE_WAIT_MS = 1500;
/** A passkey that did not finish (cancelled, unavailable, no valid assertion, or one avibe.bot
 *  refused): native keeps the submission, so the person may try again or use a code. An
 *  unverifiable challenge or reply is final and starts again. */
const PASSKEY_FAILED = new Set(['passkey_cancelled', 'passkey_unavailable', 'invalid_passkey_assertion']);

const OBSERVED: Record<string, ComputerBinding['state']> = {
  no_published_binding: 'unbound', legacy_review_required: 'unbound', owner_observed: 'bound',
  bound_owner_unconfirmed: 'unconfirmed', legacy_wait: 'pending', legacy_publication_pending: 'pending',
  legacy_recovery_required: 'repair',
};

/** What a review showed the person, to say what changed when native shows another one. */
type Shown = { account: string; computer: string; host: string | null; available: boolean };

type Handles = {
  receipt: string | null;
  revision: string | null;
  offer: string | null;
  delivery: string | null;
  submission: string | null;
  setup: string | null;
  /** The review the person last confirmed, for a review native returns after verification. */
  confirmed: Shown | null;
  shown: Shown | null;
  /** The name being typed, and whether a check for it is on its way. */
  checking: boolean;
};
const NO_HANDLES: Handles = { receipt: null, revision: null, offer: null, delivery: null, submission: null, setup: null, confirmed: null, shown: null, checking: false };

export function useDesktopClaim(state: DesktopClientState | null, enabled: boolean) {
  const generation = state?.generation ?? 0;
  const latest = useRef(state);
  useLayoutEffect(() => { latest.current = state; });
  const handles = useRef<Handles>({ ...NO_HANDLES });
  // `published`: native said this submission's sign-in is published and owes a second factor, so
  // the account's gate, once it shows that, is this sign-in's and not an earlier snapshot's.
  const saving = useRef<{ tries: number; running: boolean; recover: string | null; timer: number | null; published: boolean } | null>(null);
  const [binding, setBinding] = useState<ComputerBinding>({ state: 'reading' });
  const [recovery, setRecovery] = useState<{ state: AccountRecovery; receipt: string | null } | null>(null);
  // The flow belongs to the generation it started in: a changed account or document generation
  // ends it (native has fenced it already), without a state update from an effect.
  const [flow, setFlow] = useState<{ generation: number; step: BindStep | null }>({ generation, step: null });
  const step = flow.generation === generation ? flow.step : null;
  const setStep = useCallback((next: BindStep | null | ((current: BindStep | null) => BindStep | null)) => {
    setFlow((current) => {
      const at = latest.current?.generation ?? 0;
      const previous = current.generation === at ? current.step : null;
      return { generation: at, step: typeof next === 'function' ? next(previous) : next };
    });
  }, []);
  const [review, setReview] = useState<{ account: string; computer: string }>({ account: '', computer: '' });
  const [busy, setBusy] = useState(false);
  const nameRef = useRef('');
  // A reply for a document, account or flow that is gone is dropped, never drawn.
  const epoch = useRef(0);

  const call = useCallback(async (command: Command, action: Record<string, unknown>) => {
    const mine = epoch.current;
    const reply = await invoke(command, latest.current?.generation ?? 0, action);
    return mine === epoch.current ? reply : null;
  }, []);

  const reset = useCallback(() => {
    epoch.current += 1;
    if (saving.current?.timer) window.clearTimeout(saving.current.timer);
    saving.current = null;
    handles.current = { ...NO_HANDLES };
    nameRef.current = '';
    setStep(null);
    setBusy(false);
  }, [setStep]);

  const observe = useCallback(async () => {
    setBinding({ state: 'reading' });
    const reply = await call('desktop_claim_review', { type: 'observe_current_binding' });
    if (!reply) return;
    if (!reply.ok) { setBinding(reply.code === 'local_runtime_upgrade_required' ? { state: 'upgrade' } : { state: 'unknown' }); return; }
    const answer = str(reply.value?.state) ?? '';
    const kind = OBSERVED[answer];
    const local = latest.current?.local;
    const listed = latest.current?.catalog.instances.find((each) => each.id === local?.instance_id);
    const url = local?.remote_access_url ?? null;
    const host = listed?.host ?? (url ? new URL(url).host : null);
    if (kind === 'bound') setBinding({ state: 'bound', host, url: local?.remote_access === 'on' ? url : null });
    else if (kind === 'unconfirmed') setBinding({ state: 'unconfirmed', host });
    else if (kind === 'unbound') setBinding({ state: 'unbound', previous: answer === 'legacy_review_required' });
    else if (kind) setBinding({ state: kind } as ComputerBinding);
    else setBinding({ state: 'unknown' });
  }, [call]);

  const inspect = useCallback(async () => {
    const reply = await call('desktop_account_recovery', { type: 'inspect' });
    if (!reply) return;
    const receipt = reply.ok ? str(reply.value?.receipt) : null;
    setRecovery(reply.ok && reply.value?.state === 'available' && receipt ? { state: 'available', receipt } : null);
  }, [call]);

  // The two reads, once per showing of the Account page and per account generation.
  const showing = enabled && state?.account.status === 'signed_in';
  // Closing the flow reads again itself (close()); a flow opening or ending is not a new showing.
  const flowOpen = useRef(false);
  useEffect(() => { flowOpen.current = step !== null; }, [step]);
  useEffect(() => {
    if (!showing || flowOpen.current) return;
    void observe();
    void inspect();
  }, [showing, generation, observe, inspect]);

  // A changed generation also drops the old flow's handles and any reply still on its way.
  const seen = useRef(generation);
  useEffect(() => {
    if (seen.current === generation) return;
    seen.current = generation;
    epoch.current += 1;
    handles.current = { ...NO_HANDLES };
  }, [generation]);

  // Leaving the page ends the review it held. Cancelling only attenuates this receipt.
  useEffect(() => () => {
    const receipt = handles.current.receipt;
    epoch.current += 1;
    if (receipt) void invoke('desktop_claim_review', latest.current?.generation ?? 0, { type: 'cancel', receipt });
  }, []);

  /** Draws one review native delivered; `changed` names what differs from the confirmed one. A
   *  review for a name other than the field's now is checked again for what the field says. */
  const showReview = useCallback(async (delivered: Record<string, unknown>, first: 'read' | 'check' | 'proof'): Promise<void> => {
    const h = handles.current;
    let value = delivered;
    let after = first;
    for (;;) {
      const revision = str(value.revision);
      const screen = obj(value.screen);
      if (!revision || !screen) { setStep({ kind: 'problem', problem: 'unknown' }); return; }
      h.revision = revision;
      const account = str(value.account_email) ?? '';
      const computer = str(value.device_name) ?? '';
      setReview({ account, computer });
      const kind = str(screen.state);
      const names = obj(screen.names);
      const eligible: EligibleName[] = Array.isArray(names?.eligible)
        ? names.eligible.flatMap((each) => { const e = obj(each); const prefix = str(e?.prefix); const host = str(e?.hostname); return prefix && host ? [{ prefix, host }] : []; })
        : [];
      let next: BindStep;
      let shown: Shown = { account, computer, host: null, available: false };
      if (kind === 'new') {
        const selected = str(screen.prefix);
        const prefilled = str(screen.prefilled_prefix);
        // The first read carries avibe.bot's suggestion; check it once so the review names it exactly.
        const ask = after === 'read' && !selected && prefilled && validPrefix(prefilled) ? prefilled
          // The person kept typing while this check was on its way: check what the field says now.
          : after !== 'read' && selected !== null && selected !== nameRef.current && validPrefix(nameRef.current) ? nameRef.current
            : null;
        if (ask) {
          nameRef.current = ask;
          setStep({ kind: 'name', name: ask, check: 'checking' });
          const reply = await call('desktop_claim_review', { type: 'check', revision, prefix: ask });
          if (!reply) return;
          if (!reply.ok) { setStep(problemFor(reply.code)); return; }
          value = reply.value ?? {};
          after = 'check';
          continue;
        }
        const name = after === 'read' ? (selected ?? prefilled ?? '') : nameRef.current;
        nameRef.current = name;
        if (selected !== null && selected !== name) { setStep({ kind: 'name', name, check: 'invalid' }); return; }
        const check = screen.taken === true ? 'taken' : !selected ? 'invalid' : eligible.some((each) => each.prefix === selected) ? 'reclaim' : 'free';
        shown = { account, computer, host: selected ? hostFor(selected) : null, available: check === 'free' || check === 'reclaim' };
        next = { kind: 'name', name, check };
      } else if (kind === 'existing') {
        const host = str(screen.hostname) ?? '';
        shown = { account, computer, host, available: true };
        next = { kind: 'existing', host };
      } else if (kind === 'name_holding_limit_reached') {
        const holding = obj(names?.holding);
        next = {
          kind: 'limit', name: nameRef.current,
          held: typeof holding?.limit === 'number' ? holding.limit : null,
          earliest: str(holding?.earliest_release_at), eligible,
        };
      } else if (kind === 'owner_conflict') {
        next = { kind: 'conflict' };
      } else if (kind === 'local_runtime_upgrade_required') {
        next = { kind: 'upgrade' };
      } else {
        next = { kind: 'problem', problem: 'unknown' };
      }
      h.shown = shown;
      if (after === 'proof' && h.confirmed && (next.kind === 'name' || next.kind === 'existing')) {
        const before = h.confirmed;
        const changed: BindField[] = [
          ...(before.account !== shown.account ? ['account' as const] : []),
          ...(before.computer !== shown.computer ? ['computer' as const] : []),
          ...(before.host !== shown.host ? ['name' as const] : []),
          ...(before.available !== shown.available ? ['availability' as const] : []),
        ];
        next = { ...next, changed: changed.length ? changed : ['availability'] };
      }
      setStep(next);
      return;
    }
  }, [call, setStep]);

  /** What native decided for a confirmed review, or for one re-read after verification. */
  const decided = useCallback(async (value: Record<string, unknown> | null): Promise<void> => {
    const h = handles.current;
    const answer = str(value?.state);
    if (value && obj(value.screen)) return showReview(value, 'proof');
    if (answer === 'bound') { h.receipt = null; setStep({ kind: 'bound', host: h.shown?.host ?? '' }); return; }
    if (answer === 'declined') { h.receipt = null; setStep({ kind: 'problem', problem: 'changed' }); return; }
    if (answer === 'confirmation_required' && str(value?.offer)) { h.offer = str(value?.offer); setStep({ kind: 'proof' }); return; }
    if (answer === 'name_taken' && str(value?.revision)) {
      h.revision = str(value?.revision);
      setStep({ kind: 'name', name: nameRef.current, check: 'taken' });
      return;
    }
    setStep({ kind: 'problem', problem: 'unknown' });
  }, [showReview, setStep]);

  /** Runs one pressed action; nothing else runs while it is on its way. */
  const run = useCallback(async (work: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    const mine = epoch.current;
    try { await work(); } finally { if (mine === epoch.current) setBusy(false); }
  }, [busy]);

  const start = useCallback(() => run(async () => {
    handles.current = { ...NO_HANDLES };
    nameRef.current = '';
    setStep({ kind: 'opening' });
    const opened = await call('desktop_claim_review', { type: 'open' });
    if (!opened) return;
    const receipt = opened.ok ? str(opened.value?.receipt) : null;
    if (!opened.ok || !receipt) { setStep(problemFor(opened.ok ? 'invalid_response' : opened.code)); return; }
    handles.current.receipt = receipt;
    const read = await call('desktop_claim_review', { type: 'read', receipt });
    if (!read) return;
    if (!read.ok) { setStep(problemFor(read.code)); return; }
    await showReview(read.value ?? {}, 'read');
  }), [run, call, showReview, setStep]);

  const timer = useRef<number | null>(null);
  const changeName = useCallback((typed: string) => {
    const name = typed.trim().toLowerCase();
    nameRef.current = name;
    if (timer.current !== null) window.clearTimeout(timer.current);
    if (!validPrefix(name)) { setStep({ kind: 'name', name, check: 'invalid' }); return; }
    setStep({ kind: 'name', name, check: 'checking' });
    // Ask avibe.bot once typing pauses; a reply for an older name checks the current one again.
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const h = handles.current;
      if (h.checking || !h.revision) return;
      h.checking = true;
      void call('desktop_claim_review', { type: 'check', revision: h.revision, prefix: nameRef.current }).then(async (reply) => {
        h.checking = false;
        if (!reply) return;
        if (!reply.ok) { setStep(problemFor(reply.code)); return; }
        await showReview(reply.value ?? {}, 'check');
      });
    }, 400);
  }, [call, showReview, setStep]);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);

  const pick = useCallback((prefix: string) => run(async () => {
    const h = handles.current;
    if (!h.revision) return;
    nameRef.current = prefix;
    const reply = await call('desktop_claim_review', { type: 'check', revision: h.revision, prefix });
    if (!reply) return;
    if (!reply.ok) { setStep(problemFor(reply.code)); return; }
    await showReview(reply.value ?? {}, 'check');
  }), [run, call, showReview, setStep]);

  const confirm = useCallback(() => run(async () => {
    const h = handles.current;
    if (!h.receipt || !h.revision) return;
    h.confirmed = h.shown;
    const reply = await call('desktop_claim_review', { type: 'confirm', receipt: h.receipt, revision: h.revision });
    if (!reply) return;
    if (!reply.ok) { setStep(problemFor(reply.code)); return; }
    await decided(reply.value);
  }), [run, call, decided, setStep]);

  const sendCode = useCallback(() => run(async () => {
    const h = handles.current;
    if (!h.receipt || !h.offer) return;
    const reply = await call('desktop_claim_review', { type: 'send_proof_code', receipt: h.receipt, offer: h.offer });
    if (!reply) return;
    h.offer = null;
    const delivery = reply.ok ? str(reply.value?.delivery) : null;
    if (!reply.ok || !delivery) { setStep(problemFor(reply.ok ? 'invalid_response' : reply.code)); return; }
    h.delivery = delivery;
    setStep({ kind: 'code', code: '' });
  }), [run, call, setStep]);

  /** After an accepted verification: a second factor the account still owes, or the same ticket again. */
  const owedFactor = useCallback((): boolean => {
    const account = latest.current?.account;
    if (account?.status !== 'second_factor_required') return false;
    if (account.gate === 'enroll') { setStep({ kind: 'factorSetup', material: null, code: '' }); return true; }
    const methods = account.methods.length ? account.methods : ['totp' as const];
    setStep({ kind: 'factor', method: methods[0] ?? 'totp', methods, code: '' });
    return true;
  }, [setStep]);
  // After an accepted verification, native reads the new sign-in back and saves it on its own;
  // `verification_accepted` arrives before that is done. `review_after_proof` answers
  // `{state:"publishing"}` while it is, without spending the submission or reaching avibe.bot: only
  // that answer is asked again, one request at a time, on each native broadcast and on a short
  // timer. Any other refusal is final. If publishing does not end, the account's own recovery
  // continues it on a press, with no new code. This runs only after the person's own verification
  // in this document; a new document starts with no flow.
  const [saveTick, setSaveTick] = useState(0);
  const stopSaving = useCallback(() => {
    if (saving.current?.timer) window.clearTimeout(saving.current.timer);
    saving.current = null;
  }, []);
  const afterVerification = useCallback((submission: string) => {
    handles.current.submission = submission;
    stopSaving();
    saving.current = { tries: 0, running: false, recover: null, timer: null, published: false };
    setStep({ kind: 'saving' });
    setSaveTick((tick) => tick + 1);
  }, [setStep, stopSaving]);
  const settle = useCallback(async () => {
    const h = handles.current;
    const current = saving.current;
    if (!current || current.running || current.recover || !h.receipt || !h.submission) return;
    const retry = () => {
      if (++current.tries < SAVE_TRIES) {
        current.timer = window.setTimeout(() => setSaveTick((tick) => tick + 1), SAVE_WAIT_MS);
        return true;
      }
      return false;
    };
    // The account state never decides on its own: an earlier snapshot can still owe a factor this
    // submission has just answered. Only after native said this one is published and owes a factor
    // does the account's gate choose the step, once its broadcast shows it.
    if (current.published) {
      if (owedFactor()) { stopSaving(); return; }
      if (!retry()) { stopSaving(); setStep(problemFor('authentication_required')); }
      return;
    }
    current.running = true;
    const reply = await call('desktop_claim_review', { type: 'review_after_proof', receipt: h.receipt, submission: h.submission });
    current.running = false;
    if (!reply || saving.current !== current) return;
    const publishing = reply.ok && reply.value?.state === 'publishing';
    if (reply.ok && !publishing) { stopSaving(); await decided(reply.value); return; }
    if (!reply.ok) {
      // Published, and the new sign-in owes a second factor: the account's gate says which.
      if (reply.code === 'authentication_required') {
        current.published = true;
        if (owedFactor()) { stopSaving(); return; }
        if (retry()) return;
      }
      stopSaving();
      setStep(problemFor(reply.code));
      return;
    }
    if (retry()) return;
    const inspected = await call('desktop_account_recovery', { type: 'inspect' });
    if (saving.current !== current) return;
    const receipt = inspected?.ok && inspected.value?.state === 'available' ? str(inspected.value.receipt) : null;
    if (receipt) { current.recover = receipt; setStep({ kind: 'saving', recover: true }); return; }
    stopSaving();
    setStep({ kind: 'problem', problem: 'expired' });
  }, [call, decided, owedFactor, setStep, stopSaving]);
  const savingNow = step?.kind === 'saving' && !step.recover;
  const revision = state?.revision;
  useEffect(() => {
    if (savingNow) void settle();
  }, [savingNow, saveTick, revision, settle]);
  useEffect(() => () => stopSaving(), [stopSaving]);
  /** The person continues saving the accepted sign-in; then the same ticket is read again. */
  const recoverSaving = useCallback(() => run(async () => {
    const current = saving.current;
    if (!current?.recover) return;
    const reply = await call('desktop_account_recovery', { type: 'resume', receipt: current.recover });
    if (!reply || saving.current !== current) return;
    const answer = reply.ok ? str(reply.value?.state) : null;
    const renewed = reply.ok && answer === 'available' ? str(reply.value?.receipt) : null;
    if (answer === 'saved') {
      current.recover = null;
      current.tries = 0;
      setStep({ kind: 'saving' });
      setSaveTick((tick) => tick + 1);
    } else if (renewed) {
      // The sign-in was read back; saving it is a separate press.
      current.recover = renewed;
    } else {
      stopSaving();
      setStep({ kind: 'problem', problem: answer === 'storage_blocked' ? 'storage' : 'expired' });
    }
  }), [run, call, setStep, stopSaving]);

  const submissionFrom = (reply: Reply | null): string | null => reply?.ok ? str(reply.value?.submission) : null;

  const submitCode = useCallback((code: string) => run(async () => {
    const h = handles.current;
    if (!h.receipt) return;
    if (step?.kind === 'code' && h.delivery) {
      const reply = await call('desktop_claim_review', { type: 'submit_proof_code', receipt: h.receipt, delivery: h.delivery, code });
      if (!reply) return;
      if (!reply.ok) { setStep(WRONG_CODE.has(reply.code) ? { kind: 'code', code: '', invalid: true } : problemFor(reply.code)); return; }
      const submission = submissionFrom(reply);
      if (!submission) { setStep(problemFor('invalid_response')); return; }
      h.delivery = null;
      afterVerification(submission);
    } else if (step?.kind === 'factor' && step.method !== 'passkey' && h.submission) {
      const reply = await call('desktop_claim_review', { type: 'submit_factor_code', receipt: h.receipt, submission: h.submission, method: step.method, code });
      if (!reply) return;
      if (!reply.ok) {
        setStep(WRONG_CODE.has(reply.code) ? { ...step, code: '', error: 'code' }
          : reply.code === 'rate_limited' ? { ...step, code: '', error: 'rateLimited', retryMinutes: Math.max(1, Math.ceil((reply.retryAfter ?? 60) / 60)) }
            : problemFor(reply.code));
        return;
      }
      const submission = submissionFrom(reply);
      if (!submission) { setStep(problemFor('invalid_response')); return; }
      afterVerification(submission);
    } else if (step?.kind === 'factorSetup' && h.setup) {
      const reply = await call('desktop_claim_review', { type: 'finish_totp_setup', receipt: h.receipt, setup: h.setup, code });
      if (!reply) return;
      if (!reply.ok) { setStep(WRONG_SETUP_CODE.has(reply.code) ? { ...step, code: '', invalid: true } : problemFor(reply.code)); return; }
      const submission = submissionFrom(reply);
      if (!submission) { setStep(problemFor('invalid_response')); return; }
      h.setup = null;
      const recovered = obj(reply.value?.recovery);
      const codes = Array.isArray(recovered?.codes) ? recovered.codes.filter((each): each is string => typeof each === 'string') : [];
      // Recovery codes are shown once, here, until the person says they saved them.
      if (recovered?.state === 'issued' && codes.length) { h.submission = submission; setStep({ kind: 'codes', codes }); return; }
      afterVerification(submission);
    }
  }), [run, call, step, afterVerification, setStep]);

  const passkey = useCallback(() => run(async () => {
    const h = handles.current;
    if (!h.receipt || !h.submission || step?.kind !== 'factor') return;
    setStep({ ...step, error: null });
    const reply = await call('desktop_claim_review', { type: 'submit_factor_passkey', receipt: h.receipt, submission: h.submission });
    if (!reply) return;
    if (!reply.ok) {
      setStep(PASSKEY_FAILED.has(reply.code) ? { ...step, error: 'passkey' }
        : reply.code === 'rate_limited' ? { ...step, error: 'rateLimited', retryMinutes: Math.max(1, Math.ceil((reply.retryAfter ?? 60) / 60)) }
          : problemFor(reply.code));
      return;
    }
    const submission = submissionFrom(reply);
    if (!submission) { setStep(problemFor('invalid_response')); return; }
    afterVerification(submission);
  }), [run, call, step, afterVerification, setStep]);

  const beginSetup = useCallback(() => run(async () => {
    const h = handles.current;
    if (!h.receipt || !h.submission) return;
    const reply = await call('desktop_claim_review', { type: 'prepare_totp_setup', receipt: h.receipt, submission: h.submission });
    if (!reply) return;
    const setup = reply.ok ? str(reply.value?.setup) : null;
    const uri = reply.ok ? str(reply.value?.qr_payload) : null;
    const secret = reply.ok ? str(reply.value?.manual_key) : null;
    if (!reply.ok || !setup || !uri || !secret) { setStep(problemFor(reply.ok ? 'invalid_response' : reply.code)); return; }
    h.setup = setup;
    setStep({ kind: 'factorSetup', material: { uri, secret }, code: '' });
  }), [run, call, setStep]);

  const savedCodes = useCallback(() => run(async () => {
    const submission = handles.current.submission;
    if (submission) afterVerification(submission);
  }), [run, afterVerification]);

  const setCode = useCallback((code: string) => {
    setStep((current) => current && (current.kind === 'code' || current.kind === 'factor' || current.kind === 'factorSetup') ? { ...current, code } : current);
  }, [setStep]);
  const factorStep = useCallback((method: 'passkey' | 'totp' | 'recovery') => {
    setStep((current) => current?.kind === 'factor' ? { ...current, method, code: '', error: null } : current);
  }, [setStep]);

  const close = useCallback(() => {
    const receipt = handles.current.receipt;
    reset();
    if (receipt) void invoke('desktop_claim_review', latest.current?.generation ?? 0, { type: 'cancel', receipt });
    if (showing) void observe();
  }, [reset, observe, showing]);

  const restart = useCallback(() => {
    const receipt = handles.current.receipt;
    reset();
    if (receipt) void invoke('desktop_claim_review', latest.current?.generation ?? 0, { type: 'cancel', receipt });
    void start();
  }, [reset, start]);

  const resume = useCallback(() => run(async () => {
    const receipt = recovery?.receipt;
    if (!receipt) return;
    const reply = await call('desktop_account_recovery', { type: 'resume', receipt });
    if (!reply) return;
    const answer = reply.ok ? str(reply.value?.state) : null;
    if (answer === 'saved') { setRecovery(null); void observe(); return; }
    // Read back but not saved yet, or a retryable save: a new receipt for the next press.
    const renewed = reply.ok && answer === 'available' ? str(reply.value?.receipt) : null;
    if (renewed) { setRecovery({ state: 'available', receipt: renewed }); return; }
    setRecovery({ state: answer === 'storage_blocked' ? 'blocked' : 'gone', receipt: null });
  }), [run, call, recovery, observe]);

  return {
    binding, recovery: recovery?.state ?? null, step, busy,
    account: review.account, computer: review.computer,
    start, changeName, pick, confirm, sendCode, submitCode, passkey, beginSetup, savedCodes, setCode, factorStep,
    close, restart, resume, recoverSaving, observe,
  };
}
