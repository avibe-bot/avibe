// 连接订阅 dialog (frame 09). RENDERS DECLARATIVELY from the runtime-declared
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
import { CheckCircle2, Sparkles, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { OAuthDeviceCodeRow, OAuthLinkRow, OAuthSubmitRow } from '../oauth/OAuthFlowParts';
import { AdoptionNote } from './AdoptionNote';
import {
  createFlowAuthority,
  initialFlowView,
  isDone,
  releaseFlow,
  startNeedsStatusRead,
  type FlowAuthority,
  type FlowView,
} from './asyncLifetime';
import { ExperimentalConsentDialog } from './ExperimentalConsentDialog';
import { SUBSCRIPTION_HUB_EXPERIMENTAL } from './featureFlags';
import { apiFailure, modelsApi, type OAuthResult } from './modelsApi';
import { REPAIR_LINE_KEY, REPAIR_TOAST, repairOutcome, repairSettles, type RepairOutcome } from './repair';
import { oauthFailureKey, serverText, type OAuthJourney } from './serverCopy';
import { adoptionVerdict } from './sufficiency';
import { SupplyGapNote } from './SupplyGapNote';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';
import type { AdoptedBy, Source, SupplyChannel, SupplyGap } from './types';

const POLL_MS = 2000;
const DEADLINE_MS = 16 * 60 * 1000;

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
  onClose: () => void;
  onConnected: () => void;
}> = ({ open, vendor, reauth = null, onClose, onConnected }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [view, setView] = React.useState<FlowView>(initialFlowView);
  const [code, setCode] = React.useState('');
  const [submitting, setSubmitting] = React.useState(false);
  const [channel, setChannel] = React.useState<SupplyChannel>('native_cli');
  const [consentOpen, setConsentOpen] = React.useState(false);
  // Which Agents took the new subscription in, frozen at commit (api.md). Same
  // note as the API-key dialog: connecting a credential is not the same as
  // putting it into service, and a `custom` Agent is silently absent.
  //
  // `null` means the terminal response did not report a creation — which is not
  // 「没有 Agent 采用」 and must not be rendered as it.
  const [adoptedBy, setAdoptedBy] = React.useState<AdoptedBy[] | null>(null);
  // The reauth counterpart of `adoptedBy`, read through the same owner the key
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
  const onConnectedRef = React.useRef(onConnected);
  onConnectedRef.current = onConnected;
  const onCloseRef = React.useRef(onClose);
  onCloseRef.current = onClose;

  const accent = vendor === 'openai' ? 'gold' : 'mint';
  // One derivation for 「which journey is this」, read by the start call, the
  // terminal handler and the title alike.
  const reauthId = reauth?.id ?? null;
  const isReauth = reauthId !== null;
  // The same derivation as a value, because the failure copy needs it as one: a
  // terminal code means 「couldn't create it」 on a connect and 「it's still broken」
  // on a repair, and `oauthFailureKey` takes the journey rather than guessing.
  const journey: OAuthJourney = isReauth ? 'reauth' : 'connect';

  /**
   * One owner for 「this reauth already reached the server」, reached from EVERY
   * path that ends the journey without a terminal success.
   *
   * The question at each call site is deliberately that one, not 「did it fail?」.
   * A reauth is not a no-op the way a connect is: the row is rewritten as the flow
   * starts — `mark_native_irreversible_start` writes 需要处理 across that vendor's
   * native sources and rolls it back only when the login fails to SPAWN — and
   * `_materialize_reauth` clears the source and marks it unavailable before
   * answering `discovery_failed`. So the list the page is showing is stale from
   * the start call onward, whether the journey then failed, or was simply
   * abandoned. Asking 「did it fail?」 is what left the abandoned case behind.
   *
   * The pairs travel on the error for the same reason `adopted_by` travels with a
   * creation: no later read of `/agents` reproduces them. They name who the
   * irreversible half left without a source, which is the one thing the user
   * cannot find out anywhere else on the page. A path with no error carries none,
   * and passing nothing is how it says so.
   */
  const reauthLeftRowsStale = (failure?: ReturnType<typeof apiFailure>) => {
    if (!isReauth) return;
    setStranded(failure?.interrupted ?? []);
    onConnectedRef.current();
  };

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

  // Drive the flow while the dialog is open. Re-runs when the target channel
  // changes (experimental hub opt-in restarts the flow).
  React.useEffect(() => {
    if (!open) return;
    let cancelled = false;
    let pollTimer: number | null = null;
    let deadline = Date.now() + DEADLINE_MS;

    const stop = () => {
      if (pollTimer !== null) window.clearTimeout(pollTimer);
      pollTimer = null;
    };
    const authority = createFlowAuthority(setView);
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
        onConnectedRef.current();
        // Line AND tone from the verdict's own owner (`REPAIR_TOAST`), because
        // choosing them separately here is what put a green 「连接成功」 over a gap
        // report. An absent tail is the only arrival with no verdict to speak for
        // it, and 「连接成功」 is what it has always said.
        const toast = verdict ? REPAIR_TOAST[verdict.kind] : null;
        showToast(
          t(toast?.key ?? 'settings.models.oauth.status.success') as string,
          toast?.tone ?? 'success',
        );
        // Unlike the adoption auto-close below this one is PROVABLE: 「nothing was
        // stranded」 is a field the server sends, so a clean repair may dismiss
        // itself while a gap report stays on screen to be read. An absent tail
        // says nothing to read either, so it closes too.
        if (!verdict || repairSettles(verdict))
          successTimer.current = window.setTimeout(() => onCloseRef.current(), 1400);
      } else if (step.action === 'succeed') {
        // The Source already exists. The status/submit call that first reports
        // success materializes it server-side and consumes the flow binding doing
        // it — there is nothing left to finalize, and a POST /sources afterwards
        // is refused as `flow_not_found` on a connect that in fact succeeded.
        setAdoptedBy(created ? created.adopted_by : null);
        showToast(t('settings.models.oauth.status.success') as string, 'success');
        onConnectedRef.current();
        // Same rule as the API-key dialog, through the same owner: 1.4s auto-dismiss
        // is for a pure 「连接成功」, and every other verdict leaves an instruction on
        // screen that 1.4s is not long enough to read. The old `!== 0` also read an
        // ABSENT creation as adopted, which auto-dismissed the one case that knows
        // least — `adoptionVerdict(null)` is indeterminate, so it now waits.
        if (adoptionVerdict(created?.adopted_by ?? null).kind === 'covered')
          successTimer.current = window.setTimeout(() => onCloseRef.current(), 1400);
      }
      // A paste submit can terminate the flow while a poll timer is still armed;
      // the guard above already makes that poll harmless, but there is no reason
      // to let it fire.
      if (isDone(step.action)) stop();
      return isDone(step.action);
    };
    settleRef.current = settle;

    const poll = async (flowId: string) => {
      if (cancelled) return;
      const overdue = transition({ kind: 'tick', overdue: Date.now() > deadline });
      if (isDone(overdue.action)) return;
      try {
        const result = await modelsApi.getOAuthStatus(flowId);
        if (cancelled) return;
        if (settle(result)) return;
        pollTimer = window.setTimeout(() => void poll(flowId), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        // A poll that lands on a just-succeeded flow is also the call that
        // materializes the outcome, so it can fail for reasons that have nothing
        // to do with the authorization (consent_required / discovery_failed /
        // engine_down): the vendor said yes and what came after it broke. Naming
        // that separately is the difference between 「重试授权」 and 「授权成功，
        // 后面没成」 — and WHICH object it broke is the journey's to say.
        const failure = apiFailure(err);
        reauthLeftRowsStale(failure);
        transition({ kind: 'error', errorKey: oauthFailureKey(failure?.code, journey) });
      }
    };

    // Clear any stale flow from a prior open so the previous success/failure
    // isn't shown while the new startOAuth request is in flight.
    transition({ kind: 'reset' });
    setCode('');
    setSubmitting(false);
    setAdoptedBy(null);
    setRepair(null);
    setStranded([]);
    void (async () => {
      try {
        // Two ways in, one flow out. The reauth route opens the flow ON the
        // existing source and acknowledges the irreversibility server-side (the
        // page has already asked); a create opens a fresh one, and a hub-held
        // connect (channel === 'hub' only after the experimental consent below)
        // must carry consent or the server returns consent_required.
        const started = reauthId
          ? await modelsApi.reauthSource(reauthId)
          : await modelsApi.startOAuth(vendor, channel, channel === 'hub');
        if (cancelled) {
          // The dialog closed while this request was in flight, so the cleanup
          // below found no flow to cancel — the flow id exists nowhere but here.
          // Dropping it would leave a live login running against a source whose
          // old sign-in the server has ALREADY invalidated. Hand it back where it
          // is known instead.
          //
          // Through `releaseFlow`, which decides whether this journey may still
          // cancel: by the time we resume, our own cleanup has already released
          // the ref, so a replacement dialog may own the source's flow — and it
          // would own THIS one, since a start reuses a live pending flow. The
          // refetch happens either way, and only after the call settles.
          await releaseFlow(authority, flowAuthorityRef.current, {
            cancel: () => modelsApi.cancelOAuth(started.flow_id),
            // The close path refetches too, but while this request is still in
            // flight: that read can return the row as it was before
            // `mark_native_irreversible_start` committed, and nothing afterwards
            // would correct it. The abandoned journey is the one case where the
            // user is no longer looking at a dialog that could tell them.
            reread: () => reauthLeftRowsStale(),
          });
          return;
        }
        // A reused pending flow can arrive already finished. Read its status
        // instead of latching it (`startNeedsStatusRead`), so the terminal lands
        // through `settle` with the repair tail the start envelope does not carry.
        if (startNeedsStatusRead(started)) {
          await poll(started.flow_id);
          return;
        }
        transition({ kind: 'response', flow: started });
        if (started.expires_at) deadline = new Date(started.expires_at).getTime() + 60_000;
        pollTimer = window.setTimeout(() => void poll(started.flow_id), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        const failure = apiFailure(err);
        // A reauth that fails to START can still have written the row: the
        // irreversible marking is rolled back only for a login that fails to
        // spawn, not for the flow-binding failures after it.
        reauthLeftRowsStale(failure);
        const code = failure?.code;
        transition({
          kind: 'error',
          errorKey:
            code === 'consent_required'
              ? 'settings.models.oauth.error.consent'
              : 'settings.models.oauth.error.start',
        });
      }
    })();

    return () => {
      cancelled = true;
      stop();
      settleRef.current = null;
      if (successTimer.current !== null) window.clearTimeout(successTimer.current);
      const cur = authority.current().flow;
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
      // terminal flow materializes it), and the only state to branch on here is
      // `cur` — the last POLLED snapshot, which a poll in flight can terminalize
      // between that read and the cancel landing. So nothing branches on it; it
      // supplies the flow id and nothing else.
      void releaseFlow(authority, owner, {
        cancel: cur ? () => modelsApi.cancelOAuth(cur.flow_id) : null,
        reread: () => reauthLeftRowsStale(),
      });
    };
  }, [open, vendor, channel, reauthId, t, showToast]);

  // 1-second ticker so the paste-flow countdown updates.
  React.useEffect(() => {
    if (!open) return;
    const id = window.setInterval(() => tick(), 1000);
    return () => window.clearInterval(id);
  }, [open]);

  // Consent is per-attempt: reset the experimental hub opt-in when the dialog
  // CLOSES, so the next open's start effect always begins from native_cli.
  // (Resetting on open would run after the start effect and briefly launch a
  // stale hub flow before the reset lands.)
  React.useEffect(() => {
    if (!open) setChannel('native_cli');
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
      // Drop the response if the dialog closed or a new flow started meanwhile.
      if (!isCurrent()) return;
      // Submit can terminate the flow outright (the contract gives status and
      // submit the same terminal shape), so it goes through the same handler
      // rather than storing the flow and waiting for a poll to notice.
      settleRef.current?.(result);
    } catch (err) {
      if (!isCurrent()) return;
      // Submit reaches the same materialization as the poll, so it can fail the
      // same way — including after the credential change has committed.
      const failure = apiFailure(err);
      reauthLeftRowsStale(failure);
      authority.transition({ kind: 'error', errorKey: oauthFailureKey(failure?.code, journey) });
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
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
              {/* Past tense (`gapsDone`), because this is not a confirm: the
                  credential change these pairs are the cost of has already
                  happened. Self-hides when the failure stranded nobody. */}
              <SupplyGapNote gaps={stranded} title={t('settings.models.repair.gapsDone') as string} />
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
                <span className="text-[12.5px] font-semibold leading-relaxed text-gold">
                  {t('settings.models.repair.gapsDone')}
                </span>
                <SupplyGapNote gaps={repair.gaps} />
              </div>
            ) : repair?.kind === 'unresolved' ? (
              // Gold, not destructive, and not a green check: nothing failed —
              // the login completed and the source is still stopped (a native CLI
              // that reports itself signed out lands here). A 「已恢复可用」 over
              // that is the dead end §4.5 forbids; the row keeps its remedy and
              // this line is why it is still there.
              <div className="flex items-center gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-4 py-3 text-[13px] font-medium text-gold">
                <TriangleAlert className="size-4 shrink-0" />
                {t('settings.models.repair.unresolved')}
              </div>
            ) : (
              <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
                <CheckCircle2 className="size-4 shrink-0" />
                {repair
                  ? t(REPAIR_LINE_KEY[repair.kind])
                  : t('settings.models.oauth.connected')}
              </div>
            )
          ) : success ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
                <CheckCircle2 className="size-4 shrink-0" />
                {t('settings.models.oauth.connected')}
              </div>
              {/* Only when the response actually reported the creation: an absent
                  `adopted_by` is not an empty one, and 「没有 Agent 采用」 would be
                  a claim this response never made. */}
              <AdoptionNote adoptedBy={adoptedBy} />
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

                {/* Withheld on a re-auth: where a subscription is HELD is a
                    property of the existing source, and this flow is signing back
                    into it — offering to move it here would be a different
                    operation wearing this one's clothes. */}
                {SUBSCRIPTION_HUB_EXPERIMENTAL && !isReauth && (
                  <button
                    type="button"
                    onClick={() => (channel === 'hub' ? setChannel('native_cli') : setConsentOpen(true))}
                    className={cn(
                      'flex items-center justify-between gap-3 rounded-lg border px-4 py-2.5 text-left text-[12px] transition-colors',
                      channel === 'hub'
                        ? 'border-gold/40 bg-gold/[0.06]'
                        : 'border-border bg-background hover:border-border-strong',
                    )}
                  >
                    <span className="flex flex-col gap-0.5">
                      <span className="font-medium text-foreground">{t('settings.models.oauth.hubOption.title')}</span>
                      <span className="text-muted">{t('settings.models.oauth.hubOption.subtitle')}</span>
                    </span>
                    <span
                      className={cn(
                        'shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold',
                        channel === 'hub' ? 'bg-gold/20 text-gold' : 'bg-surface-2 text-muted',
                      )}
                    >
                      {channel === 'hub' ? t('settings.models.oauth.hubOption.on') : t('settings.models.oauth.hubOption.off')}
                    </span>
                  </button>
                )}
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
            <Button variant={active ? 'ghost' : 'outline'} size="sm" className="h-10 sm:h-9" onClick={onClose}>
              {active ? t('common.cancel') : t('common.close')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <ExperimentalConsentDialog
        open={consentOpen}
        onConsent={() => {
          setConsentOpen(false);
          setChannel('hub');
        }}
        onCancel={() => setConsentOpen(false)}
      />
    </>
  );
};
