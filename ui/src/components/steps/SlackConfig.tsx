import React, { useEffect, useMemo, useState } from 'react';
import { Lock, Shield, RefreshCw, Copy, ExternalLink, Check, ChevronDown, Key, Hash, Plus, ArrowLeft, ArrowRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { hasUsableSecret, secretInputValue, withSecretDrafts } from '../../lib/secretFields';
import { copyTextToClipboard } from '../../lib/utils';
import { openLinkInNewContext } from '../../lib/pwaNavigation';
import { EmbeddedConfigShell, EyebrowBadge, WizardCard } from '../visual';
import { ProxyUrlField } from '../shared/ProxyUrlField';
import { StepHeader, StepShell } from '../shared/WizardStep';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { errorMessage } from '@/lib/errorMessage';

interface SlackConfigProps {
  data: any;
  onNext: (data: any) => void;
  onBack?: () => void;
  embedded?: boolean;
  onApply?: (data: any) => Promise<void> | void;
  onCancel?: () => void;
}

// Mirrors design.pen XCWAT (Slack creds wizard step): WizardCard 920 wide,
// sStep accordion rows with mint-bordered active row + faint border collapsed
// rows, navigation row with chevron-left back and mint pill next.
export const SlackConfig: React.FC<SlackConfigProps> = ({ data, onNext, onBack, embedded = false, onApply, onCancel }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [botToken, setBotToken] = useState(secretInputValue(data.slack, 'bot_token') || data.slackBotToken || '');
  const [appToken, setAppToken] = useState(secretInputValue(data.slack, 'app_token') || data.slackAppToken || '');
  const [proxyUrl, setProxyUrl] = useState(data.slack?.proxy_url || '');
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [authResult, setAuthResult] = useState<any>(null);
  const [manifest, setManifest] = useState<string>('');
  const [manifestCompact, setManifestCompact] = useState<string>('');
  const [manifestLoading, setManifestLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  // Collapsible states for each step
  const [expandedSteps, setExpandedSteps] = useState<Record<number, boolean>>({ 1: true, 2: false, 3: false, 4: false });

  const isValid = useMemo(
    () =>
      (botToken ? botToken.startsWith('xoxb-') : hasUsableSecret(data.slack, 'bot_token')) &&
      (appToken ? appToken.startsWith('xapp-') : hasUsableSecret(data.slack, 'app_token')),
    [appToken, botToken, data.slack]
  );

  useEffect(() => {
    void loadManifest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-expand next step when token is entered
  useEffect(() => {
    if ((botToken.startsWith('xoxb-') || (!botToken && hasUsableSecret(data.slack, 'bot_token'))) && !expandedSteps[3]) {
      setExpandedSteps((prev) => ({ ...prev, 2: false, 3: true }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botToken, data.slack]);

  useEffect(() => {
    if ((appToken.startsWith('xapp-') || (!appToken && hasUsableSecret(data.slack, 'app_token'))) && !expandedSteps[4]) {
      setExpandedSteps((prev) => ({ ...prev, 3: false, 4: true }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appToken, data.slack]);

  const loadManifest = async () => {
    setManifestLoading(true);
    try {
      const result = await api.slackManifest();
      if (result.ok) {
        if (result.manifest) setManifest(result.manifest);
        if (result.manifest_compact) setManifestCompact(result.manifest_compact);
      }
    } catch (err) {
      console.error('Failed to load manifest:', err);
    } finally {
      setManifestLoading(false);
    }
  };

  const copyManifest = async () => {
    if (!manifest) return;
    const ok = await copyTextToClipboard(manifest);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      return;
    }
    showToast(t('common.copyFailed'), 'error');
  };

  const openSlackCreateApp = () => {
    if (manifestCompact) {
      const url = `https://api.slack.com/apps?new_app=1&manifest_json=${encodeURIComponent(manifestCompact)}`;
      openLinkInNewContext(url);
    } else {
      openLinkInNewContext('https://api.slack.com/apps?new_app=1');
    }
  };

  const runAuthTest = async () => {
    if (!botToken && !hasUsableSecret(data.slack, 'bot_token')) return;
    setChecking(true);
    try {
      const result = await api.slackAuthTest(botToken, proxyUrl);
      setAuthResult(result);
    } catch (err) {
      setAuthResult({ ok: false, error: errorMessage(err) || 'Request failed' });
    } finally {
      setChecking(false);
    }
  };

  const toggleStep = (step: number) => {
    setExpandedSteps((prev) => ({ ...prev, [step]: !prev[step] }));
  };

  const completedCount = [
    !!manifest, // step 1 done as soon as manifest is available (best heuristic without backend signal)
    botToken.startsWith('xoxb-') || (!botToken && hasUsableSecret(data.slack, 'bot_token')),
    appToken.startsWith('xapp-') || (!appToken && hasUsableSecret(data.slack, 'app_token')),
    Boolean(authResult?.ok),
  ].filter(Boolean).length;

  const buildSubmitData = () => ({
    slack: {
      ...withSecretDrafts(data.slack, { bot_token: botToken, app_token: appToken }),
      proxy_url: proxyUrl || undefined,
    },
    mode: 'self_host',
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

  const stepShells = (
    <>
          {/* steps content */}
          {/* Step 1 */}
          <StepShell active={expandedSteps[1]}>
            <StepHeader
              step={1}
              title={t('slackConfig.step1Title')}
              icon={<Plus size={16} className="text-cyan-ink" />}
              expanded={expandedSteps[1]}
              onToggle={() => toggleStep(1)}
            />
            {expandedSteps[1] && (
              <div className="space-y-4 border-t border-border px-5 py-4">
                <p className="text-[13px] leading-[1.55] text-muted">{t('slackConfig.step1Description')}</p>
                <div className="flex flex-wrap items-center gap-3">
                  <Button
                    variant="brand"
                    size="sm"
                    onClick={openSlackCreateApp}
                    disabled={!manifestCompact || manifestLoading}
                  >
                    <ExternalLink size={14} strokeWidth={2.25} />
                    {t('slackConfig.createSlackApp')}
                  </Button>
                  {manifest && (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={copyManifest}
                      disabled={manifestLoading}
                    >
                      {copied ? <Check size={14} /> : <Copy size={14} />}
                      {copied ? t('slackConfig.copied') : t('slackConfig.copyManifest')}
                    </Button>
                  )}
                </div>
                {manifest && (
                  <details className="group">
                    <summary className="flex cursor-pointer items-center gap-1 text-[12px] text-cyan-ink hover:text-cyan-ink/80">
                      <ChevronDown size={14} className="transition-transform group-open:rotate-180" />
                      {t('slackConfig.viewManifest')}
                    </summary>
                    <pre className="mt-2 max-h-40 overflow-auto rounded-lg border border-border bg-background px-3 py-3 font-mono text-[11px] text-foreground">
                      {manifest}
                    </pre>
                  </details>
                )}
                {manifestLoading && (
                  <div className="flex items-center gap-2 text-[12px] text-muted">
                    <RefreshCw size={14} className="animate-spin" />
                    {t('common.loading')}
                  </div>
                )}
                <div className="rounded-lg border border-cyan/30 bg-cyan/[0.06] px-3 py-2 text-[12px] leading-[1.55] text-cyan-ink">
                  <strong>{t('slackConfig.tip')}:</strong> {t('slackConfig.step1Tip')}
                </div>
              </div>
            )}
          </StepShell>

          {/* Step 2 */}
          <StepShell active={expandedSteps[2]}>
            <StepHeader
              step={2}
              title={t('slackConfig.step2Title')}
              icon={<Shield size={16} className="text-cyan-ink" />}
              completed={botToken.startsWith('xoxb-')}
              expanded={expandedSteps[2]}
              onToggle={() => toggleStep(2)}
            />
            {expandedSteps[2] && (
              <div className="space-y-4 border-t border-border px-5 py-4">
                <p className="text-[13px] leading-[1.55] text-muted">{t('slackConfig.step2Description')}</p>
                <ol className="list-inside list-decimal space-y-1.5 pl-1 text-[12px] leading-[1.55] text-muted">
                  <li>{t('slackConfig.step2Item1')}</li>
                  <li>{t('slackConfig.step2Item2')}</li>
                  <li>{t('slackConfig.step2Item3')}</li>
                  <li>{t('slackConfig.step2Item4')}</li>
                </ol>
                <div className="space-y-2 pt-1">
                  <label className="flex items-center gap-2 text-[12px] font-medium text-foreground">
                    <Key size={14} className="text-cyan-ink" /> {t('slackConfig.botToken')}
                  </label>
                  <Input
                    type="password"
                    value={botToken}
                    onChange={(e) => setBotToken(e.target.value)}
                    placeholder={t('slackConfig.botTokenPlaceholder')}
                    className="w-full font-mono text-[12px]"
                  />
                  <p className="text-[11px] text-muted">
                    {t('slackConfig.botTokenHint')}{' '}
                    <code className="rounded bg-foreground/[0.06] px-1.5 py-0.5 text-foreground">xoxb-</code>
                  </p>
                </div>
              </div>
            )}
          </StepShell>

          {/* Step 3 */}
          <StepShell active={expandedSteps[3]}>
            <StepHeader
              step={3}
              title={t('slackConfig.step3Title')}
              icon={<Lock size={16} className="text-cyan-ink" />}
              completed={appToken.startsWith('xapp-')}
              expanded={expandedSteps[3]}
              onToggle={() => toggleStep(3)}
            />
            {expandedSteps[3] && (
              <div className="space-y-4 border-t border-border px-5 py-4">
                <p className="text-[13px] leading-[1.55] text-muted">{t('slackConfig.step3Description')}</p>
                <ol className="list-inside list-decimal space-y-1.5 pl-1 text-[12px] leading-[1.55] text-muted">
                  <li>{t('slackConfig.step3Item1')}</li>
                  <li>{t('slackConfig.step3Item2')}</li>
                  <li>{t('slackConfig.step3Item3')}</li>
                  <li>{t('slackConfig.step3Item4')}</li>
                  <li>{t('slackConfig.step3Item5')}</li>
                </ol>
                <div className="space-y-2 pt-1">
                  <label className="flex items-center gap-2 text-[12px] font-medium text-foreground">
                    <Key size={14} className="text-cyan-ink" /> {t('slackConfig.appToken')}
                  </label>
                  <Input
                    type="password"
                    value={appToken}
                    onChange={(e) => setAppToken(e.target.value)}
                    placeholder={t('slackConfig.appTokenPlaceholder')}
                    className="w-full font-mono text-[12px]"
                  />
                  <p className="text-[11px] text-muted">
                    {t('slackConfig.appTokenHint')}{' '}
                    <code className="rounded bg-foreground/[0.06] px-1.5 py-0.5 text-foreground">xapp-</code>
                  </p>
                </div>
                <div className="rounded-lg border border-gold/30 bg-gold/10 px-3 py-2 text-[12px] leading-[1.55] text-gold-ink">
                  <strong>{t('slackConfig.important')}:</strong> {t('slackConfig.step3Warning')}
                </div>
              </div>
            )}
          </StepShell>

          {/* Step 4 */}
          <StepShell active={expandedSteps[4]}>
            <StepHeader
              step={4}
              title={t('slackConfig.step4Title')}
              icon={<Hash size={16} className="text-cyan-ink" />}
              completed={!!authResult?.ok}
              expanded={expandedSteps[4]}
              onToggle={() => toggleStep(4)}
            />
            {expandedSteps[4] && (
              <div className="space-y-4 border-t border-border px-5 py-4">
                <p className="text-[13px] leading-[1.55] text-muted">{t('slackConfig.step4Description')}</p>
                <ProxyUrlField value={proxyUrl} onChange={setProxyUrl} />
                <div className="flex flex-wrap items-center gap-3">
                  <Button
                    variant="brand"
                    size="sm"
                    onClick={runAuthTest}
                    disabled={
                      (!botToken && !hasUsableSecret(data.slack, 'bot_token')) ||
                      (!appToken && !hasUsableSecret(data.slack, 'app_token')) ||
                      checking
                    }
                  >
                    {checking ? <RefreshCw size={14} className="animate-spin" /> : <Shield size={14} strokeWidth={2.25} />}
                    {t('slackConfig.validateTokens')}
                  </Button>
                  {authResult && (
                    <span
                      className={clsx(
                        'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] font-medium',
                        authResult.ok
                          ? 'border-mint/30 bg-mint/[0.08] text-mint-ink'
                          : 'border-destructive/30 bg-destructive/10 text-destructive-ink'
                      )}
                    >
                      {authResult.ok ? <Check size={14} /> : null}
                      {authResult.ok ? t('slackConfig.tokenValidated') : `${t('slackConfig.authFailed')}: ${authResult.error}`}
                    </span>
                  )}
                </div>
                {authResult?.ok && (
                  <div className="space-y-2.5 pt-1">
                    <p className="text-[12px] font-semibold text-foreground">{t('slackConfig.inviteBotTitle')}</p>
                    <ol className="list-inside list-decimal space-y-1.5 pl-1 text-[12px] leading-[1.55] text-muted">
                      <li>{t('slackConfig.step4Item1')}</li>
                      <li>{t('slackConfig.step4Item2')}</li>
                      <li>{t('slackConfig.step4Item3')}</li>
                    </ol>
                    <div className="rounded-lg border border-mint/30 bg-mint/[0.08] px-3 py-2 text-[12px] leading-[1.55] text-mint-ink">
                      <strong>{t('slackConfig.tip')}:</strong> {t('slackConfig.step4Tip')}
                    </div>
                  </div>
                )}
              </div>
            )}
          </StepShell>
    </>
  );

  if (embedded) {
    return (
      <EmbeddedConfigShell
        total={4}
        completed={completedCount}
        canApply={isValid}
        applying={applying}
        onApply={() => void handleApply()}
        onCancel={() => onCancel?.()}
      >
        {stepShells}
      </EmbeddedConfigShell>
    );
  }

  return (
    <div className="flex w-full justify-center">
      <WizardCard className="gap-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-2">
            <EyebrowBadge tone="mint">{t('slackConfig.eyebrow')}</EyebrowBadge>
            <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
              {t('slackConfig.title')}
            </h2>
            <p className="max-w-[560px] text-[14px] leading-[1.55] text-muted">
              {t('slackConfig.selfHostDescription')}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-foreground/[0.04] px-3 py-1.5">
            <span className="font-mono text-[11px] font-bold uppercase tracking-[0.16em] text-mint-ink">
              {completedCount} / 4
            </span>
            <div className="flex gap-1">
              {[0, 1, 2, 3].map((i) => (
                <span
                  key={i}
                  className={clsx(
                    'h-1 w-5 rounded-full',
                    i < completedCount ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.6)]' : 'bg-foreground/[0.08]'
                  )}
                />
              ))}
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-3">{stepShells}</div>

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
            onClick={() =>
              onNext(buildSubmitData())
            }
            disabled={!isValid}
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
