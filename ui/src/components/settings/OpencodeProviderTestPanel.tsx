import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Play, Zap } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../ui/button';
import { Select } from '@/components/ui/select';
import { useApi } from '@/context/ApiContext';
import type { BackendAuthTestResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { errorMessage } from '@/lib/errorMessage';

export type OpencodeProviderTestPanelProps = {
  providerId: string;
  providerName: string;
  models: string[];
  defaultModel?: string | null;
};

/**
 * Per-provider Test connectivity panel for OpenCode. Slots inside each
 * provider card alongside the OAuth panel + API Key field. Mirrors
 * ``BackendTestPanel`` (Claude / Codex) but probes a single OpenCode
 * provider through ``opencode serve``'s HTTP API rather than spawning
 * a CLI subprocess — multiple providers can be tested independently
 * without serializing one slow request behind another.
 */
export const OpencodeProviderTestPanel: React.FC<OpencodeProviderTestPanelProps> = ({
  providerId,
  providerName,
  models,
  defaultModel,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [testing, setTesting] = useState(false);
  const [lastResult, setLastResult] = useState<BackendAuthTestResult | null>(null);
  // Default the dropdown to the provider's default model so first-time
  // testers don't have to scroll a long list to pick something.
  const initialModel = useMemo(() => {
    if (defaultModel && models.includes(defaultModel)) return defaultModel;
    return '';
  }, [defaultModel, models]);
  const [selectedModel, setSelectedModel] = useState<string>(initialModel);

  // Reset selected model when the catalog changes (provider remount /
  // model list refresh after save).
  useEffect(() => {
    setSelectedModel(initialModel);
  }, [initialModel]);

  // Reuse the shared classifier → i18n keys. Anything the backend
  // didn't classify lands on the generic ``cli_failed`` fallback and
  // the raw ``detail`` still rides in the toast.
  const failureSentence = (result: BackendAuthTestResult): string => {
    const detail = (result.detail || '').trim();
    const code = (result.error || '').trim();
    const map: Record<string, string> = {
      invalid_credentials: 'settings.backends.testFailureInvalidCredentials',
      forbidden: 'settings.backends.testFailureForbidden',
      model_not_found: 'settings.backends.testFailureModelNotFound',
      rate_limited: 'settings.backends.testFailureRateLimited',
      endpoint_unreachable: 'settings.backends.testFailureEndpointUnreachable',
      server_error: 'settings.backends.testFailureServerError',
      timed_out: 'settings.backends.testFailureTimedOut',
      cli_failed: 'settings.backends.testFailureCliFailed',
      // OpenCode-specific codes (added below in i18n).
      opencode_server_unavailable: 'settings.backends.testFailureOpencodeServerUnavailable',
      no_models_available: 'settings.backends.testFailureNoModels',
      session_create_failed: 'settings.backends.testFailureSessionCreate',
      missing_provider: 'settings.backends.testFailureMissingProvider',
    };
    const key = map[code];
    if (key) {
      return t(key, { detail: detail || code });
    }
    return t('settings.backends.testConnectionFailedToast', {
      detail: detail || code || 'unknown',
    });
  };

  const runTest = async () => {
    setTesting(true);
    try {
      const result = await api.testOpencodeProvider(providerId, {
        model: selectedModel || undefined,
      });
      setLastResult(result);
      if (result.ok) {
        showToast(
          t('settings.backends.testConnectionSuccessToast', { ms: result.duration_ms ?? '?' }),
          'success',
        );
      } else {
        showToast(failureSentence(result), 'error');
      }
    } catch (err) {
      const fallback = { ok: false, error: errorMessage(err) || 'test_failed' } as BackendAuthTestResult;
      setLastResult(fallback);
      showToast(
        t('settings.backends.testConnectionFailedToast', { detail: fallback.error }),
        'error',
      );
    } finally {
      setTesting(false);
    }
  };

  const resultLine = (() => {
    if (!lastResult) return null;
    if (lastResult.ok) {
      return t('settings.backends.testConnectionLastOk', {
        ms: lastResult.duration_ms ?? '?',
      });
    }
    return t('settings.backends.testConnectionLastFail', {
      detail: failureSentence(lastResult),
    });
  })();

  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-border bg-foreground/[0.025] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-[8px] border border-mint/30 bg-mint-soft">
            <Zap size={14} className="text-mint" />
          </div>
          <div className="flex flex-col gap-0.5">
            <p className="text-[13px] font-bold text-foreground">
              {t('settings.backends.opencodeTestTitle', { name: providerName })}
            </p>
            <p className="text-[11px] text-muted">
              {t('settings.backends.opencodeTestSubtitle')}
            </p>
          </div>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Select
          value={selectedModel}
          onChange={(e) => setSelectedModel(e.target.value)}
          disabled={testing || models.length === 0}
          wrapperClassName="w-auto"
          className="font-mono text-[11px]"
          aria-label={t('settings.backends.testConnectionModelLabel') as string}
        >
          <option value="">
            {t('settings.backends.opencodeTestModelDefault')}
          </option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
        <Button
          type="button"
          variant="brand"
          size="sm"
          onClick={() => void runTest()}
          disabled={testing}
        >
          <Play className="size-3" />
          {testing ? t('common.testing') : t('settings.backends.testConnectionRun')}
        </Button>
      </div>
      {resultLine && (
        <p
          className={clsx(
            'font-mono text-[11px] font-semibold',
            lastResult?.ok ? 'text-mint' : 'text-destructive',
          )}
        >
          {resultLine}
        </p>
      )}
      {lastResult?.ok && lastResult.excerpt && (
        <div className="rounded-md border border-border bg-background px-3 py-2">
          <p className="font-mono text-[10px] uppercase tracking-wide text-muted">
            {t('settings.backends.testConnectionResponseLabel')}
          </p>
          <p className="mt-1 break-words font-mono text-[12px] leading-relaxed text-foreground">
            {lastResult.excerpt}
          </p>
        </div>
      )}
      {lastResult && !lastResult.ok && lastResult.detail && (
        <details className="rounded-md border border-destructive/30 bg-destructive/[0.04] px-3 py-2 [&[open]>summary]:mb-2">
          <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wide text-destructive">
            {t('settings.backends.testConnectionRawOutputLabel')}
          </summary>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-background px-3 py-2 font-mono text-[11px] leading-relaxed text-muted">
            {lastResult.detail}
          </pre>
        </details>
      )}
    </div>
  );
};
