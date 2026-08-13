// Add-subscription dialog (frame 04). RENDERS DECLARATIVELY from the runtime-declared
// oauth-flow presentation (S1 gap ③): `expects` ∈ none | paste_code |
// paste_callback_url selects the step-2 control; there is NO vendor→form table
// in the UI. Composes the shared OAuth atoms (OAuthLinkRow / OAuthDeviceCodeRow
// / OAuthSubmitRow) so it matches the Backends OAuth panel. State machine
// mirrors BackendOAuthPanel: start → 2s poll → verifying → success, 15-min
// timeout, cancel.
//
// It runs the RE-AUTH journey too (`reauth` prop, AC-2/AC-13). api.md gives both
// intents one flow and one terminal envelope — only the tail beside it differs
// (`adopted_by` for a create, `recovered`/`interrupted_pairs` for a repair) — so
// they share this machine rather than getting a second copy of the poll, the
// deadline, the paste submit and the cancel-on-unmount. A duplicate is precisely
// how one of the two ends up reading a different half of the envelope, which is
// the bug `settle` below was written to fix.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { ArrowRight, CheckCircle2, Info, Sparkles, TriangleAlert, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { OAuthDeviceCodeRow, OAuthLinkRow, OAuthSubmitRow } from '../oauth/OAuthFlowParts';
import { AdoptionNote } from './AdoptionNote';
import {
  createFlowAuthority,
  failureLanded,
  initialFlowView,
  isDone,
  pollFailureSettles,
  releaseFlow,
  startNeedsStatusRead,
  terminalArrivalMovedRows,
  type FlowAuthority,
  type FlowView,
} from './asyncLifetime';
import { apiFailure, modelsApi, type Adoption, type OAuthResult } from './modelsApi';
import { REPAIR_LINE_KEY, REPAIR_TOAST, repairOutcome, repairSettles, type RepairOutcome } from './repair';
import { NATIVE_SUBSCRIPTION_SLOT_FAILURE, oauthFailureKey, oauthStartFailureKey, serverText, type OAuthJourney } from './serverCopy';
import {
  initialSubscriptionChannel,
  nativeSubscriptionSlotTaken,
  recommendedSubscriptionChannel,
  subscriptionOptionOrder,
  subscriptionVendorCopy,
} from './subscriptionOptions';
import { GuardGapList } from './GuardGapList';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';
import type { Source, SupplyChannel, SupplyGap } from './types';

const POLL_MS = 2000;
const DEADLINE_MS = 16 * 60 * 1000;

type ConnectPhase = 'choose' | 'flow';

const CHANNELS: SupplyChannel[] = ['native_cli', 'hub'];

const Step: React.FC<{ n: number; label: string; children: React.ReactNode }> = ({ n, label, children }) => (
  <div className="flex flex-col gap-2.5 rounded-lg border border-border bg-surface-2/40 px-4 py-3">
    <span className="text-[13px] font-medium text-foreground">
      <span className="font-mono text-muted">{n} · </span>
      {label}
    </span>
    {children}
  </div>
);

export const OAuthConnectDialog: React.FC<{
  open: boolean;
  /** 'anthropic' (Claude) | 'openai' (ChatGPT) | any future subscription vendor. */
  vendor: string;
  /**
   * Present ⇒ this is a RE-AUTH of that existing source, not a new connect. A
   * SNAPSHOT on purpose: a native re-auth writes 需处理 on the row before the login
   * even starts, so re-reading the live row would rewrite this dialog's own
   * subject mid-flow.
   */
  reauth?: Source | null;
  /** Snapshot candidates used to decide whether this backend's native slot is occupied. */
  sources?: Source[];
  onClose: () => void;
  onConnected: (source?: Source, placement?: Adoption) => void;
}> = ({ open, vendor, reauth = null, sources = [], onClose, onConnected }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [view, setView] = React.useState<FlowView>(initialFlowView);
  const [code, setCode] = React.useState('');
  const [submitting, setSubmitting] = React.useState(false);
  // Seed the chooser from the opening snapshot. Radix may autofocus a control
  // before the passive open effect runs; deriving this here prevents an occupied
  // native row from ever being the initially focused/selected option.
  const [channel, setChannel] = React.useState<SupplyChannel>(() =>
    reauth ? (reauth.supply_channel ?? 'native_cli') : initialSubscriptionChannel(vendor, sources),
  );
  const [phase, setPhase] = React.useState<ConnectPhase>('choose');
  const [nativeSlotTaken, setNativeSlotTaken] = React.useState(() =>
    !reauth && nativeSubscriptionSlotTaken(vendor, sources),
  );
  const [startAttempt, setStartAttempt] = React.useState(0);
  const [startFailureCode, setStartFailureCode] = React.useState<string | null>(null);
  // Which Agents took the new subscription in, frozen at commit (api.md). Same
  // note as the API-key dialog: connecting a credential is not the same as
  // putting it into service, and an Agent with no accepted match is absent.
  //
  // `null` means the terminal response did not report a creation — which is not
  // 「没有 Agent 采用」 and must not be rendered as it.
  //
  // The whole tail rather than the adopter list alone, held as one value: the note
  // reads both halves, and two states can hold one half of an older arrival.
  const [adoption, setAdoption] = React.useState<Adoption | null>(null);
  // The reauth counterpart of `adoption`, read through the same owner the key
  // replacement uses so 「did that fix it?」 has one answer on the page.
  const [repair, setRepair] = React.useState<RepairOutcome | null>(null);
  // What a FAILED reauth stranded on its way down. Not the same thing as
  // `repair`'s gap report: that one describes a write that landed, this one
  // describes the irreversible half of a journey that then broke, and it exists
  // only on the error the server threw.
  const [stranded, setStranded] = React.useState<SupplyGap[]>([]);
  const [, tick] = React.useReducer((x) => x + 1, 0);

  // Set by the flow effect so `submit` lands through the very same owners as a
  // poll response: one authority owns the complete view and one handler owns
  // terminal side effects.
  const flowAuthorityRef = React.useRef<FlowAuthority | null>(null);
  const settleRef = React.useRef<((result: OAuthResult) => void) | null>(null);
  const successTimer = React.useRef<number | null>(null);
  // Mirrored for the flow effect, which is built once per attempt and would
  // otherwise read this from the render that built it — i.e. always `false`.
  const submittingRef = React.useRef(false);
  submittingRef.current = submitting;
  const onConnectedRef = React.useRef(onConnected);
  onConnectedRef.current = onConnected;
  const onCloseRef = React.useRef(onClose);
  onCloseRef.current = onClose;
  const initializedOpenSubject = React.useRef<string | null>(null);
  const clientNonce = React.useRef<string | null>(null);
  const providerWindow = React.useRef<Window | null>(null);
  const heldFlowId = React.useRef<string | null>(null);
  const rereadHeldFlow = React.useRef<(() => Promise<boolean>) | null>(null);

  const preopenProviderWindow = React.useCallback(() => {
    if (providerWindow.current && !providerWindow.current.closed) return;
    try {
      providerWindow.current = window.open('about:blank', '_blank');
    } catch {
      providerWindow.current = null;
    }
  }, []);

  const createClientNonce = React.useCallback(() => {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return `ofn_${uuid.replaceAll('-', '').toLowerCase()}`;
    const bytes = new Uint8Array(16);
    globalThis.crypto?.getRandomValues?.(bytes);
    return `ofn_${Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')}`;
  }, []);

  const accent = vendor === 'openai' ? 'gold' : 'mint';
  // One derivation for 「which journey is this」, read by the start call, the
  // terminal handler and the title alike.
  const reauthId = reauth?.id ?? null;
  const isReauth = reauthId !== null;
  // The same derivation as a value, because the failure copy needs it as one: a
  // terminal code means 「couldn't create it」 on a connect and 「it's still broken」
  // on a repair, and `oauthFailureKey` takes the journey rather than guessing.
  const journey: OAuthJourney = isReauth ? 'reauth' : 'connect';

  // Take the native-slot reading once per open. A source arriving after this is
  // the singleton race the start route owns; it must surface as Already bound,
  // not silently rewrite a choice already under the user's pointer.
  const openSubject = open ? `${vendor}:${reauthId ?? 'create'}` : null;
  React.useEffect(() => {
    if (!openSubject) {
      initializedOpenSubject.current = null;
      clientNonce.current = null;
      setStartAttempt(0);
      setStartFailureCode(null);
      setPhase('choose');
      return;
    }
    if (initializedOpenSubject.current === openSubject) return;
    initializedOpenSubject.current = openSubject;
    clientNonce.current = null;
    setStartAttempt(0);
    setStartFailureCode(null);
    if (isReauth) {
      setChannel(reauth?.supply_channel ?? 'native_cli');
      setPhase('flow');
      return;
    }
    const occupied = nativeSubscriptionSlotTaken(vendor, sources);
    setNativeSlotTaken(occupied);
    setChannel(initialSubscriptionChannel(vendor, sources));
    setPhase('choose');
  }, [isReauth, openSubject, reauth?.supply_channel, sources, vendor]);

  /**
   * One owner for 「the server moved the rows the page behind this dialog draws」,
   * reached from EVERY path where that can have happened.
   *
   * The question at each call site is deliberately that one — not 「did it fail?」,
   * and not 「is this a reauth?」. A reauth makes the difference impossible to miss,
   * because its row is rewritten as the flow starts: `mark_native_irreversible_start`
   * writes 需要处理 across that vendor's native sources and rolls it back only when
   * the login fails to SPAWN, and `_materialize_reauth` clears the source and marks
   * it unavailable before answering `discovery_failed`. But a create writes too, at
   * the other end — `_materialize_completed_oauth` persists the new source, and
   * `oauth_cancel` routes a `success` flow into it, so the call that was meant to
   * throw the journey away is the one that commits it. Scoping the refetch to the
   * journey is what left that page showing no source over a credential the server
   * had already taken.
   *
   * A refetch of rows that did not move is inert, which is what makes it the wrong
   * thing to be clever about. The pairs are not: they travel on the error for the
   * same reason `adopted_by` travels with a creation — no later read of `/agents`
   * reproduces them — and they name who the REAUTH's irreversible half left
   * without a source. That half is what a create does not have, so that is the one
   * part still asking about the journey.
   *
   * Which is why `pairsSpeak` exists beside them rather than being read off the
   * failure: passing no pairs still WRITES an empty list, so 「this arrival has
   * none」 and 「this arrival may not answer that question」 need saying separately.
   * It defaults to the SILENT answer, because the two answers are not equally
   * wrong. This component outlives the attempt — both hosts leave it mounted and
   * toggle `open`, so `stranded`
   * survives a close, and a request left in flight by attempt A can land after
   * attempt B has already put ITS gap report on screen. A site that forgets to
   * speak costs nothing: the refetch it came for still runs. A site that forgets
   * to stay silent erases the one part of B's report no later read reproduces.
   *
   * So exactly one arrival opts IN — the terminal settle, which is the account of
   * record for the view it just settled and has no pairs to show, because
   * `interrupted_pairs` rides an error envelope and a terminal RESPONSE has none.
   * The three `catch` sites hand over `failureLanded(step.action)`, since an
   * arrival the authority ignored is not the failure whose sentence these pairs
   * are rendered under. Everything reached after the attempt is over — the
   * resolved-after-close path, the released flow's re-read — says nothing, and
   * says it by default.
   */
  const rowsBehindAreStale = React.useCallback((failure?: ReturnType<typeof apiFailure>, pairsSpeak = false) => {
    if (isReauth && pairsSpeak) setStranded(failure?.interrupted ?? []);
    onConnectedRef.current();
  }, [isReauth]);

  /**
   * A request of one attempt that resolved after that attempt stopped being the one
   * on screen — because the dialog closed, or because a later attempt replaced it.
   * Those are one fact here: whatever this arrival wrote, it wrote it to rows
   * somebody else is now reading.
   *
   * The close path re-read too — but it did so while this request was still in
   * flight, so that read can predate the request's own write, and every request
   * here has one: a status poll MATERIALIZES a just-succeeded flow, a paste submit
   * does the same and can write BEFORE it rejects (`_materialize_reauth` saves the
   * source as 需处理 with its models stripped and only then raises
   * `discovery_failed`), a start that rejects can have kept
   * `mark_native_irreversible_start`'s marking, and `oauth_cancel` itself routes a
   * terminal flow into materialization. This is the last thing that corrects the
   * rows, which is why it lives out here where every one of those exits can reach
   * it — a request that outlived its attempt is one rule, not one per call site.
   *
   * It says nothing about the pairs, and takes the default to say so. Not because
   * it has none to hand over — it never does — but because the empty list is
   * itself a claim, and this arrival has no standing to make it: the dialog that
   * would have shown ITS pairs is gone, while the one on screen may already
   * belong to a later attempt. Closing a re-login and starting another on a
   * different source is one click away, and this request can land after that
   * one has failed.
   */
  const resolvedAfterAttempt = React.useCallback(() => rowsBehindAreStale(), [rowsBehindAreStale]);

  const copy = (text: string | null | undefined) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!text) return;
    // navigator.clipboard is undefined in non-secure contexts / older browsers;
    // touching .writeText there throws synchronously, not as a rejected promise.
    if (!navigator.clipboard?.writeText) {
      showToast(t('common.copyFailed') as string, 'error');
      return;
    }
    navigator.clipboard
      .writeText(text)
      .then(() => showToast(t('common.copied') as string, 'success'))
      .catch(() => showToast(t('common.copyFailed') as string, 'error'));
  };

  // Drive the flow only after the user confirms one channel.
  React.useEffect(() => {
    if (!open || phase !== 'flow') return;
    let cancelled = false;
    let pollTimer: number | null = null;
    let deadline = Date.now() + DEADLINE_MS;
    // The flow id this journey opened, kept beside the view instead of read back out
    // of it. Cleanup needs an id to cancel with, and the VIEW is exactly what
    // `startNeedsStatusRead` keeps a terminal start out of — on purpose. Taking the
    // id from there let 「what may be SHOWN」 decide 「what may be CANCELLED」, and the
    // one arrival whose cancel IS the materialization was the one it left with
    // neither.
    let openedFlowId: string | null = null;

    const stop = () => {
      if (pollTimer !== null) window.clearTimeout(pollTimer);
      pollTimer = null;
    };
    // The subject travels with the authority because `flowLetGo` asks about the
    // identity of whichever journey holds the ref, and the server's handover is
    // keyed by source: a successor re-authing a DIFFERENT row cannot be handed
    // this flow, so it may not stand in for one that could.
    const authority = createFlowAuthority(setView, reauthId);
    flowAuthorityRef.current = authority;
    const transition = authority.transition;

    // The single place a terminal result becomes a finished (or failed) connect.
    // Shared by the poll and the paste submit because api.md gives both the SAME
    // terminal shape — written twice, one of them ends up reading a different
    // half of the envelope, which is exactly the bug this replaces. WHICH arrival
    // is allowed to change what is shown is `flowStep`'s call (asyncLifetime.ts);
    // this function only carries out the side effects. Returns whether the flow is
    // done, so the caller stops polling.
    const settle = (result: OAuthResult): boolean => {
      const { created } = result;
      const step = transition({ kind: 'response', flow: result.flow });
      if (step.action === 'succeed' && isReauth) {
        // A reauth terminates on the SAME source it started from; what the server
        // adds is whether that cleared the blocker and what is still stranded.
        const verdict = result.repaired ? repairOutcome(result.repaired) : null;
        setRepair(verdict);
        // Line AND tone from the verdict's own owner (`REPAIR_TOAST`), because
        // choosing them separately here is what put a green 「连接成功」 over a gap
        // report. An absent tail is the only arrival with no verdict to speak for
        // it, and 「连接成功」 is what it has always said.
        const toast = verdict ? REPAIR_TOAST[verdict.kind] : null;
        showToast(
          t(toast?.key ?? 'settings.models.oauth.status.success') as string,
          toast?.tone ?? 'success',
        );
        // Same shape as the adoption auto-close below, on the same footing now that
        // both halves are server facts: 「nothing was stranded」 is a field the
        // server sends, so a clean repair may dismiss itself while a gap report
        // stays on screen to be read. An absent tail says nothing to read either,
        // so it closes too.
        if (!verdict || repairSettles(verdict))
          successTimer.current = window.setTimeout(() => onCloseRef.current(), 1400);
      } else if (step.action === 'succeed') {
        // The Source already exists. The status/submit call that first reports
        // success materializes it server-side and consumes the flow binding doing
        // it — there is nothing left to finalize, and a POST /sources afterwards
        // is refused as `flow_not_found` on a connect that in fact succeeded.
        setAdoption(created ? { added_to: created.added_to, adopted_by: created.adopted_by } : null);
        showToast(t('settings.models.oauth.status.success') as string, 'success');
        if (created) onConnectedRef.current(created.source, created);
      }
      // Both branches above end with the same fact about the page behind them, and
      // so does the failure neither of them handles: `terminalArrivalMovedRows`
      // says which arrivals moved the rows, in one place, for all of them. A
      // `failed` hub reauth has already been persisted as 需处理 with its models
      // stripped by the time this reads it — the two branches asked 「did it
      // succeed?」, and that is the one terminal the answer left behind.
      // The `true` is the one opt-in to speaking for the pairs, and it is this
      // arrival's to make: it just settled the view they render under, and it has
      // none — `interrupted_pairs` rides an error envelope, so a terminal RESPONSE
      // that says `failed` carries no list. Clearing them here is how a stale
      // report from an earlier arrival stops outliving the sentence it belonged to.
      if (terminalArrivalMovedRows(step.action)) rowsBehindAreStale(undefined, true);
      // A paste submit can terminate the flow while a poll timer is still armed;
      // the guard above already makes that poll harmless, but there is no reason
      // to let it fire.
      if (isDone(step.action)) stop();
      return isDone(step.action);
    };
    settleRef.current = settle;

    const poll = async (flowId: string) => {
      // Nothing has been requested yet on this tick, so there is nothing that
      // could have written — unlike the two exits below.
      if (cancelled) return;
      const overdue = transition({ kind: 'tick', overdue: Date.now() > deadline });
      if (isDone(overdue.action)) {
        // Keep the just-expired flow addressable. Retry performs one authoritative
        // status read before it is allowed to mint a fresh provider flow.
        if (overdue.action === 'timeout' && openedFlowId) {
          const timedOutFlowId = openedFlowId;
          rereadHeldFlow.current = async () => {
            try {
              const result = await modelsApi.getOAuthStatus(timedOutFlowId);
              if (cancelled || flowAuthorityRef.current !== authority) return true;
              transition({ kind: 'reset' });
              if (settle(result)) return true;
              // The held flow is still pending. Let retryStart continue with a
              // fresh acquisition; this status read was the required last chance
              // to observe a near-deadline terminal result.
              return false;
            } catch {
              return false;
            }
          };
        }
        return;
      }
      try {
        const result = await modelsApi.getOAuthStatus(flowId);
        if (cancelled) {
          resolvedAfterAttempt();
          return;
        }
        if (settle(result)) return;
        pollTimer = window.setTimeout(() => void poll(flowId), POLL_MS);
      } catch (err) {
        if (cancelled) {
          resolvedAfterAttempt();
          return;
        }
        // A poll that lands on a just-succeeded flow is also the call that
        // materializes the outcome, so it can fail for reasons that have nothing
        // to do with the authorization (for example discovery_failed):
        // the vendor said yes and what came after it broke. Naming that
        // separately is the difference between 「重试授权」 and 「授权成功，后面
        // 没成」 — and WHICH object it broke is the journey's to say.
        //
        // Which phase a code belongs to is `oauthFailureKey`'s to decide, not
        // this site's: the SAME poll can fail before authorization completes,
        // and the codes that only exist after it are the only ones that may
        // claim completion. `engine_down` is not one of them.
        const failure = apiFailure(err);
        // Two reasons a failed READ may not speak for the journey, and
        // `pollFailureSettles` holds both: the user's own submit is the writer of
        // record beside it, and a failure the ROUTE never named is not an answer
        // about the flow at all — that same read is what materializes a
        // just-succeeded one. Keep reading instead of stopping; the deadline check
        // at the top of each poll bounds either.
        if (!pollFailureSettles(submittingRef.current, failure?.serverNamed ?? false, failure?.code ?? failure?.detail)) {
          pollTimer = window.setTimeout(() => void poll(flowId), POLL_MS);
          return;
        }
        // The authority goes first because its answer is what decides whether these
        // pairs are the ones on screen — see `failureLanded`. The refetch below is
        // owed whatever it answers.
        const step = transition({ kind: 'error', errorKey: oauthFailureKey(failure?.code, journey) });
        rowsBehindAreStale(failure, failureLanded(step.action));
      }
    };

    // Clear any stale flow from a prior open so the previous success/failure
    // isn't shown while the new startOAuth request is in flight.
    transition({ kind: 'reset' });
    setCode('');
    setSubmitting(false);
    setAdoption(null);
    setRepair(null);
    setStranded([]);
    void (async () => {
      try {
        // Two ways in, one flow out. The reauth route opens the flow on the
        // existing source; a create opens a fresh one for the selected channel.
        const started = reauthId
          ? await modelsApi.reauthSource(reauthId)
          : await modelsApi.startOAuth(vendor, channel, clientNonce.current ?? (clientNonce.current = createClientNonce()));
        if (cancelled) {
          // The dialog closed while this request was in flight, so the cleanup
          // below found no flow to cancel — the flow id exists nowhere but here.
          // Dropping it would leave a live login running against a source whose
          // old sign-in the server has ALREADY invalidated. Hand it back where it
          // is known instead.
          //
          // Through `releaseFlow`, which decides whether this journey may still
          // cancel: by the time we resume, our own cleanup has already released
          // the ref, so a replacement dialog may own the source's flow — and on a
          // reauth FOR THE SAME ROW it would own THIS one, since `POST …/reauth`
          // hands such a successor the live pending flow. `oauth_start` never
          // does: it mints a fresh pending source id per call, so a create's flow
          // has no successor to protect and the released ref means nobody is
          // coming for it. Which is the difference `reusable` carries, because
          // ownership alone cannot: the two are indistinguishable at this line.
          // Whether the successor is the right ROW is the authority's subject to
          // say — see `flowLetGo`. The refetch happens either way, and only after
          // the call settles.
          await releaseFlow(authority, flowAuthorityRef.current, {
            cancel: () => modelsApi.cancelOAuth(started.flow_id),
            reusable: isReauth,
            // The close path refetches too, but while this request is still in
            // flight: that read can return the row as it was before
            // `mark_native_irreversible_start` committed, and nothing afterwards
            // would correct it. The abandoned journey is the one case where the
            // user is no longer looking at a dialog that could tell them — the
            // same reason, and the same owner, as every other exit of a request
            // that outlived its dialog.
            reread: resolvedAfterAttempt,
          });
          return;
        }
        // A reused pending flow can arrive already finished — in ANY terminal
        // state. Read its status instead of latching it (`startNeedsStatusRead`),
        // so the terminal lands through `settle`: `oauth_start` does not
        // materialize, and the status route is where a success grows its repair
        // tail and an unsuccessful hub reauth gets failed closed.
        // Cleanup can only cancel a flow it can NAME, and from this line it can: a
        // close landing during the status read below now finds an id where it used
        // to find a null. Earlier than this the abandoned-start branch above is the
        // owner, because cleanup has already run and already answered.
        openedFlowId = started.flow_id;
        heldFlowId.current = started.flow_id;
        if (startNeedsStatusRead(started)) {
          await poll(started.flow_id);
          return;
        }
        transition({ kind: 'response', flow: started });
        if (started.expires_at) deadline = new Date(started.expires_at).getTime() + 60_000;
        pollTimer = window.setTimeout(() => void poll(started.flow_id), POLL_MS);
      } catch (err) {
        const failure = apiFailure(err);
        // A reauth that fails to START can still have written the row: the
        // irreversible marking is rolled back only for a login that fails to
        // spawn, not for the flow-binding failures after it. Which is why this
        // rejection re-reads on BOTH sides of the guard — the abandoned case
        // through the one owner for it, since the close path's own read may have
        // been issued before this write landed.
        if (cancelled) {
          resolvedAfterAttempt();
          return;
        }
        // Nothing can have settled this view yet — the reset above is the last
        // thing that touched it, and both other arrivals need the flow this call
        // failed to produce. It asks anyway, because 「may these pairs speak?」 is
        // one rule for every arrival that carries them, and a site that answers it
        // by being sure of its position is a site that stops being right when the
        // position moves.
        const step = transition({
          kind: 'error',
          errorKey: isReauth ? 'settings.models.oauth.error.start' : oauthStartFailureKey(failure?.detail ?? failure?.code),
        });
        if (!isReauth) setStartFailureCode(failure?.detail ?? failure?.code ?? 'start_failed');
        rowsBehindAreStale(failure, failureLanded(step.action));
      }
    })();

    return () => {
      cancelled = true;
      stop();
      settleRef.current = null;
      if (successTimer.current !== null) window.clearTimeout(successTimer.current);
      transition({ kind: 'reset' });
      // Read ownership BEFORE releasing it. React runs this cleanup ahead of the
      // next effect body, so at this instant the ref is still ours whenever it is
      // this dialog re-running — which is why the same `releaseFlow` call answers
      // differently here than on the abandoned-start path above: that one only
      // gets to ask after this line has already run.
      const owner = flowAuthorityRef.current;
      if (owner === authority) flowAuthorityRef.current = null;
      // A cleanup cannot await, but the refetch inside `releaseFlow` still has to
      // wait for the cancel: this call can BE the write (`oauth_cancel` on a
      // terminal flow materializes it). Which is also why the id comes from the
      // REQUEST and not from the landed view. The view was the wrong place twice
      // over: a poll in flight can terminalize it between the read and the cancel
      // landing, so nothing may branch on it — and a start that came back already
      // terminal is never landed there at all, by design, so for that one the view
      // had no id to give. `cancel: null` then read as 「there is nothing to
      // cancel」 when it meant 「the view was not told」, and the flow the user
      // closed stayed un-materialized: still `not completed`, which is the only
      // thing `pending_reauth` filters on, so the next reauth of that same source
      // is handed the login the close was abandoning and commits it.
      // The re-read speaks for the rows and not for the pairs, by the default: it
      // is the latest-landing arrival in the file, because it awaits the cancel
      // first, and by then the attempt it belongs to is not merely settled but
      // GONE. Whatever gap report is on screen when it returns is somebody else's.
      const opened = openedFlowId;
      if (heldFlowId.current === opened) heldFlowId.current = null;
      rereadHeldFlow.current = null;
      void releaseFlow(authority, owner, {
        cancel: opened ? () => modelsApi.cancelOAuth(opened) : null,
        reusable: isReauth,
        reread: () => rowsBehindAreStale(),
      });
    };
  }, [open, phase, startAttempt, vendor, channel, reauthId, t, showToast, isReauth, rowsBehindAreStale, resolvedAfterAttempt, journey, createClientNonce]);

  // 1-second ticker so the paste-flow countdown updates.
  React.useEffect(() => {
    if (!open) return;
    const id = window.setInterval(() => tick(), 1000);
    return () => window.clearInterval(id);
  }, [open]);

  const submit = async () => {
    const authority = flowAuthorityRef.current;
    const cur = authority?.current().flow;
    if (!cur || !code.trim()) return;
    const isCurrent = () =>
      flowAuthorityRef.current === authority && authority.current().flow?.flow_id === cur.flow_id;
    setSubmitting(true);
    try {
      const result = await modelsApi.submitOAuth(cur.flow_id, code.trim());
      // Drop the response if the dialog closed or a new flow started meanwhile —
      // the response, not the re-read. This call is the write of record: it went
      // through `_materialize_completed_oauth` before answering, so by the time
      // there is no dialog left to update, the rows behind the one on screen have
      // already moved. The close path re-read while this was still in flight.
      if (!isCurrent()) {
        resolvedAfterAttempt();
        return;
      }
      // Submit can terminate the flow outright (the contract gives status and
      // submit the same terminal shape), so it goes through the same handler
      // rather than storing the flow and waiting for a poll to notice.
      settleRef.current?.(result);
    } catch (err) {
      // Same fact as the success above, through the same owner: a rejection is not
      // evidence that nothing was written. `_materialize_reauth` saves the source as
      // 需处理 with its models stripped and only THEN raises `discovery_failed`, so
      // an arrival with no dialog left to update still has rows to correct — and it
      // is the only thing that can, the close path having read while it was in
      // flight. Silent about the pairs by the default, because the report on screen
      // belongs to whichever attempt replaced this one.
      if (!isCurrent()) {
        resolvedAfterAttempt();
        return;
      }
      // Submit reaches the same materialization as the poll, so it can fail the
      // same way — including after the credential change has committed.
      //
      // `isCurrent()` is not enough on its own to make this arrival the failure of
      // record: a terminal poll settles the VIEW without touching the authority or
      // the flow id, so a submit rejecting afterwards is still current and still
      // ignored. `failureLanded` is the part that knows.
      const failure = apiFailure(err);
      const step = authority.transition({
        kind: 'error',
        errorKey: oauthFailureKey(failure?.code, journey),
      });
      rowsBehindAreStale(failure, failureLanded(step.action));
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  };

  const retryStart = async () => {
    if (startFailureCode === NATIVE_SUBSCRIPTION_SLOT_FAILURE && !isReauth) {
      // The start route checked the current store under its mutation lock, so
      // this error is newer and more authoritative than the page snapshot.
      setNativeSlotTaken(true);
      setChannel('hub');
      setStartFailureCode(null);
      setPhase('choose');
      return;
    }
    const timedOutFlow = view.errorKey === 'settings.models.oauth.error.timeout' ? heldFlowId.current : null;
    if (timedOutFlow && rereadHeldFlow.current) {
      if (await rereadHeldFlow.current()) return;
    }
    const freshAcquisition = startFailureCode === null;
    setStartFailureCode(null);
    if (freshAcquisition) clientNonce.current = null;
    setStartAttempt((attempt) => attempt + 1);
    setPhase('flow');
  };

  const { flow, errorKey } = view;
  const presentation = flow?.presentation;
  const expects = presentation?.expects;
  const isDevice = expects === 'none';
  const state = flow?.state;
  // A `success` state now means the Source exists: the server materializes it in
  // the same call, so there is no in-between to hold the banner for. The complete
  // rendered state comes from the authority's landed view.
  const success = view.settled && flow?.state === 'success';
  const failed = view.settled && Boolean(errorKey);
  const active = !view.settled;

  const remainingMs = flow?.expires_at ? Math.max(0, new Date(flow.expires_at).getTime() - Date.now()) : null;
  const mmss =
    remainingMs !== null
      ? `${String(Math.floor(remainingMs / 60000)).padStart(2, '0')}:${String(Math.floor((remainingMs % 60000) / 1000)).padStart(2, '0')}`
      : '';

  // `instructions_key` is runtime-declared (a new adapter can ship one this
  // bundle has never seen), so it falls back to the copy for the `expects` shape
  // instead of printing the key at the user.
  const step2Fallback = isDevice
    ? 'settings.models.oauth.deviceCode.hint'
    : expects === 'paste_callback_url'
      ? 'settings.models.oauth.callback.hint'
      : 'settings.models.oauth.pasteCode.hint';
  const step2Label = serverText(t, presentation?.instructions_key, step2Fallback) ?? '';

  React.useEffect(() => {
    const target = providerWindow.current;
    if (!target || !presentation?.auth_url || target.closed) return;
    try {
      target.location.href = presentation.auth_url;
    } catch {
      // A popup may become inaccessible after opening; the visible link remains
      // the fallback in that case.
    }
    providerWindow.current = null;
  }, [presentation?.auth_url]);

  const choosing = !isReauth && phase === 'choose';
  const vendorCopy = subscriptionVendorCopy(vendor);
  const vendorName = vendor === 'openai' ? 'ChatGPT' : 'Claude';
  const recommended = recommendedSubscriptionChannel(vendor);
  const optionOrder = subscriptionOptionOrder(vendor);
  const optionRefs = React.useRef<Partial<Record<SupplyChannel, HTMLButtonElement | null>>>({});
  const selectableChannels = CHANNELS.filter((candidate) => candidate !== 'native_cli' || !nativeSlotTaken);

  const moveSelection = (direction: number) => {
    const currentIndex = selectableChannels.indexOf(channel);
    const nextIndex =
      (Math.max(0, currentIndex) + direction + selectableChannels.length) % selectableChannels.length;
    const next = selectableChannels[nextIndex];
    setChannel(next);
    window.requestAnimationFrame(() => optionRefs.current[next]?.focus());
  };

  if (choosing) {
    return (
      <DialogPrimitive.Root open={open} onOpenChange={(value) => !value && onClose()}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="model-hub-add-sub-overlay fixed inset-0 z-50" />
          <DialogPrimitive.Content
            className="model-hub-add-sub-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col gap-0 overflow-y-auto border border-border-strong bg-surface p-0 shadow-xl outline-none"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              window.requestAnimationFrame(() => optionRefs.current[channel]?.focus());
            }}
          >
          <header className="model-hub-add-sub-head flex flex-col border-b border-border">
            <div className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title id="model-hub-add-sub-title" className="model-hub-add-sub-title font-bold">
                {t('settings.models.addSub.title', { vendor: vendorName })}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close asChild>
                <Button type="button" variant="ghost" size="icon" className="model-hub-add-sub-close" aria-label={t('settings.models.addSub.cancel')} title={t('settings.models.addSub.cancel')}>
                  <X aria-hidden />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-add-sub-subtitle text-muted">{t(`settings.models.addSub.subtitle.${vendorCopy}`)}</DialogPrimitive.Description>
          </header>

          <div className="model-hub-add-sub-body flex flex-col">
            <div
              role="radiogroup"
              aria-labelledby="model-hub-add-sub-title"
              className="model-hub-add-sub-options flex flex-col"
            >
              {optionOrder.map((candidate) => {
                const isNative = candidate === 'native_cli';
                const disabled = isNative && nativeSlotTaken;
                const selected = channel === candidate;
                const badgeKey = disabled
                  ? 'added'
                  : candidate === recommended
                    ? 'recommended'
                    : vendorCopy === 'claude'
                      ? 'secondary'
                      : 'supportedNotRecommended';
                const optionKey = isNative ? 'native' : 'hub';
                return (
                  <button
                    key={candidate}
                    ref={(node) => {
                      optionRefs.current[candidate] = node;
                    }}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    aria-disabled={disabled}
                    tabIndex={selected ? 0 : -1}
                    onClick={() => !disabled && setChannel(candidate)}
                    onKeyDown={(event) => {
                      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                        event.preventDefault();
                        moveSelection(1);
                      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                        event.preventDefault();
                        moveSelection(-1);
                      }
                    }}
                    className={cn(
                      'model-hub-add-sub-option flex items-start gap-3 text-left transition-colors',
                      selected
                        ? 'border-mint/35 bg-mint/[0.06]'
                        : 'model-hub-add-sub-option--idle border-border hover:border-border-strong',
                      disabled && 'cursor-not-allowed opacity-55 hover:border-border',
                    )}
                  >
                    {!disabled && (
                      <span
                        className={cn(
                          'mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border',
                          selected ? 'border-mint' : 'model-hub-border-33',
                        )}
                        aria-hidden
                      >
                        {selected && <span className="size-2 rounded-full bg-mint" />}
                      </span>
                    )}
                    <span className="model-hub-add-sub-option-copy min-w-0 flex flex-1 flex-col">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="model-hub-add-sub-option-label font-semibold text-foreground">
                          {t(`settings.models.addSub.opt.${optionKey}.label`)}
                        </span>
                        <span className="model-hub-accent-pill--mint model-hub-add-sub-badge rounded-full border font-semibold">
                          {t(`settings.models.addSub.${disabled ? 'opt' : 'badge'}.${badgeKey}`)}
                        </span>
                      </span>
                      <span className="model-hub-add-sub-description block text-muted">
                        {t(`settings.models.addSub.opt.${optionKey}.desc.${vendorCopy}`)}
                      </span>
                      {vendorCopy === 'claude' && candidate === 'hub' && (
                        <span className="model-hub-add-sub-risk flex items-start gap-2 border border-gold/30 bg-gold/10">
                          <TriangleAlert className="mt-0.5 size-3 shrink-0" />
                          <span>{t('settings.models.addSub.tos.claude')}</span>
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
            <p className="model-hub-add-sub-hint flex items-start gap-2 text-muted">
              <Info className="mt-0.5 size-3 shrink-0" />
              <span>{t(`settings.models.addSub.hint.${vendorCopy}`)}</span>
            </p>
          </div>

          <div className="model-hub-add-sub-foot model-hub-fill-05 flex items-center justify-end gap-2 border-t border-border">
            <Button variant="ghost" size="sm" className="model-hub-dialog-action" onClick={onClose}>
              {t('settings.models.addSub.cancel')}
            </Button>
            <Button
              variant="brand"
              size="sm"
              className="model-hub-dialog-action"
              onClick={() => {
                preopenProviderWindow();
                setPhase('flow');
              }}
            >
              {t('settings.models.addSub.signIn')}
              <ArrowRight className="size-3.5" />
            </Button>
          </div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    );
  }

  return (
    <>
      <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
        <DialogContent className="max-w-[520px] gap-5">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2.5 text-[17px] font-bold">
              <span className={cn('grid size-8 shrink-0 place-items-center rounded-lg', ACCENT_TILE[accent])}>
                <Sparkles className={cn('size-4', ACCENT_ICON[accent])} />
              </span>
              {isReauth
                ? t('settings.models.oauth.title.reauth', { name: reauth?.display_name ?? '' })
                : t(`settings.models.oauth.title.${vendor}`, {
                    defaultValue: t('settings.models.oauth.title.generic') as string,
                  })}
            </DialogTitle>
          </DialogHeader>

          {failed && (
            <div className="flex flex-col gap-2">
              <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                {/* errorKey may be the flow's own runtime-declared `error_key`, so
                    an unknown one degrades to 连接失败 rather than rendering itself. */}
                <span>{serverText(t, errorKey, 'settings.models.oauth.error.generic')}</span>
              </div>
              {isReauth && stranded.length > 0 && <>
              {/* Past tense (`gapsDone`), because this is not a confirm: the
                  credential change these pairs are the cost of has already
                  happened. Self-hides when the failure stranded nobody. */}
              <p className="text-[12px] font-semibold text-foreground">{t('settings.models.repair.gapsDone')}</p>
              <GuardGapList gaps={stranded} />
              </>}
            </div>
          )}

          {success && isReauth ? (
            // A repair reports on the source it repaired, not on a new connection:
            // 「已恢复可用」 when the login cleared the blocker, 「已更新」 when there
            // was nothing to clear, 「仍然不可用」 when the flow finished and the
            // source came back stopped anyway, and the stranded pairs when
            // something is still without a source. Only the last stays on screen.
            repair?.kind === 'gaps' ? (
              <div className="flex flex-col gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-3">
                <span className="model-hub-ink-gold text-[12.5px] font-semibold leading-relaxed">
                  {t('settings.models.repair.gapsDone')}
                </span>
                <GuardGapList gaps={repair.gaps} />
              </div>
            ) : repair?.kind === 'unresolved' ? (
              // Gold, not destructive, and not a green check: nothing failed —
              // the login completed and the source is still stopped (a native CLI
              // that reports itself signed out lands here). A 「已恢复可用」 over
              // that is the dead end §4.5 forbids; the row keeps its remedy and
              // this line is why it is still there.
              <div className="model-hub-ink-gold flex items-center gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-4 py-3 text-[13px] font-medium">
                <TriangleAlert className="size-4 shrink-0" />
                {t('settings.models.repair.unresolved')}
              </div>
            ) : (
              <div className="model-hub-ink-mint flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium">
                <CheckCircle2 className="size-4 shrink-0" />
                {repair
                  ? t(REPAIR_LINE_KEY[repair.kind])
                  : t('settings.models.oauth.connected')}
              </div>
            )
          ) : success ? (
            <div className="flex flex-col gap-2">
              <div className="model-hub-ink-mint flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium">
                <CheckCircle2 className="size-4 shrink-0" />
                {t('settings.models.oauth.connected')}
              </div>
              {/* Only when the response actually reported the creation: an absent
                  `adopted_by` is not an empty one, and 「没有 Agent 采用」 would be
                  a claim this response never made. The two halves come off ONE
                  value, so the note can never read a skip list from one arrival
                  against an adopter list from another. */}
              <AdoptionNote addedTo={adoption?.added_to ?? null} adoptedBy={adoption?.adopted_by ?? null} />
            </div>
          ) : (
            active && (
              <div className="flex flex-col gap-3">
                <Step
                  n={1}
                  label={
                    isDevice
                      ? (t('settings.models.oauth.step1.devicePage') as string)
                      : (t('settings.models.oauth.step1.authLink') as string)
                  }
                >
                  {presentation?.auth_url ? (
                    <OAuthLinkRow
                      url={presentation.auth_url}
                      onCopy={copy(presentation.auth_url)}
                      copyLabel={t('common.copy') as string}
                    />
                  ) : (
                    <p className="text-[12px] text-muted">{t('settings.models.oauth.starting')}</p>
                  )}
                </Step>

                <Step n={2} label={step2Label}>
                  {isDevice ? (
                    <OAuthDeviceCodeRow
                      code={presentation?.device_code ?? ''}
                      onCopy={copy(presentation?.device_code)}
                      copyLabel={t('common.copy') as string}
                    />
                  ) : (
                    <OAuthSubmitRow
                      value={code}
                      onChange={setCode}
                      onSubmit={() => void submit()}
                      submitting={submitting || state === 'verifying'}
                      placeholder={
                        expects === 'paste_callback_url' ? 'http://127.0.0.1:.../callback?code=…' : 'ac_…#st_…'
                      }
                      submitLabel={t('common.submit') as string}
                      submittingLabel={t('common.submitting') as string}
                    />
                  )}
                </Step>

              </div>
            )
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
            {active ? (
              <span className="flex items-center gap-2 text-[12px] text-muted">
                <span className="size-2 shrink-0 rounded-full bg-gold" aria-hidden />
                {state === 'verifying'
                  ? t('settings.models.oauth.status.verifying')
                  : isDevice
                    ? t('settings.models.oauth.status.awaitingDevice')
                    : t('settings.models.oauth.status.awaitingPaste', { time: mmss })}
              </span>
            ) : (
              <span />
            )}
            <div className="flex items-center gap-2">
              {failed && !isReauth && (
                <Button
                  variant="brand"
                  size="sm"
                  className="h-10 sm:h-9"
                  onClick={() => {
                    preopenProviderWindow();
                    void retryStart();
                  }}
                >
                  {t('settings.models.addSub.retry')}
                </Button>
              )}
              <Button variant={active ? 'ghost' : 'outline'} size="sm" className="h-10 sm:h-9" onClick={onClose}>
                {active ? t('common.cancel') : t('common.close')}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

    </>
  );
};
