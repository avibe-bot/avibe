import React, { useEffect, useState } from 'react';
import { CheckCircle2, Cloud, ExternalLink, Link2, RefreshCcw } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import { useApi } from '../context/ApiContext';
import { useToast } from '../context/ToastContext';
import { CompactField } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { Badge } from './ui/badge';

const VIBE_CLOUD_URL = 'https://avibe.bot';
const VIBE_CLOUD_APP_URL = 'https://avibe.bot/app';

export const RemoteAccess: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [loading, setLoading] = useState(true);
  const [pairing, setPairing] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const [pairingKey, setPairingKey] = useState('');
  const [reconfiguring, setReconfiguring] = useState(false);
  const [actionMessage, setActionMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const describeError = (payload: any) => {
    const code = typeof payload?.error === 'string' ? payload.error : '';
    if (!code) {
      return t('errors.remote_access_unknown');
    }
    return t(`errors.${code}`, { defaultValue: t('errors.remote_access_unknown') });
  };

  const refresh = async () => {
    setLoading(true);
    try {
      const remoteStatus = await api.remoteAccessStatus();
      setStatus(remoteStatus);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh().catch(() => setLoading(false));
  }, []);

  const pair = async () => {
    setPairing(true);
    setActionMessage(null);
    try {
      const result = await api.pairVibeCloudRemoteAccess({
        backend_url: VIBE_CLOUD_URL,
        pairing_key: pairingKey.trim(),
        device_name: 'avibe',
      });
      setStatus(result);
      setPairingKey('');
      if (result?.start?.ok === false) {
        const message = describeError(result.start);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
      } else {
        const message = t('remoteAccess.pairSuccess');
        setReconfiguring(false);
        setActionMessage({ type: 'success', text: message });
        showToast(message, 'success');
      }
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    } finally {
      setPairing(false);
    }
  };

  const stop = async () => {
    setActionMessage(null);
    try {
      const result = await api.stopRemoteAccess();
      setStatus(result);
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      const message = t('remoteAccess.stopSuccess');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    }
  };

  const start = async () => {
    setActionMessage(null);
    try {
      const result = await api.startRemoteAccess();
      setStatus(result);
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      const message = t('remoteAccess.startSuccess');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    }
  };

  const publicUrl = status?.public_url;
  const paired = Boolean(status?.paired);
  const running = Boolean(status?.running);
  const showPairingForm = !paired || reconfiguring;
  const connectorState = status?.pid_state === 'unknown'
    ? t('remoteAccess.stateNeedsAttention')
    : running
      ? t('common.running')
      : t('common.stopped');

  return (
    <section
      id="remote-access"
      className="scroll-mt-24 overflow-hidden rounded-xl border border-cyan/45 bg-cyan/[0.06] shadow-[0_0_40px_-10px_rgba(63,224,229,0.45)]"
    >
      <div className="flex items-start justify-between gap-4 border-b border-cyan/20 bg-cyan/[0.07] px-5 py-4">
        <div className="min-w-0 space-y-2">
          <h2 className="inline-flex items-center gap-2 text-[15px] font-semibold text-foreground">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full border border-cyan/30 bg-cyan/[0.12] text-cyan">
              <Cloud className="size-4" strokeWidth={2.25} />
            </span>
            {t('remoteAccess.title')}
          </h2>
          <p className="max-w-2xl text-[12px] leading-relaxed text-muted">{t('remoteAccess.subtitleWithLink')}</p>
          <ol className="ml-4 list-decimal space-y-1 text-[12px] leading-relaxed text-muted">
            <li>
              <Trans
                i18nKey="remoteAccess.flowStep1"
                components={{
                  console: (
                    <a
                      href={VIBE_CLOUD_APP_URL}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-0.5 font-medium text-cyan hover:underline"
                    />
                  ),
                }}
              />
            </li>
            <li>{t('remoteAccess.flowStep2')}</li>
            <li>{t('remoteAccess.flowStep3')}</li>
          </ol>
        </div>
        <Button
          variant="secondary"
          size="xs"
          className="shrink-0"
          onClick={refresh}
          type="button"
        >
          <RefreshCcw className="size-3.5" />
          {t('common.refresh')}
        </Button>
      </div>

      <div className="grid border-b border-border md:grid-cols-3">
        <div className="border-b border-border px-5 py-3.5 md:border-b-0 md:border-r">
          <div className="text-[12px] text-muted">{t('remoteAccess.paired')}</div>
          <div className="mt-1">
            {paired ? (
              <Badge variant="success">{t('common.enabled')}</Badge>
            ) : (
              <Badge variant="secondary">{t('common.disabled')}</Badge>
            )}
          </div>
        </div>
        <div className="border-b border-border px-5 py-3.5 md:border-b-0 md:border-r">
          <div className="text-[12px] text-muted">{t('remoteAccess.connector')}</div>
          <div className="mt-1 text-[13px] font-medium text-foreground">{loading ? t('common.loading') : connectorState}</div>
        </div>
        <div className="px-5 py-3.5">
          <div className="text-[12px] text-muted">{t('remoteAccess.vibeCloudService')}</div>
          <a className="mt-1 inline-flex text-[13px] font-medium text-cyan" href={VIBE_CLOUD_URL} target="_blank" rel="noreferrer">
            avibe.bot
            <ExternalLink className="ml-1 size-3.5" />
          </a>
        </div>
      </div>

      {showPairingForm ? (
        <div className="grid gap-3 px-5 py-4 md:grid-cols-[1fr_auto] md:items-end">
          <label className="space-y-1.5">
            <span className="text-[12px] font-medium text-foreground">{t('remoteAccess.pairingKey')}</span>
            <CompactField
              className="w-full font-mono"
              value={pairingKey}
              onChange={(event) => setPairingKey(event.target.value)}
              placeholder="vrp_xxxxxxxxxxxxxxxxx"
            />
            <span className="block text-[10px] text-muted">{t('remoteAccess.pairingKeyHelp')}</span>
          </label>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="default"
              size="xs"
              className="font-semibold"
              disabled={pairing || !pairingKey.trim()}
              onClick={pair}
            >
              <Link2 className="size-3.5" />
              {pairing ? t('remoteAccess.pairing') : t('remoteAccess.pair')}
            </Button>
            {paired && (
              <Button
                type="button"
                variant="secondary"
                size="xs"
                onClick={() => {
                  setReconfiguring(false);
                  setPairingKey('');
                }}
              >
                {t('common.cancel')}
              </Button>
            )}
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[13px] font-medium text-mint">
              <CheckCircle2 className="size-3.5" />
              {t('remoteAccess.configuredBadge')}
            </div>
            {publicUrl && (
              <a
                href={publicUrl}
                target="_blank"
                rel="noreferrer"
                className="mt-1 inline-flex max-w-full items-center gap-1 truncate font-mono text-[11px] text-cyan hover:underline"
                title={publicUrl}
              >
                <span className="truncate">{publicUrl}</span>
                <ExternalLink className="size-3 shrink-0" />
              </a>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="secondary"
              size="xs"
              onClick={() => setReconfiguring(true)}
            >
              {t('remoteAccess.repair')}
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="xs"
              disabled={!paired || running}
              onClick={start}
            >
              {t('common.start')}
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="xs"
              disabled={!paired || !running}
              onClick={stop}
            >
              {t('common.stop')}
            </Button>
          </div>
        </div>
      )}

      {actionMessage && (
        <div className={`border-t border-border px-4 py-3 text-[12px] ${
          actionMessage.type === 'error' ? 'text-gold' : 'text-mint'
        }`}>
          {actionMessage.text}
        </div>
      )}
    </section>
  );
};
