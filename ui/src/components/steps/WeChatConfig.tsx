import React, { useCallback, useEffect, useRef, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  KeyRound,
  Loader2,
  RefreshCw,
  Smartphone,
  Wifi,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { hasUsableSecret, secretInputValue, withSecretDraft } from '../../lib/secretFields';
import { EmbeddedConfigShell, EyebrowBadge, WizardCard } from '../visual';
import { ProxyUrlField } from '../shared/ProxyUrlField';
import { Button } from '../ui/button';
import { Input } from '../ui/input';

interface WeChatConfigProps {
  data: Record<string, any>;
  onNext: (data: Record<string, any>) => void;
  onBack?: () => void;
  embedded?: boolean;
  onApply?: (data: Record<string, any>) => Promise<void> | void;
  onCancel?: () => void;
  autoStartLogin?: boolean;
}

const QR_POLL_INTERVAL_MS = 5000;

// Mirrors design.pen XCWAT visual treatment for the QR-driven WeChat onboarding.
// Three-stop horizontal stepper, mint-bordered QR card, mint primary actions.
export const WeChatConfig: React.FC<WeChatConfigProps> = ({
  data,
  onNext,
  onBack,
  embedded = false,
  onApply,
  onCancel,
  autoStartLogin = true,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const [applying, setApplying] = useState(false);

  const [loginState, setLoginState] = useState<
    'idle' | 'qr_ready' | 'scanning' | 'confirming' | 'connected' | 'error'
  >('idle');
  const [qrCodeUrl, setQrCodeUrl] = useState<string>('');
  const [message, setMessage] = useState<string>('');
  const [botToken, setBotToken] = useState<string>(secretInputValue(data.wechat, 'bot_token'));
  const [baseUrl, setBaseUrl] = useState<string>(data.wechat?.base_url || '');
  const [proxyUrl, setProxyUrl] = useState<string>(data.wechat?.proxy_url || '');
  const [verifyCode, setVerifyCode] = useState('');
  const [needsVerifyCode, setNeedsVerifyCode] = useState(false);
  const [starting, setStarting] = useState(false);
  const hasSavedBotToken = hasUsableSecret(data.wechat, 'bot_token', botToken);

  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoStartedRef = useRef(false);
  const activeSessionKeyRef = useRef<string | null>(null);

  const preserveExistingConnectionFields = useCallback((result: Record<string, any> = {}) => {
    setBotToken(result.bot_token || secretInputValue(data.wechat, 'bot_token'));
    setBaseUrl(result.base_url || data.wechat?.base_url || '');
  }, [data.wechat]);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      stopPolling();
    };
  }, [stopPolling]);

  const startLogin = useCallback(async () => {
    setStarting(true);
    setLoginState('idle');
    setMessage('');
    setQrCodeUrl('');
    setBotToken('');
    setBaseUrl('');
    setVerifyCode('');
    setNeedsVerifyCode(false);
    activeSessionKeyRef.current = null;
    stopPolling();

    try {
      let result = await api.wechatStartLogin();
      if (result?.error) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        result = await api.wechatStartLogin();
      }
      if (result.error) {
        setLoginState('error');
        setMessage(result.error);
        return;
      }
      setQrCodeUrl(result.qrcode_url || '');
      setMessage(result.message || '');
      setLoginState('qr_ready');

      if (result.session_key) {
        activeSessionKeyRef.current = result.session_key;
        startPolling(result.session_key);
      }
    } catch (err: any) {
      try {
        await new Promise((resolve) => setTimeout(resolve, 800));
        const retryResult = await api.wechatStartLogin();
        if (retryResult?.error) {
          setLoginState('error');
          setMessage(retryResult.error);
          return;
        }
        setQrCodeUrl(retryResult.qrcode_url || '');
        setMessage(retryResult.message || '');
        setLoginState('qr_ready');
        if (retryResult.session_key) {
          activeSessionKeyRef.current = retryResult.session_key;
          startPolling(retryResult.session_key);
        }
      } catch (retryErr: any) {
        setLoginState('error');
        setMessage(retryErr?.message || err?.message || t('wechatConfig.startFailed'));
      }
    } finally {
      setStarting(false);
    }
  }, [api, stopPolling, t]);

  useEffect(() => {
    if (autoStartedRef.current) return;
    if (!autoStartLogin) return;
    if (starting) return;
    if (loginState !== 'idle') return;
    if (hasSavedBotToken) return;

    autoStartedRef.current = true;
    void startLogin();
  }, [autoStartLogin, loginState, startLogin, starting, hasSavedBotToken]);

  const startPolling = (key: string) => {
    stopPolling();
    const pollOnce = async () => {
      try {
        const result = await api.wechatPollLogin(key);
        if (!result || activeSessionKeyRef.current !== key) return;

        const status = result.status;
        if (status === 'scaned') {
          setNeedsVerifyCode(false);
          setVerifyCode('');
          setLoginState('confirming');
          setMessage(result.message || t('wechatConfig.confirmOnPhone'));
        } else if (status === 'need_verifycode') {
          setNeedsVerifyCode(true);
          setLoginState('confirming');
          setMessage(result.message || t('wechatConfig.verifyCodePrompt'));
        } else if (status === 'confirmed') {
          setNeedsVerifyCode(false);
          setVerifyCode('');
          setLoginState('connected');
          setMessage(result.message || t('wechatConfig.connected'));
          setBotToken(result.bot_token || '');
          setBaseUrl(result.base_url || '');
          activeSessionKeyRef.current = null;
          stopPolling();
          return;
        } else if (status === 'already_connected') {
          setNeedsVerifyCode(false);
          setVerifyCode('');
          setLoginState('connected');
          setMessage(result.message || t('wechatConfig.connected'));
          preserveExistingConnectionFields(result);
          activeSessionKeyRef.current = null;
          stopPolling();
          return;
        } else if (status === 'refreshed') {
          setNeedsVerifyCode(false);
          setVerifyCode('');
          setLoginState('qr_ready');
          setQrCodeUrl(result.qrcode_url || '');
          setMessage(result.message || t('wechatConfig.qrExpired'));
        } else if (status === 'expired') {
          setNeedsVerifyCode(false);
          setLoginState('error');
          setMessage(result.message || t('wechatConfig.qrExpired'));
          activeSessionKeyRef.current = null;
          stopPolling();
          return;
        } else if (status === 'error') {
          setNeedsVerifyCode(false);
          setLoginState('error');
          setMessage(result.message || t('wechatConfig.pollError'));
          activeSessionKeyRef.current = null;
          stopPolling();
          return;
        }
        pollTimerRef.current = setTimeout(() => {
          void pollOnce();
        }, QR_POLL_INTERVAL_MS);
      } catch {
        if (activeSessionKeyRef.current !== key) return;
        pollTimerRef.current = setTimeout(() => {
          void pollOnce();
        }, QR_POLL_INTERVAL_MS);
      }
    };

    void pollOnce();
  };

  const submitVerifyCode = async () => {
    const key = activeSessionKeyRef.current;
    const code = verifyCode.trim();
    if (!key || !code) return;
    stopPolling();
    try {
      const result = await api.wechatPollLogin(key, code);
      if (!result || activeSessionKeyRef.current !== key) return;
      if (result.status === 'confirmed') {
        setNeedsVerifyCode(false);
        setVerifyCode('');
        setLoginState('connected');
        setMessage(result.message || t('wechatConfig.connected'));
        setBotToken(result.bot_token || '');
        setBaseUrl(result.base_url || '');
        activeSessionKeyRef.current = null;
        return;
      }
      if (result.status === 'already_connected') {
        setNeedsVerifyCode(false);
        setVerifyCode('');
        setLoginState('connected');
        setMessage(result.message || t('wechatConfig.connected'));
        preserveExistingConnectionFields(result);
        activeSessionKeyRef.current = null;
        return;
      }
      if (result.status === 'scaned') {
        setNeedsVerifyCode(false);
        setVerifyCode('');
        setLoginState('confirming');
        setMessage(result.message || t('wechatConfig.confirmOnPhone'));
        startPolling(key);
        return;
      }
      if (result.status === 'need_verifycode') {
        setNeedsVerifyCode(true);
        setLoginState('confirming');
        setMessage(result.message || t('wechatConfig.verifyCodePrompt'));
        return;
      }
      if (result.status === 'refreshed') {
        setNeedsVerifyCode(false);
        setVerifyCode('');
        setLoginState('qr_ready');
        setQrCodeUrl(result.qrcode_url || '');
        setMessage(result.message || t('wechatConfig.qrExpired'));
        startPolling(key);
        return;
      }
      if (result.status === 'error' || result.status === 'expired') {
        setNeedsVerifyCode(false);
        setLoginState('error');
        setMessage(result.message || t('wechatConfig.pollError'));
        activeSessionKeyRef.current = null;
        return;
      }
      startPolling(key);
    } catch (err: any) {
      setMessage(err?.message || t('wechatConfig.pollError'));
      startPolling(key);
    }
  };

  const canProceed = hasSavedBotToken;
  const isAlreadyBound = loginState === 'idle' && !botToken && hasSavedBotToken;

  const getStepState = () => {
    if (isAlreadyBound) return { step: 3, scanning: false, connected: true };
    if (loginState === 'idle') return { step: 1, scanning: false, connected: false };
    if (loginState === 'qr_ready') return { step: 2, scanning: false, connected: false };
    if (loginState === 'scanning' || loginState === 'confirming') return { step: 2, scanning: true, connected: false };
    if (loginState === 'connected') return { step: 3, scanning: false, connected: true };
    return { step: 1, scanning: false, connected: false };
  };

  const { step, connected } = getStepState();

  const completedDots = connected ? 3 : step;

  const stepLabels = [t('wechatConfig.stepStart'), t('wechatConfig.stepScan'), t('wechatConfig.stepDone')];

  const buildSubmitData = () => ({
    platform: 'wechat',
    wechat: {
      ...withSecretDraft(data.wechat, 'bot_token', botToken),
      base_url: baseUrl || data.wechat?.base_url || '',
      proxy_url: proxyUrl || undefined,
    },
  });

  const handleApply = async () => {
    if (!onApply) return;
    setApplying(true);
    try {
      await onApply(buildSubmitData());
    } finally {
      setApplying(false);
    }
  };

  const bodyContent = (
    <>
        {/* Horizontal stepper */}
        <div className="rounded-xl border border-border bg-background px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            {stepLabels.map((label, idx) => {
              const num = idx + 1;
              const isCompleted = num < step || (connected && num <= 3);
              const isActive = !connected && num === step && loginState !== 'error';
              return (
                <React.Fragment key={label}>
                  <div className="flex items-center gap-2">
                    <span
                      className={clsx(
                        'flex size-7 items-center justify-center rounded-full text-[12px] font-bold transition-colors',
                        isCompleted
                          ? 'bg-mint text-primary-foreground'
                          : isActive
                            ? 'bg-cyan/15 text-cyan'
                            : 'bg-foreground/[0.06] text-muted'
                      )}
                    >
                      {isCompleted ? <Check size={14} /> : num}
                    </span>
                    <span
                      className={clsx(
                        'text-[12px] font-semibold',
                        isCompleted || isActive ? 'text-foreground' : 'text-muted'
                      )}
                    >
                      {label}
                    </span>
                  </div>
                  {idx < stepLabels.length - 1 && <span className="mx-2 h-px flex-1 bg-border" />}
                </React.Fragment>
              );
            })}
          </div>
        </div>

        <div className="space-y-4">
          {/* Already bound */}
          {isAlreadyBound && (
            <div className="rounded-xl border border-border bg-background px-6 py-6">
              <div className="flex flex-col items-center gap-4 text-center">
                <div className="flex size-16 items-center justify-center rounded-full border border-mint/30 bg-mint/[0.08] text-mint shadow-[0_0_32px_-6px_rgba(91,255,160,0.5)]">
                  <Check size={32} />
                </div>
                <div>
                  <h3 className="text-[16px] font-semibold text-foreground">{t('wechatConfig.alreadyBound')}</h3>
                  <p className="mt-1 text-[12px] text-muted">{t('wechatConfig.alreadyBoundDesc')}</p>
                </div>
                <div className="w-full max-w-md rounded-lg border border-border bg-surface-2 px-3 py-2.5 text-left">
                  <div className="mb-1 flex items-center gap-1 text-[11px] text-muted">
                    <KeyRound size={12} /> Token
                  </div>
                  <div className="truncate font-mono text-[12px] text-foreground">
                    {botToken.slice(0, 12)}
                    {'•'.repeat(16)}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    autoStartedRef.current = true;
                    void startLogin();
                  }}
                  disabled={starting}
                >
                  <RefreshCw size={14} className={starting ? 'animate-spin' : ''} />
                  {t('wechatConfig.rebind')}
                </Button>
              </div>
            </div>
          )}

          {/* Starting */}
          {loginState === 'idle' && !botToken && !isAlreadyBound && (
            <div className="rounded-xl border border-border bg-background px-6 py-8 text-center">
              <div className="mx-auto flex size-14 items-center justify-center rounded-full border border-cyan/30 bg-cyan/[0.06] text-cyan">
                {starting || autoStartLogin ? (
                  <Loader2 size={26} className="animate-spin" />
                ) : (
                  <Smartphone size={26} />
                )}
              </div>
              <p className="mt-3 text-[13px] text-muted">
                {starting ? t('wechatConfig.starting') : t('wechatConfig.startDescription')}
              </p>
              {!autoStartLogin && !starting && (
                <Button type="button" variant="brand" size="sm" className="mt-4" onClick={startLogin}>
                  <RefreshCw size={14} strokeWidth={2.25} />
                  {t('wechatConfig.startLogin')}
                </Button>
              )}
            </div>
          )}

          {/* QR */}
          {(loginState === 'qr_ready' || loginState === 'scanning' || loginState === 'confirming') && (
            <div className="rounded-xl border border-mint/35 bg-surface-2 px-6 py-6 shadow-[0_8px_32px_-8px_rgba(91,255,160,0.078)]">
              <div className="flex flex-col items-center gap-4">
                <div className="rounded-xl border border-border bg-white p-4 shadow-[0_0_24px_-4px_rgba(91,255,160,0.4)]">
                  <QRCodeSVG value={qrCodeUrl} size={224} level="M" includeMargin={false} />
                </div>
                <div
                  className={clsx(
                    'inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-[12px] font-medium',
                    loginState === 'qr_ready' && 'border-cyan/30 bg-cyan/[0.06] text-cyan',
                    (loginState === 'scanning' || loginState === 'confirming') &&
                      'border-gold/30 bg-gold/10 text-gold'
                  )}
                >
                  {loginState === 'qr_ready' && (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      {t('wechatConfig.waitingForScan')}
                    </>
                  )}
                  {(loginState === 'scanning' || loginState === 'confirming') && (
                    <>
                      <Smartphone size={14} />
                      {t('wechatConfig.confirmOnPhone')}
                    </>
                  )}
                </div>
                <p className="text-center text-[11px] text-muted">{t('wechatConfig.scanHint')}</p>
                {needsVerifyCode && (
                  <div className="w-full max-w-xs space-y-2">
                    <Input
                      value={verifyCode}
                      onChange={(event) => setVerifyCode(event.target.value)}
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      placeholder={t('wechatConfig.verifyCodePlaceholder')}
                    />
                    <Button type="button" variant="brand" size="sm" className="w-full" onClick={submitVerifyCode}>
                      {t('wechatConfig.submitVerifyCode')}
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Connected */}
          {loginState === 'connected' && (
            <div className="rounded-xl border border-mint/35 bg-surface-2 px-6 py-6 shadow-[0_8px_32px_-8px_rgba(91,255,160,0.078)]">
              <div className="flex flex-col items-center gap-4 text-center">
                <div className="flex size-16 items-center justify-center rounded-full border border-mint/30 bg-mint/[0.08] text-mint shadow-[0_0_32px_-6px_rgba(91,255,160,0.5)]">
                  <Check size={32} />
                </div>
                <div>
                  <h3 className="text-[16px] font-semibold text-foreground">{t('wechatConfig.connectedTitle')}</h3>
                  <p className="mt-1 text-[12px] text-muted">{message}</p>
                </div>
                <div className="inline-flex items-center gap-2 rounded-lg border border-mint/30 bg-mint/[0.08] px-3 py-1.5 text-[12px] font-medium text-mint">
                  <Wifi size={14} />
                  {t('wechatConfig.connectionEstablished')}
                </div>
                <div className="w-full max-w-md rounded-lg border border-cyan/30 bg-cyan/[0.06] px-3 py-2.5 text-left">
                  <div className="text-[12px] font-semibold text-foreground">{t('wechatConfig.nextStepTitle')}</div>
                  <p className="mt-0.5 text-[11px] text-muted">{t('wechatConfig.nextStepDesc')}</p>
                </div>
              </div>
            </div>
          )}

          {/* Error */}
          {loginState === 'error' && (
            <div className="rounded-xl border border-danger/30 bg-danger/10 px-6 py-6">
              <div className="flex flex-col items-center gap-4 text-center">
                <div className="flex size-14 items-center justify-center rounded-full border border-danger/30 bg-danger/15 text-danger">
                  <AlertTriangle size={28} />
                </div>
                <div>
                  <h3 className="text-[14px] font-semibold text-foreground">{t('wechatConfig.errorTitle')}</h3>
                  <p className="mt-1 text-[12px] text-danger">{message}</p>
                </div>
                <Button variant="brand" size="sm" onClick={startLogin} disabled={starting}>
                  <RefreshCw size={14} strokeWidth={2.25} />
                  {t('wechatConfig.retry')}
                </Button>
              </div>
            </div>
          )}

          {/* Proxy (optional) — applies to outbound iLink/CDN traffic */}
          <div className="rounded-xl border border-border bg-background px-5 py-4">
            <ProxyUrlField value={proxyUrl} onChange={setProxyUrl} />
          </div>
        </div>
    </>
  );

  if (embedded) {
    return (
      <EmbeddedConfigShell
        total={3}
        completed={completedDots}
        canApply={canProceed}
        applying={applying}
        onApply={() => void handleApply()}
        onCancel={() => onCancel?.()}
      >
        {bodyContent}
      </EmbeddedConfigShell>
    );
  }

  return (
    <div className="flex w-full justify-center">
      <WizardCard className="gap-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-2">
            <EyebrowBadge tone="mint">{t('wechatConfig.eyebrow')}</EyebrowBadge>
            <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
              {t('wechatConfig.title')}
            </h2>
            <p className="max-w-[560px] text-[14px] leading-[1.55] text-muted">
              {t('wechatConfig.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-foreground/[0.04] px-3 py-1.5">
            <span className="font-mono text-[11px] font-bold uppercase tracking-[0.16em] text-mint">
              {completedDots} / 3
            </span>
            <div className="flex gap-1">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  className={clsx(
                    'h-1 w-6 rounded-full',
                    i < completedDots ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.6)]' : 'bg-foreground/[0.08]'
                  )}
                />
              ))}
            </div>
          </div>
        </div>

        {bodyContent}

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          <Button
            type="button"
            variant="secondary"
            size="default"
            onClick={onBack}
            className="font-semibold"
          >
            <ArrowLeft size={14} strokeWidth={2.25} />
            {t('common.back')}
          </Button>
          <Button
            type="button"
            variant="brand"
            size="default"
            onClick={() => onNext(buildSubmitData())}
            disabled={!canProceed}
            className="flex-1 sm:flex-none"
          >
            {t('common.continue')}
            <ArrowRight size={14} strokeWidth={2.25} />
          </Button>
        </div>
      </WizardCard>
    </div>
  );
};
