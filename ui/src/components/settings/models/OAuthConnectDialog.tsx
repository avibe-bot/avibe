// 连接订阅 dialog (frame 09). RENDERS DECLARATIVELY from the runtime-declared
// oauth-flow presentation (S1 gap ③): `expects` ∈ none | paste_code |
// paste_callback_url selects the step-2 control; there is NO vendor→form table
// in the UI. Composes the shared OAuth atoms (OAuthLinkRow / OAuthDeviceCodeRow
// / OAuthSubmitRow) so it matches the Backends OAuth panel. State machine
// mirrors BackendOAuthPanel: start → 2s poll → verifying → success, 15-min
// timeout, cancel.
import * as React from 'react';
import { CheckCircle2, Sparkles, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { OAuthDeviceCodeRow, OAuthLinkRow, OAuthSubmitRow } from '../oauth/OAuthFlowParts';
import { AdoptionNote } from './AdoptionNote';
import { createFlowAuthority, isDone, type FlowAuthority } from './asyncLifetime';
import { ExperimentalConsentDialog } from './ExperimentalConsentDialog';
import { SUBSCRIPTION_HUB_EXPERIMENTAL } from './featureFlags';
import { modelsApi, type OAuthResult } from './modelsApi';
import { serverText } from './serverCopy';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';
import type { AdoptedBy, OAuthFlow, SupplyChannel } from './types';

const POLL_MS = 2000;
const DEADLINE_MS = 16 * 60 * 1000;
const TERMINAL = ['success', 'failed', 'cancelled'];

// The creation half of a terminal status/submit response fails with its own
// codes (api.md → POST /sources errors, raised while materializing the Source).
const CREATION_CODES = ['discovery_failed', 'engine_down', 'migration_item_conflict'];

/** Terminal-response failure code → copy, keeping 「授权没成」 distinct from
 *  「授权成了但来源没建起来」 instead of collapsing both into 连接失败. */
const errorKeyFor = (code?: string): string =>
  code === 'consent_required'
    ? 'settings.models.oauth.error.consent'
    : code && CREATION_CODES.includes(code)
      ? 'settings.models.oauth.error.finalize'
      : 'settings.models.oauth.error.generic';

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
  onClose: () => void;
  onConnected: () => void;
}> = ({ open, vendor, onClose, onConnected }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [flow, setFlow] = React.useState<OAuthFlow | null>(null);
  const [code, setCode] = React.useState('');
  const [submitting, setSubmitting] = React.useState(false);
  const [errorKey, setErrorKey] = React.useState<string | null>(null);
  const [channel, setChannel] = React.useState<SupplyChannel>('native_cli');
  const [consentOpen, setConsentOpen] = React.useState(false);
  // Which Agents took the new subscription in, frozen at commit (api.md). Same
  // note as the API-key dialog: connecting a credential is not the same as
  // putting it into service, and a `custom` Agent is silently absent.
  //
  // `null` means the terminal response did not report a creation — which is not
  // 「没有 Agent 采用」 and must not be rendered as it.
  const [adoptedBy, setAdoptedBy] = React.useState<AdoptedBy[] | null>(null);
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
    const authority = createFlowAuthority((view) => setFlow(view.flow));
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
      if (step.action === 'succeed') {
        // The Source already exists. The status/submit call that first reports
        // success materializes it server-side and consumes the flow binding doing
        // it — there is nothing left to finalize, and a POST /sources afterwards
        // is refused as `flow_not_found` on a connect that in fact succeeded.
        setAdoptedBy(created ? created.adopted_by : null);
        showToast(t('settings.models.oauth.status.success') as string, 'success');
        onConnectedRef.current();
        // Same rule as the API-key dialog: 1.4s auto-dismiss is for a pure
        // 「连接成功」. When no Agent adopted the subscription the banner carries an
        // instruction, so the dialog waits to be closed. An unreported creation
        // says nothing about adoption, so it auto-dismisses like a plain success.
        if (created?.adopted_by.length !== 0) successTimer.current = window.setTimeout(() => onCloseRef.current(), 1400);
      } else if (step.action === 'fail') {
        setErrorKey(result.flow.error_key ?? 'settings.models.oauth.error.generic');
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
      if (isDone(overdue.action)) {
        if (overdue.action === 'timeout') {
          setErrorKey('settings.models.oauth.error.timeout');
        }
        return;
      }
      try {
        const result = await modelsApi.getOAuthStatus(flowId);
        if (cancelled) return;
        if (settle(result)) return;
        pollTimer = window.setTimeout(() => void poll(flowId), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        // A poll that lands on a just-succeeded flow is also the call that
        // materializes the Source, so it can fail for creation reasons
        // (consent_required / discovery_failed / engine_down): the vendor said
        // yes and the Source still doesn't exist. Naming that separately is the
        // difference between 「重试授权」 and 「授权成功但没能建立来源」.
        setErrorKey(errorKeyFor((err as { code?: string } | null)?.code));
      }
    };

    // Clear any stale flow from a prior open so the previous success/failure
    // isn't shown while the new startOAuth request is in flight.
    transition({ kind: 'reset' });
    setErrorKey(null);
    setCode('');
    setSubmitting(false);
    setAdoptedBy(null);
    void (async () => {
      try {
        // A hub-held subscription connect (channel === 'hub' only when the user
        // has confirmed the experimental consent below) must carry consent, or
        // the server returns consent_required.
        const started = await modelsApi.startOAuth(vendor, channel, channel === 'hub');
        if (cancelled) return;
        transition({ kind: 'response', flow: started });
        if (started.expires_at) deadline = new Date(started.expires_at).getTime() + 60_000;
        pollTimer = window.setTimeout(() => void poll(started.flow_id), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        const code = (err as { code?: string } | null)?.code;
        setErrorKey(code === 'consent_required' ? 'settings.models.oauth.error.consent' : 'settings.models.oauth.error.start');
      }
    })();

    return () => {
      cancelled = true;
      stop();
      settleRef.current = null;
      if (successTimer.current !== null) window.clearTimeout(successTimer.current);
      const cur = authority.current().flow;
      transition({ kind: 'reset' });
      if (flowAuthorityRef.current === authority) flowAuthorityRef.current = null;
      if (cur && !TERMINAL.includes(cur.state)) modelsApi.cancelOAuth(cur.flow_id).catch(() => {});
    };
  }, [open, vendor, channel, t, showToast]);

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
      setErrorKey(errorKeyFor((err as { code?: string } | null)?.code));
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  };

  const presentation = flow?.presentation;
  const expects = presentation?.expects;
  const isDevice = expects === 'none';
  const state = flow?.state;
  // A `success` state now means the Source exists: the server materializes it in
  // the same call, so there is no in-between to hold the banner for. An errorKey
  // still wins — a terminal response can fail while creating the Source.
  const success = state === 'success' && !errorKey;
  const failed = state === 'failed' || state === 'cancelled' || Boolean(errorKey);
  const active = !success && !failed;

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
              {t(`settings.models.oauth.title.${vendor}`, {
                defaultValue: t('settings.models.oauth.title.generic') as string,
              })}
            </DialogTitle>
          </DialogHeader>

          {failed && (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              {/* errorKey may be the flow's own runtime-declared `error_key`, so
                  an unknown one degrades to 连接失败 rather than rendering itself. */}
              <span>{serverText(t, errorKey, 'settings.models.oauth.error.generic')}</span>
            </div>
          )}

          {success ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
                <CheckCircle2 className="size-4 shrink-0" />
                {t('settings.models.oauth.connected')}
              </div>
              {/* Only when the response actually reported the creation: an absent
                  `adopted_by` is not an empty one, and 「没有 Agent 采用」 would be
                  a claim this response never made. */}
              {adoptedBy && <AdoptionNote adoptedBy={adoptedBy} />}
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

                {SUBSCRIPTION_HUB_EXPERIMENTAL && (
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
