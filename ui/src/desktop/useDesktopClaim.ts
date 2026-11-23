import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { hasDesktopClient, type DesktopClientState } from './bridge';
import type { BindField, BindProblem, BindStep, ComputerBinding, EligibleName } from './BindComputerView';

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
  response_too_large: 'unreachable',
  // avibe.bot answered, but not with a verdict this page can name: say only that it did not finish.
  code_delivery_unknown: 'unknown', factor_delivery_unknown: 'unknown',
  bridge_unavailable: 'unreachable',
  authentication_required: 'signedOut', account_changed: 'signedOut', signed_out: 'signedOut', subject_mismatch: 'signedOut',
  rate_limited: 'rateLimited',
  expired: 'expired', decision_changed: 'expired', stale_generation: 'expired', invalid_or_expired: 'expired',
  claim_in_progress: 'busy', busy: 'busy', operation_in_progress: 'busy',
  local_runtime_unavailable: 'notResponding', local_admission_failed: 'notResponding', invalid_local_descriptor: 'notResponding',
  fresh_claim_required: 'lost',
  // avibe.bot no longer accepts this computer (a deleted, never-paired one; or one whose earlier
  // pairing needs a browser on it): trying again cannot change it, so it is a dead end.
  binding_unavailable: 'unavailable', paired_installation_loopback_required: 'unavailable',
  already_bound: 'changed', local_binding_changed: 'changed',
  // An older Desktop's attempt still holds this computer (shown as not bound): it is still running,
  // or still publishing. Pressing again gets the same refusal, so the sentence says to wait.
  local_instance_conflict: 'busy', pairing_recovery_required: 'busy',
  local_binding_unavailable: 'notResponding', local_claim_unavailable: 'notResponding',
  claim_unavailable: 'expired',
  reauthentication_failed: 'reauth',
};
/** The longest delay one timer takes; a longer wait is re-armed until its deadline. */
const MAX_TIMER_MS = 2 ** 31 - 1;
/**
 * Calls `due` once the absolute `deadline` (epoch ms) has passed, never earlier: a timer that wakes
 * early, or a wait longer than one timer can hold, is re-armed against the clock. Returns a cancel.
 */
export function afterDeadline(deadline: number, due: () => void, now: () => number = Date.now): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const check = () => {
    const left = deadline - now();
    if (left <= 0) { timer = null; due(); return; }
    timer = setTimeout(check, Math.min(left, MAX_TIMER_MS));
  };
  check();
  return () => { if (timer !== null) clearTimeout(timer); };
}
/** When a 429's wait ends: native refuses an earlier try, so the step stays locked until then. */
const until = (reply: Reply) => Date.now() + 1000 * (!reply.ok && reply.retryAfter ? reply.retryAfter : 60);
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

/** The Account page shows only whether it can be used and what to do (owner, 2026-11-23). An older
 *  attempt still running or ended is "not bound": native refuses or replaces it on Bind, and the
 *  dialog says the one sentence. Only a repair no button here can do keeps its own line. */
const OBSERVED: Record<string, ComputerBinding['state']> = {
  // An older Desktop's record that ended without a result: native does not replace it yet (rule
  // table, row 78), so Bind could only be refused. The same dead end as a computer avibe.bot refuses.
  // Only an exact `no_published_binding` admits a review (native, 2026-11-23): an older attempt
  // still running or not yet published is waited out, never opened over.
  no_published_binding: 'unbound', legacy_review_required: 'unavailable', legacy_wait: 'pending',
  legacy_publication_pending: 'pending', owner_observed: 'bound', bound_owner_unconfirmed: 'otherAccount',
  legacy_recovery_required: 'repair',
};
/** Native read this computer's binding here, then could not read the account's list: bound, so
 *  shown bound while it is read again quietly. Every other failure may come before that. */
const BOUND_UNREAD = 'account_observation_unavailable';
const QUIET_RETRY_MS = 30_000;

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

/** The claim view's own face for what observing found, when no flow is open. Opening a review is
 *  only for a computer with no published binding (native reviewer, 2026-11-23). */
function observedStep(binding: ComputerBinding, settled: boolean): BindStep {
  switch (binding.state) {
    case 'bound': return { kind: 'bound', host: binding.host ?? '' };
    case 'otherAccount': case 'unknown': case 'repair': case 'pending': return { kind: 'notHere', reason: binding.state };
    case 'upgrade': return { kind: 'upgrade' };
    case 'unavailable': return { kind: 'problem', problem: 'unavailable' };
    // Unbound and nothing open once the opening settled: the review ended without a step to show.
    case 'unbound': return settled ? { kind: 'problem', problem: 'unknown' } : { kind: 'opening' };
    default: return { kind: 'opening' };
  }
}

/**
 * This computer's binding. `account`: the Account page reads it, read-only. `claim`: the shell's
 * claim view (avibe://desktop/claim), which a person's 「开启远程访问」 in the Workbench opened; it
 * signs in if needed, observes, and opens one review only when nothing is bound, once per document.
 */
export function useDesktopClaim(state: DesktopClientState | null, enabled: boolean, mode: 'account' | 'claim' = 'account') {
  const claimView = mode === 'claim';
  const generation = state?.generation ?? 0;
  const latest = useRef(state);
  useLayoutEffect(() => { latest.current = state; });
  const handles = useRef<Handles>({ ...NO_HANDLES });
  // `published`: native said this submission's sign-in is published and owes a second factor, so
  // the account's gate, once it shows that, is this sign-in's and not an earlier snapshot's.
  // `thenOpen`: this recovery was continued from Bind, so the name opens once it is saved.
  const saving = useRef<{ tries: number; running: boolean; recover: string | null; timer: number | null; published: boolean; thenOpen?: boolean } | null>(null);
  const stopSaving = useCallback(() => {
    if (saving.current?.timer) window.clearTimeout(saving.current.timer);
    saving.current = null;
  }, []);
  const [binding, setBinding] = useState<ComputerBinding>({ state: 'reading' });
  // An accepted sign-in waiting to be saved: Bind continues it first.
  const recovery = useRef<string | null>(null);
  // Closing the flow reads again itself (close()); a flow opening or ending is not a new showing.
  const flowOpen = useRef(false);
  const quiet = useRef<number | null>(null);
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
  // Busy belongs to the generation its action started in, like the flow: a retired generation's
  // action cannot leave the page disabled, whether or not its finalizer still runs.
  const [busyAt, setBusyAt] = useState<number | null>(null);
  const busy = busyAt === generation;
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
    setBusyAt(null);
  }, [setStep]);

  const observe = useCallback(async (quietly = false): Promise<ComputerBinding['state'] | null> => {
    if (quiet.current !== null) { window.clearTimeout(quiet.current); quiet.current = null; }
    if (!quietly) setBinding({ state: 'reading' });
    const reply = await call('desktop_claim_review', { type: 'observe_current_binding' });
    if (!reply) return null;
    const local = latest.current?.local;
    const listed = latest.current?.catalog.instances.find((each) => each.id === local?.instance_id);
    const url = local?.remote_access_url ?? null;
    const bound = (): ComputerBinding => ({ state: 'bound', host: listed?.host ?? (url ? new URL(url).host : null), url: local?.remote_access === 'on' ? url : null });
    if (!reply.ok) {
      if (reply.code === BOUND_UNREAD) {
        setBinding(bound());
        quiet.current = window.setTimeout(() => { quiet.current = null; void observe(true); }, QUIET_RETRY_MS);
        return 'bound';
      }
      const failed = reply.code === 'local_runtime_upgrade_required' ? 'upgrade' : 'unknown';
      setBinding({ state: failed });
      return failed;
    }
    const kind = OBSERVED[str(reply.value?.state) ?? ''];
    setBinding(kind === 'bound' ? bound() : kind ? { state: kind } as ComputerBinding : { state: 'unknown' });
    return kind ?? 'unknown';
  }, [call]);
  useEffect(() => () => { if (quiet.current !== null) window.clearTimeout(quiet.current); }, []);

  // An accepted sign-in waiting to be saved is the one thing to do here, whatever the card can read
  // (an occupied binding slot blocks observation; an owed factor shows no card): show it as the
  // dialog's Continue. Showing it decides nothing; only the press resumes.
  const inspect = useCallback(async (show = true) => {
    const reply = await call('desktop_account_recovery', { type: 'inspect' });
    if (!reply) return;
    const receipt = reply.ok && reply.value?.state === 'available' ? str(reply.value?.receipt) : null;
    // Kept also while shown: a person who cancels the dialog meets it again on Bind.
    recovery.current = receipt;
    if (!show || !receipt || flowOpen.current) return;
    if (saving.current?.timer) window.clearTimeout(saving.current.timer);
    saving.current = { tries: 0, running: false, recover: receipt, timer: null, published: false, thenOpen: false };
    setStep({ kind: 'saving', recover: true });
  }, [call, setStep]);

  // The two reads, once per showing of the Account page and per account generation.
  // A changed generation drops the old flow's handles and any reply still on its way.
  const seen = useRef(generation);
  useEffect(() => {
    if (seen.current === generation) return;
    seen.current = generation;
    epoch.current += 1;
    handles.current = { ...NO_HANDLES };
  }, [generation]);
  // (Before the reads below, so a new generation's read is not dropped as the old one's.)
  const showing = enabled && state?.account.status === 'signed_in';
  // A sign-in accepted while binding may publish an account that still owes a second factor; its
  // recovery must stay reachable then too.
  useEffect(() => { flowOpen.current = step !== null; }, [step]);
  // Only the claim view binds, so only it finishes saving an accepted sign-in. Signed in, its
  // opening reads that first (begin, below); owing a factor, it is shown as soon as it is read.
  const recoverable = enabled && claimView && state?.account.status === 'second_factor_required';
  useEffect(() => {
    if (claimView || !showing || flowOpen.current) return;
    void observe();
  }, [claimView, showing, generation, observe]);
  useEffect(() => {
    if (!recoverable || flowOpen.current) return;
    void inspect();
  }, [recoverable, generation, inspect]);


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
        next = { kind: 'name', name, check, held: eligible };
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
        // A change no field shows (avibe.bot's suggestion, say) is still a change: say so plainly.
        next = { ...next, changed };
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
    setBusyAt(latest.current?.generation ?? 0);
    const mine = epoch.current;
    try { await work(); } finally { if (mine === epoch.current) setBusyAt(null); }
  }, [busy]);

  /** Opens a review (one ticket) and shows what native read for it. */
  // Native admits one review per claim document, after this generation observed an exact
  // `no_published_binding`. Once sent, the way on after a failure is closing, not opening again.
  const [opened, setOpened] = useState(false);
  const openReview = useCallback(async () => {
    setOpened(true);
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
  }, [call, showReview, setStep]);

  const start = useCallback(() => run(async () => {
    handles.current = { ...NO_HANDLES };
    nameRef.current = '';
    // An accepted sign-in still waiting to be saved comes first: the dialog continues it on a
    // press (no new code), then opens the name.
    if (recovery.current) {
      stopSaving();
      saving.current = { tries: 0, running: false, recover: recovery.current, timer: null, published: false, thenOpen: true };
      recovery.current = null;
      setStep({ kind: 'saving', recover: true });
      return;
    }
    await openReview();
  }), [run, openReview, setStep, stopSaving]);

  /** The claim view's opening: an accepted sign-in to finish saving comes first; otherwise observe,
   *  and open a review only when nothing is bound. Also 「再看一次」, which a person presses. */
  const [settled, setSettled] = useState(false);
  const begin = useCallback(async () => {
    setSettled(false);
    try {
      await inspect(false);
      if (recovery.current) { await start(); return; }
      if (await observe() === 'unbound' && !flowOpen.current) await start();
    } finally {
      setSettled(true);
    }
  }, [inspect, observe, start]);
  // Once per document, the first time it is signed in: never again on a re-show, a reconnect, a
  // changed generation or a state event. The shell opens a new document for each request.
  const begun = useRef(false);
  useEffect(() => {
    if (!claimView || !showing || begun.current || flowOpen.current) return;
    begun.current = true;
    void begin();
  }, [claimView, showing, begin]);

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
      // The latch spans the whole chain: showReview checks again itself for a name typed since.
      h.checking = true;
      void call('desktop_claim_review', { type: 'check', revision: h.revision, prefix: nameRef.current }).then(async (reply) => {
        try {
          if (!reply) return;
          if (!reply.ok) { setStep(problemFor(reply.code)); return; }
          await showReview(reply.value ?? {}, 'check');
        } finally {
          h.checking = false;
        }
      });
    }, 400);
  }, [call, showReview, setStep]);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);

  const pick = useCallback((prefix: string) => run(async () => {
    const h = handles.current;
    if (!h.revision || h.checking) return;
    if (timer.current !== null) { window.clearTimeout(timer.current); timer.current = null; }
    nameRef.current = prefix;
    h.checking = true;
    try {
      const reply = await call('desktop_claim_review', { type: 'check', revision: h.revision, prefix });
      if (!reply) return;
      if (!reply.ok) { setStep(problemFor(reply.code)); return; }
      await showReview(reply.value ?? {}, 'check');
    } finally {
      h.checking = false;
    }
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
    if (!reply.ok && reply.code === 'authentication_required') {
      // Published, and the new sign-in owes a second factor: the account's gate says which.
      current.published = true;
      if (owedFactor()) { stopSaving(); return; }
      if (retry()) return;
    }
    if (reply.ok && retry()) return;
    // Publishing stopped (a failed read-back or save pauses it for the account's own recovery),
    // or did not end in time: continue that on a press, with no new code, if native offers it.
    const inspected = await call('desktop_account_recovery', { type: 'inspect' });
    if (saving.current !== current) return;
    const receipt = inspected?.ok && inspected.value?.state === 'available' ? str(inspected.value.receipt) : null;
    if (receipt) { current.recover = receipt; setStep({ kind: 'saving', recover: true }); return; }
    stopSaving();
    setStep(reply.ok ? { kind: 'problem', problem: 'expired' } : problemFor(reply.code));
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
    // This receipt is spent: a renewed one, if any, is the only one left.
    recovery.current = renewed;
    if (answer === 'saved' && current.thenOpen) {
      // Saved: observe again, and open only when nothing is bound.
      stopSaving();
      if (await observe() === 'unbound') { await openReview(); return; }
      handles.current = { ...NO_HANDLES };
      setStep(null);
    } else if (answer === 'saved' && !handles.current.submission) {
      // Continued from the page, not from a binding: done; read this computer again.
      stopSaving();
      reset();
      void observe();
    } else if (answer === 'saved') {
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
  }), [run, call, openReview, setStep, stopSaving, reset, observe]);

  const submissionFrom = (reply: Reply | null): string | null => reply?.ok ? str(reply.value?.submission) : null;

  const submitCode = useCallback((code: string) => run(async () => {
    const h = handles.current;
    if (!h.receipt || waitUntil) return;
    if (step?.kind === 'code' && h.delivery) {
      const reply = await call('desktop_claim_review', { type: 'submit_proof_code', receipt: h.receipt, delivery: h.delivery, code });
      if (!reply) return;
      if (!reply.ok) {
        setStep(WRONG_CODE.has(reply.code) ? { kind: 'code', code: '', invalid: true }
          : reply.code === 'rate_limited' ? { kind: 'code', code: '', limited: true, waitUntil: until(reply) } : problemFor(reply.code));
        return;
      }
      const submission = submissionFrom(reply);
      if (!submission) { setStep(problemFor('invalid_response')); return; }
      h.delivery = null;
      afterVerification(submission);
    } else if (step?.kind === 'factor' && step.method !== 'passkey' && h.submission) {
      const reply = await call('desktop_claim_review', { type: 'submit_factor_code', receipt: h.receipt, submission: h.submission, method: step.method, code });
      if (!reply) return;
      if (!reply.ok) {
        setStep(WRONG_CODE.has(reply.code) ? { ...step, code: '', error: 'code' }
          : reply.code === 'rate_limited' ? { ...step, code: '', error: 'rateLimited', retryMinutes: Math.max(1, Math.ceil((reply.retryAfter ?? 60) / 60)), waitUntil: until(reply) }
            : problemFor(reply.code));
        return;
      }
      const submission = submissionFrom(reply);
      if (!submission) { setStep(problemFor('invalid_response')); return; }
      afterVerification(submission);
    } else if (step?.kind === 'factorSetup' && h.setup) {
      // Native takes six ASCII digits only and ends the claim on anything else: correct it here.
      const digits = code.replace(/\s/g, '');
      if (!/^\d{6}$/.test(digits)) { setStep({ ...step, invalid: true }); return; }
      code = digits;
      const reply = await call('desktop_claim_review', { type: 'finish_totp_setup', receipt: h.receipt, setup: h.setup, code });
      if (!reply) return;
      if (!reply.ok) {
        // A wrong code or a 429 keeps the issued setup (its QR and key); only an expired one ends it.
        setStep(WRONG_SETUP_CODE.has(reply.code) ? { ...step, code: '', invalid: true }
          : reply.code === 'rate_limited' ? { ...step, code: '', invalid: false, limited: true, retryMinutes: Math.max(1, Math.ceil((reply.retryAfter ?? 60) / 60)), waitUntil: until(reply) }
            : problemFor(reply.code));
        return;
      }
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
    if (!h.receipt || !h.submission || step?.kind !== 'factor' || step.waitUntil) return;
    setStep({ ...step, error: null });
    const reply = await call('desktop_claim_review', { type: 'submit_factor_passkey', receipt: h.receipt, submission: h.submission });
    if (!reply) return;
    if (!reply.ok) {
      setStep(PASSKEY_FAILED.has(reply.code) ? { ...step, error: 'passkey' }
        : reply.code === 'rate_limited' ? { ...step, error: 'rateLimited', retryMinutes: Math.max(1, Math.ceil((reply.retryAfter ?? 60) / 60)), waitUntil: until(reply) }
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

  // A step waiting out a 429 unlocks itself when the wait ends, with its message cleared.
  const waitUntil = step && (step.kind === 'code' || step.kind === 'factor' || step.kind === 'factorSetup') ? step.waitUntil : undefined;
  useEffect(() => {
    if (!waitUntil) return;
    return afterDeadline(waitUntil, () => setStep((current) => {
      if (!current || !(current.kind === 'code' || current.kind === 'factor' || current.kind === 'factorSetup') || current.waitUntil !== waitUntil) return current;
      return current.kind === 'code' ? { ...current, limited: false, waitUntil: undefined }
        : current.kind === 'factor' ? { ...current, error: null, waitUntil: undefined }
          : { ...current, limited: false, waitUntil: undefined };
    }));
  }, [waitUntil, setStep]);
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
    // The claim view leaves for the Workbench; the Account page shows the card again.
    if (showing && !claimView) void observe();
  }, [reset, observe, showing, claimView]);

  const restart = useCallback(() => {
    const receipt = handles.current.receipt;
    reset();
    if (receipt) void invoke('desktop_claim_review', latest.current?.generation ?? 0, { type: 'cancel', receipt });
    // The claim view has one review: before it was sent, starting again observes first.
    if (!claimView) void start();
    else if (!opened) void begin();
  }, [reset, start, begin, claimView, opened]);


  // The claim view always shows the dialog: the App's sign-in first if needed, then the flow, or
  // what observing found.
  const shown: BindStep | null = !claimView ? step
    : step ?? (state && state.account.status !== 'signed_in' ? { kind: 'signIn' } : observedStep(binding, settled));
  return {
    binding, step: shown, busy, recheck: begin,
    /** Whether 「重新开始」 can do anything: the claim view's one review not yet sent. */
    restartable: !claimView || !opened,
    account: review.account, computer: review.computer,
    start, changeName, pick, confirm, sendCode, submitCode, passkey, beginSetup, savedCodes, setCode, factorStep,
    close, restart, recoverSaving, observe,
  };
}
