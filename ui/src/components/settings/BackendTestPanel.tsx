import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Play, Zap } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../ui/button';
import { Select } from '@/components/ui/select';
import { useApi } from '@/context/ApiContext';
import type { BackendAuthTestResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

type Backend = 'claude' | 'codex';

export type BackendTestPanelProps = {
  backend: Backend;
};

/**
 * Settings → Backends connectivity probe. Mirrors the ``cdTest2`` /
 * ``cxTest`` panels in ``design.pen``: sends a single ``Hi`` prompt
 * through the backend CLI so the user can confirm both the credentials
 * and the endpoint (Base URL) round-trip end-to-end. Works in both
 * OAuth and API-Key modes — the underlying CLI uses whichever auth
 * source is configured at launch.
 *
 * The model dropdown lets the user override the backend's default
 * (important for Codex users whose ``config.toml`` selects
 * ``model_reasoning_effort=xhigh``; with the override the probe stays
 * fast). On success we echo the first content line of the model's
 * response so the user sees real output, not just a duration.
 */
export const BackendTestPanel: React.FC<BackendTestPanelProps> = ({ backend }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [testing, setTesting] = useState(false);
  const [lastResult, setLastResult] = useState<BackendAuthTestResult | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');

  // Load the available models catalog once on mount so the dropdown can
  // pre-populate. Failures fall back to "default-only" — the user can
  // still test against whatever the backend's config.toml selects.
  useEffect(() => {
    let cancelled = false;
    const loader = backend === 'claude' ? api.claudeModels() : api.codexModels();
    loader
      .then((data) => {
        if (cancelled) return;
        if (data?.ok && Array.isArray(data.models)) {
          setModels(data.models.filter((m): m is string => typeof m === 'string' && m.length > 0));
        }
      })
      .catch(() => {
        /* swallow — dropdown stays empty; default-model path still works */
      });
    return () => {
      cancelled = true;
    };
  }, [api, backend]);

  // Map the backend's structured error codes to UI sentences. Anything
  // we don't recognise falls back to the generic ``cli_failed`` line,
  // and the raw ``detail`` is still surfaced in the toast for inspection.
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
      trust_check_failed: 'settings.backends.testFailureTrustCheck',
      cli_not_found: 'settings.backends.testFailureCliNotFound',
      spawn_failed: 'settings.backends.testFailureSpawnFailed',
      timed_out: 'settings.backends.testFailureTimedOut',
      not_logged_in: 'settings.backends.testFailureNotLoggedIn',
      cli_failed: 'settings.backends.testFailureCliFailed',
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
      const result = await api.testBackendAuth(backend, {
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
    } catch (err: any) {
      const fallback = { ok: false, error: err?.message || 'test_failed' } as BackendAuthTestResult;
      setLastResult(fallback);
      showToast(t('settings.backends.testConnectionFailedToast', { detail: fallback.error }), 'error');
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
    <div
      className={clsx(
        'flex flex-col gap-3 rounded-xl border border-border bg-foreground/[0.025] p-5',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-[10px] border border-mint/30 bg-mint-soft">
            <Zap size={18} className="text-mint" />
          </div>
          <div className="flex flex-col gap-0.5">
            <p className="text-[14px] font-bold text-foreground">
              {t('settings.backends.testConnectionTitle')}
            </p>
            <p className="text-[12px] text-muted">
              {t('settings.backends.testConnectionSubtitle')}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {/* Model picker — empty = use the backend CLI's default. Models
              are fetched from the same routing-config catalog so users
              don't have to guess identifiers. Codex users in particular
              want this to bypass a reasoning-heavy config default. */}
          <Select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            disabled={testing}
            wrapperClassName="w-auto"
            className="font-mono text-[11px]"
            aria-label={t('settings.backends.testConnectionModelLabel') as string}
          >
            <option value="">{t('settings.backends.testConnectionModelDefault')}</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
          {resultLine && (
            <span
              className={clsx(
                'font-mono text-[11px] font-semibold',
                lastResult?.ok ? 'text-mint' : 'text-destructive',
              )}
            >
              {resultLine}
            </span>
          )}
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
      </div>

      {/* Show what the model actually said on success — without this the
          user only sees "ok · 312 ms" and has to trust us that the
          round-trip went through. The excerpt is server-side trimmed to
          240 chars, single-line. */}
      {lastResult?.ok && lastResult.excerpt && (
        <div className="rounded-md border border-border bg-background px-3 py-2">
          <p className="font-mono text-[11px] uppercase tracking-wide text-muted">
            {t('settings.backends.testConnectionResponseLabel')}
          </p>
          <p className="mt-1 break-words font-mono text-[12px] leading-relaxed text-foreground">
            {lastResult.excerpt}
          </p>
        </div>
      )}

      {/* Failure detail block — surfaces the raw stderr / stdout snippet
          the backend captured ("detail" field) instead of pointing the
          user at a non-clickable toast. Previously the i18n copy said
          "click the toast to see raw output" but no such affordance
          existed. */}
      {lastResult && !lastResult.ok && lastResult.detail && (
        <details className="rounded-md border border-destructive/30 bg-destructive/[0.04] px-3 py-2 [&[open]>summary]:mb-2">
          <summary className="cursor-pointer font-mono text-[11px] uppercase tracking-wide text-destructive">
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
