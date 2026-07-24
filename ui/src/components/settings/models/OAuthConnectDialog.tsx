// 连接订阅 dialog (frame 09). RENDERS DECLARATIVELY from the runtime-declared
// oauth-flow presentation (S1 gap ③): `expects` ∈ none | paste_code |
// paste_callback_url selects the step-2 control; there is NO vendor→form table
// in the UI. Composes the shared OAuth atoms (OAuthLinkRow / OAuthDeviceCodeRow
// / OAuthSubmitRow) so it matches the Backends OAuth panel. State machine
// mirrors BackendOAuthPanel: start → 2s poll → verifying → success, 15-min
// timeout, cancel.
import * as React from 'react';
import { CheckCircle2, Loader2, Sparkles, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { OAuthDeviceCodeRow, OAuthLinkRow, OAuthSubmitRow } from '../oauth/OAuthFlowParts';
import { ExperimentalConsentDialog } from './ExperimentalConsentDialog';
import { SUBSCRIPTION_HUB_EXPERIMENTAL } from './featureFlags';
import { modelsApi } from './modelsApi';
import { ACCENT_ICON, ACCENT_TILE } from './vendorMeta';
import type { OAuthFlow, SupplyChannel } from './types';

const POLL_MS = 2000;
const DEADLINE_MS = 16 * 60 * 1000;
const TERMINAL = ['success', 'failed', 'cancelled'];

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
  // Between flow success and the Source being persisted (createOAuthSource):
  // holds the "Connected" banner so we never claim success before the Source
  // actually exists, and lets a finalize failure surface honestly.
  const [finalizing, setFinalizing] = React.useState(false);
  const [, tick] = React.useReducer((x) => x + 1, 0);

  // One-shot latch so the finalize handoff runs exactly once per flow.
  const finalizedRef = React.useRef(false);
  const flowRef = React.useRef<OAuthFlow | null>(null);
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
    const apply = (f: OAuthFlow | null) => {
      flowRef.current = f;
      setFlow(f);
    };

    const poll = async (flowId: string) => {
      if (cancelled) return;
      if (Date.now() > deadline) {
        setErrorKey('settings.models.oauth.error.timeout');
        if (flowRef.current) apply({ ...flowRef.current, state: 'failed' });
        return;
      }
      try {
        const next = await modelsApi.getOAuthStatus(flowId);
        if (cancelled) return;
        apply(next);
        if (next.state === 'success') {
          // P0: a completed flow is NOT yet a Source. Finalize the handoff
          // (POST /sources with oauth_flow_ref) — the server assigns the source
          // id from the flow binding and discovers models. Only then is the
          // connect truly done. `experimental_consent` goes only to the hub
          // channel; native_cli must not send it (server rejects otherwise).
          if (finalizedRef.current) return;
          finalizedRef.current = true;
          setFinalizing(true);
          try {
            await modelsApi.createOAuthSource({
              kind: 'subscription',
              vendor,
              oauth_flow_ref: next.flow_id,
              supply_channel: next.channel,
              ...(next.channel === 'hub' ? { experimental_consent: true } : {}),
            });
          } catch (err) {
            // OAuth succeeded but the Source wasn't persisted — say so, don't
            // flash a false "Connected". The Source may exist server-side if the
            // request landed, so a refetch still runs on the honest paths below.
            if (cancelled) return;
            const code = (err as { code?: string } | null)?.code;
            setErrorKey(
              code === 'consent_required'
                ? 'settings.models.oauth.error.consent'
                : 'settings.models.oauth.error.finalize',
            );
            apply({ ...next, state: 'failed' });
            setFinalizing(false);
            return;
          }
          if (cancelled) return;
          setFinalizing(false);
          showToast(t('settings.models.oauth.status.success') as string, 'success');
          onConnectedRef.current();
          successTimer.current = window.setTimeout(() => onCloseRef.current(), 1400);
          return;
        }
        if (next.state === 'failed' || next.state === 'cancelled') {
          setErrorKey(next.error_key ?? 'settings.models.oauth.error.generic');
          return;
        }
        pollTimer = window.setTimeout(() => void poll(flowId), POLL_MS);
      } catch {
        if (!cancelled) setErrorKey('settings.models.oauth.error.generic');
      }
    };

    // Clear any stale flow from a prior open so the previous success/failure
    // isn't shown while the new startOAuth request is in flight.
    apply(null);
    setErrorKey(null);
    setCode('');
    setSubmitting(false);
    setFinalizing(false);
    finalizedRef.current = false;
    void (async () => {
      try {
        // A hub-held subscription connect (channel === 'hub' only when the user
        // has confirmed the experimental consent below) must carry consent, or
        // the server returns consent_required.
        const started = await modelsApi.startOAuth(vendor, channel, channel === 'hub');
        if (cancelled) return;
        apply(started);
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
      if (successTimer.current !== null) window.clearTimeout(successTimer.current);
      const cur = flowRef.current;
      if (cur && !TERMINAL.includes(cur.state)) modelsApi.cancelOAuth(cur.flow_id).catch(() => {});
      flowRef.current = null;
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
    const cur = flowRef.current;
    if (!cur || !code.trim()) return;
    setSubmitting(true);
    try {
      const next = await modelsApi.submitOAuth(cur.flow_id, code.trim());
      // Drop the response if the dialog closed or a new flow started meanwhile.
      if (flowRef.current?.flow_id !== cur.flow_id) return;
      flowRef.current = next;
      setFlow(next);
    } catch {
      if (flowRef.current?.flow_id !== cur.flow_id) return;
      setErrorKey('settings.models.oauth.error.generic');
    } finally {
      if (flowRef.current?.flow_id === cur.flow_id) setSubmitting(false);
    }
  };

  const presentation = flow?.presentation;
  const expects = presentation?.expects;
  const isDevice = expects === 'none';
  const state = flow?.state;
  // "Connected" only once the Source is persisted — never during finalize, and
  // never if the finalize handoff failed.
  const success = state === 'success' && !finalizing && !errorKey;
  const failed = state === 'failed' || state === 'cancelled' || Boolean(errorKey);
  const active = !success && !failed && !finalizing;

  const remainingMs = flow?.expires_at ? Math.max(0, new Date(flow.expires_at).getTime() - Date.now()) : null;
  const mmss =
    remainingMs !== null
      ? `${String(Math.floor(remainingMs / 60000)).padStart(2, '0')}:${String(Math.floor((remainingMs % 60000) / 1000)).padStart(2, '0')}`
      : '';

  const step2Label = presentation?.instructions_key
    ? (t(presentation.instructions_key) as string)
    : isDevice
      ? (t('settings.models.oauth.deviceCode.hint') as string)
      : expects === 'paste_callback_url'
        ? (t('settings.models.oauth.callback.hint') as string)
        : (t('settings.models.oauth.pasteCode.hint') as string);

  return (
    <>
      <Dialog open={open} onOpenChange={(v) => !v && !finalizing && onClose()}>
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
              <span>{t(errorKey ?? 'settings.models.oauth.error.generic')}</span>
            </div>
          )}

          {success ? (
            <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
              <CheckCircle2 className="size-4 shrink-0" />
              {t('settings.models.oauth.connected')}
            </div>
          ) : finalizing ? (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2/40 px-4 py-3 text-[13px] font-medium text-muted">
              <Loader2 className="size-4 shrink-0 animate-spin" />
              {t('settings.models.oauth.status.finalizing')}
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
            <Button variant={active ? 'ghost' : 'outline'} size="sm" onClick={onClose} disabled={finalizing}>
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
