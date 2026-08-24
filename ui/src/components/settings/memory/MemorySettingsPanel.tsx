import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowUpRight,
  Building2,
  Cloud,
  Loader2,
  RefreshCw,
  ShieldAlert,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Checkbox } from '../../ui/checkbox';
import { ConfirmDialog } from '../../ui/confirm-dialog';
import { InfoHint } from '../../ui/info-hint';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { Select } from '../../ui/select';
import { Switch } from '../../ui/switch';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryEndpointConfig,
  MemoryMaintenance,
  MemoryRerankProvider,
  MemorySettings,
  MemorySettingsPatch,
  MemorySettingsResult,
} from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { isMemoryOk, memoryErrorMessage } from '../../../lib/memoryRead';
import {
  DASHSCOPE_RERANK_MODEL,
  DEFAULT_MEMORY_RERANK_PROVIDER,
  MEMORY_RERANK_PROVIDERS,
  buildEndpointPatch,
  draftFromConfig,
  normalizeRerankProvider,
} from '../../../lib/memorySettings';
import type { EndpointDraft } from '../../../lib/memorySettings';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;

const EMPTY_ENDPOINT: MemoryEndpointConfig = {
  base_url: null,
  model: null,
  api_key: null,
  has_api_key: false,
};

const RERANK_FIELD_HINTS: Record<MemoryRerankProvider, { baseUrl: string; model: string }> = {
  deepinfra: {
    baseUrl: 'https://api.deepinfra.com/v1/inference',
    model: 'Qwen/Qwen3-Reranker-4B',
  },
  vllm: {
    baseUrl: 'http://localhost:8000/v1',
    model: 'Qwen/Qwen3-Reranker-4B',
  },
  dashscope: {
    baseUrl: 'https://dashscope.aliyuncs.com',
    model: DASHSCOPE_RERANK_MODEL,
  },
};

const EndpointFields: React.FC<{
  title: string;
  help: string;
  helpLabel: string;
  draft: EndpointDraft;
  original: MemoryEndpointConfig;
  onChange: (next: EndpointDraft) => void;
  disabled: boolean;
  identityHint?: string;
  canClearKey: boolean;
  clearKeyLabel?: string;
  showProvider?: boolean;
}> = ({
  title,
  help,
  helpLabel,
  draft,
  original,
  onChange,
  disabled,
  identityHint,
  canClearKey,
  clearKeyLabel,
  showProvider = false,
}) => {
  const { t } = useTranslation();
  const clearLabel = clearKeyLabel ?? t('memory.settings.clearKeyLabel');
  const provider = showProvider ? normalizeRerankProvider(draft.provider) : DEFAULT_MEMORY_RERANK_PROVIDER;
  const hints = showProvider ? RERANK_FIELD_HINTS[provider] : null;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold text-foreground">{title}</span>
        <InfoHint label={helpLabel} content={help} />
        {original.has_api_key ? (
          <Badge variant="success">{t('memory.settings.apiKeySet')}</Badge>
        ) : (
          <Badge variant="destructive">{t('memory.settings.apiKeyNotSet')}</Badge>
        )}
      </div>
      {identityHint ? <p className="text-[11.5px] leading-snug text-muted">{identityHint}</p> : null}
      {showProvider ? (
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.rerankProvider')}</Label>
          <Select
            value={provider}
            disabled={disabled}
            aria-label={t('memory.settings.rerankProvider')}
            onChange={(event) => {
              const nextProvider = normalizeRerankProvider(event.target.value);
              const nextModel = nextProvider === 'dashscope'
                ? DASHSCOPE_RERANK_MODEL
                : draft.model === DASHSCOPE_RERANK_MODEL
                  ? ''
                  : draft.model;
              onChange({ ...draft, provider: nextProvider, model: nextModel });
            }}
          >
            {MEMORY_RERANK_PROVIDERS.map((value) => (
              <option key={value} value={value}>
                {t(`memory.settings.rerankProviders.${value}`)}
              </option>
            ))}
          </Select>
        </div>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.baseUrl')}</Label>
          <Input
            value={draft.baseUrl}
            disabled={disabled}
            placeholder={hints?.baseUrl ?? t('memory.settings.baseUrlPlaceholder')}
            onChange={(e) => onChange({ ...draft, baseUrl: e.target.value })}
            className="text-[13px]"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.model')}</Label>
          <Input
            value={draft.model}
            disabled={disabled || provider === 'dashscope'}
            placeholder={hints?.model ?? t('memory.settings.modelPlaceholder')}
            onChange={(e) => onChange({ ...draft, model: e.target.value })}
            className="text-[13px]"
          />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label className="text-[12px] text-muted">{t('memory.settings.apiKey')}</Label>
        <Input
          type="password"
          autoComplete="off"
          value={draft.apiKey}
          disabled={disabled || draft.clearKey}
          placeholder={original.has_api_key
            ? t('memory.settings.apiKeyPlaceholderSet')
            : t('memory.settings.apiKeyPlaceholder')}
          onChange={(e) => onChange({ ...draft, apiKey: e.target.value, clearKey: false })}
          className="text-[13px]"
        />
        {canClearKey && original.has_api_key ? (
          <button
            type="button"
            role="checkbox"
            aria-checked={draft.clearKey}
            aria-label={clearLabel}
            disabled={disabled}
            onClick={() => onChange({ ...draft, clearKey: !draft.clearKey, apiKey: '' })}
            className="mt-0.5 flex w-fit items-center gap-2 text-[11.5px] text-muted disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Checkbox
              presentational
              checked={draft.clearKey}
              disabled={disabled}
              className="size-3.5"
            />
            {clearLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
};

const identityChanged = (draft: EndpointDraft, original: MemoryEndpointConfig): boolean => {
  const normalize = (value: string | null | undefined): string | null => value?.trim() || null;
  return (
    normalize(draft.baseUrl) !== normalize(original.base_url)
    || normalize(draft.model) !== normalize(original.model)
  );
};

export const MemorySettingsPanel: React.FC<{
  settings: MemorySettings;
  maintenance: MemoryMaintenance | null;
  maintenanceError: string | null;
  dependencyReady: boolean;
  rebuildBusy?: boolean;
  repairBusy?: boolean;
  mutationBusy?: boolean;
  onRebuildBusyChange?: (busy: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onSaved: (next: MemorySettingsOk) => void;
  onReloadSettings: () => void;
  onReloadMaintenance: () => void;
  onClearAll: () => void;
  clearing: boolean;
}> = ({
  settings,
  maintenance,
  maintenanceError,
  dependencyReady,
  rebuildBusy = false,
  repairBusy = false,
  mutationBusy = false,
  onRebuildBusyChange,
  onSavingChange,
  onSaved,
  onReloadSettings,
  onReloadMaintenance,
  onClearAll,
  clearing,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [enabledDraft, setEnabledDraft] = useState(settings.enabled);
  const [modeDraft, setModeDraft] = useState<MemorySettings['mode']>(settings.mode);
  const [llmDraft, setLlmDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.llm));
  const [embeddingDraft, setEmbeddingDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.embedding));
  const [rerankDraft, setRerankDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.rerank ?? EMPTY_ENDPOINT, { includeProvider: true }));
  const [multimodalDraft, setMultimodalDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.multimodal ?? EMPTY_ENDPOINT));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmRebuildOpen, setConfirmRebuildOpen] = useState(false);
  const [pendingPatch, setPendingPatch] = useState<MemorySettingsPatch | null>(null);

  // Reset drafts whenever a fresh settings snapshot lands (initial load or after a save).
  useEffect(() => {
    setEnabledDraft(settings.enabled);
    setModeDraft(settings.mode);
    setLlmDraft(draftFromConfig(settings.processing.llm));
    setEmbeddingDraft(draftFromConfig(settings.processing.embedding));
    setRerankDraft(draftFromConfig(settings.processing.rerank ?? EMPTY_ENDPOINT, { includeProvider: true }));
    setMultimodalDraft(draftFromConfig(settings.processing.multimodal ?? EMPTY_ENDPOINT));
  }, [settings]);

  const rebuildRequired = settings.rebuild_required === true;
  const factoryResetRequired = settings.factory_reset_required === true;
  const canClearRequiredKeys = !enabledDraft;
  const canClearMemory = maintenance?.can_clear === true;
  const busy = saving || rebuildBusy || repairBusy || mutationBusy;
  const customMode = modeDraft === 'custom';

  const buildPatch = (): MemorySettingsPatch => {
    const patch: MemorySettingsPatch = {};
    if (enabledDraft !== settings.enabled) patch.enabled = enabledDraft;
    if (modeDraft !== settings.mode && modeDraft !== 'organization') patch.mode = modeDraft;
    if (!customMode) return patch;
    // Required keys can clear only while the resulting state stays disabled.
    const allowRequiredClear = !enabledDraft;
    const llmPatch = buildEndpointPatch(
      llmDraft,
      settings.processing.llm,
      allowRequiredClear,
    );
    // Identity fields stay editable even when data exists; rebuild confirmation
    // is the safety gate instead of a silent lock.
    const embeddingPatch = buildEndpointPatch(
      embeddingDraft,
      settings.processing.embedding,
      allowRequiredClear,
      false,
    );
    const rerankPatch = buildEndpointPatch(
      rerankDraft,
      settings.processing.rerank ?? EMPTY_ENDPOINT,
      true,
      false,
      true,
      true,
    );
    const multimodalPatch = settings.im_attachment_capture_available === true
      ? buildEndpointPatch(
        multimodalDraft,
        settings.processing.multimodal ?? EMPTY_ENDPOINT,
        true,
        false,
        true,
      )
      : null;
    if (llmPatch || embeddingPatch || rerankPatch || multimodalPatch) {
      patch.processing = {};
      if (llmPatch) patch.processing.llm = llmPatch;
      if (embeddingPatch) patch.processing.embedding = embeddingPatch;
      if (rerankPatch) patch.processing.rerank = rerankPatch;
      if (multimodalPatch) patch.processing.multimodal = multimodalPatch;
    }
    return patch;
  };

  const submitPatch = async (patch: MemorySettingsPatch) => {
    setSaving(true);
    onSavingChange?.(true);
    setError(null);
    const needsRebuild = Boolean(patch.confirm_rebuild);
    if (needsRebuild) onRebuildBusyChange?.(true);
    try {
      const res = await api.saveMemorySettings(patch);
      if (isMemoryOk(res)) {
        onSaved(res);
        const runtime = res.runtime as { ok?: boolean; error?: string } | undefined;
        // Ordinary reconcile also returns runtime.ok; only a confirmed rebuild
        // (needsRebuild) or Retry path should announce rebuild outcomes.
        if (needsRebuild && runtime && typeof runtime.ok === 'boolean') {
          showToast(
            runtime.ok
              ? t('memory.settings.rebuildCompleted')
              : memoryErrorMessage(t, runtime.error || 'memory_rebuild_failed'),
            runtime.ok ? 'success' : 'error',
          );
        } else {
          showToast(t('memory.settings.saved'), 'success');
        }
        setConfirmRebuildOpen(false);
        setPendingPatch(null);
      } else {
        const failure = res as {
          error?: string;
          diagnostic?: {
            message?: string;
            http_status?: number | null;
            provider_error_code?: string | null;
          };
          rebuild_required?: boolean;
        };
        setError(
          memoryErrorMessage(
            t,
            failure.error,
            failure.diagnostic?.message,
            failure.diagnostic?.http_status,
            failure.diagnostic?.provider_error_code,
          ),
        );
        if (!customMode && typeof patch.enabled === 'boolean') {
          setEnabledDraft(settings.enabled);
        }
        // Confirmed rebuild keeps the candidate on failure. Exit the confirm
        // modal so a second click cannot re-submit a now-non-identity patch;
        // Retry rebuild is the recovery control under the pending marker.
        setConfirmRebuildOpen(false);
        setPendingPatch(null);
        const validationFailure =
          failure.error === 'memory_embedding_unavailable' ||
          failure.error === 'memory_llm_unavailable' ||
          failure.error === 'memory_rerank_unavailable' ||
          failure.error === 'memory_multimodal_unavailable';
        if (!validationFailure || failure.rebuild_required === true) {
          onReloadSettings();
          onReloadMaintenance();
        }
      }
    } catch {
      setError(t('memory.settings.saveFailed'));
      if (!customMode && typeof patch.enabled === 'boolean') {
        setEnabledDraft(settings.enabled);
      }
      setConfirmRebuildOpen(false);
      setPendingPatch(null);
      onReloadSettings();
      onReloadMaintenance();
    } finally {
      setSaving(false);
      onSavingChange?.(false);
      if (needsRebuild) onRebuildBusyChange?.(false);
    }
  };

  const save = async () => {
    const patch = buildPatch();
    if (Object.keys(patch).length === 0) {
      showToast(t('memory.settings.saved'), 'success');
      return;
    }
    if (
      (
        modeDraft !== settings.mode
        || identityChanged(embeddingDraft, settings.processing.embedding)
      )
      && !factoryResetRequired
    ) {
      // Retain the draft and open confirmation; the same patch is replayed with
      // confirm_rebuild: true after the user accepts the cost/duration disclosure.
      setPendingPatch(patch);
      setConfirmRebuildOpen(true);
      return;
    }
    await submitPatch(patch);
  };

  const confirmRebuild = async () => {
    if (!pendingPatch) return;
    await submitPatch({ ...pendingPatch, confirm_rebuild: true });
  };

  const retryRebuild = async () => {
    onRebuildBusyChange?.(true);
    setError(null);
    try {
      const res = await api.rebuildMemoryRuntime();
      if (res.ok) {
        showToast(t('memory.settings.rebuildCompleted'), 'success');
        onReloadSettings();
        onReloadMaintenance();
      } else {
        setError(
          memoryErrorMessage(
            t,
            res.error,
            res.diagnostic?.message,
            res.diagnostic?.http_status,
            res.diagnostic?.provider_error_code,
          ),
        );
        onReloadSettings();
        onReloadMaintenance();
      }
    } catch {
      setError(t('memory.settings.rebuildFailed'));
      onReloadSettings();
      onReloadMaintenance();
    } finally {
      onRebuildBusyChange?.(false);
    }
  };

  const setMemoryEnabled = (checked: boolean) => {
    setEnabledDraft(checked);
    if (!customMode) void submitPatch({ enabled: checked });
  };

  const usePlatformMode = () => {
    if (settings.mode === 'platform') {
      setModeDraft('platform');
      return;
    }
    setPendingPatch({ mode: 'platform' });
    setConfirmRebuildOpen(true);
  };

  const acknowledgeOrganizationTransition = () => {
    setPendingPatch({ acknowledge_transition: true });
    setConfirmRebuildOpen(true);
  };

  return (
    <div className="flex flex-col gap-4">
      {rebuildRequired ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gold/40 bg-gold/10 px-4 py-3">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.rebuildRequiredTitle')}</span>
            <span className="text-[12px] leading-snug text-muted">{t('memory.settings.rebuildRequiredDescription')}</span>
          </div>
          {!factoryResetRequired && !settings.transition_notice_pending ? (
            <Button variant="secondary" size="sm" onClick={() => void retryRebuild()} disabled={busy}>
              {rebuildBusy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {rebuildBusy ? t('memory.settings.retryingRebuild') : t('memory.settings.retryRebuild')}
            </Button>
          ) : null}
        </div>
      ) : null}

      {settings.transition_notice_pending ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gold/40 bg-gold/10 px-4 py-3">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.organizationTransitionTitle')}</span>
            <span className="text-[12px] leading-snug text-muted">{t('memory.settings.organizationTransitionDescription')}</span>
          </div>
          <Button
            size="sm"
            onClick={acknowledgeOrganizationTransition}
            disabled={busy || !settings.cloud_available}
          >
            {t('memory.settings.organizationTransitionAction')}
          </Button>
        </div>
      ) : null}

      {settings.capability_paused ? (
        <div className="rounded-xl border border-gold/40 bg-gold/10 px-4 py-3">
          <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.cloudPausedTitle')}</span>
          <p className="mt-1 text-[12px] leading-snug text-muted">{t('memory.settings.cloudPausedDescription')}</p>
        </div>
      ) : null}

      <div className="flex items-start justify-between gap-4 rounded-xl border border-border bg-surface px-4 py-3.5">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.enableLabel')}</span>
          <span className="text-[11.5px] leading-snug text-muted">{t('memory.settings.enableHint')}</span>
          {!dependencyReady && !enabledDraft ? (
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11.5px] text-gold-ink">
              <ShieldAlert className="size-3.5 shrink-0" />
              {t('memory.settings.dependencyNotReady')}
              <Button asChild variant="secondary" size="xs">
                <Link to="/settings/dependencies">
                  {t('memory.settings.goToDependencies')}
                  <ArrowUpRight className="size-3.5" />
                </Link>
              </Button>
            </div>
          ) : null}
        </div>
        <Switch
          checked={enabledDraft}
          onCheckedChange={setMemoryEnabled}
          disabled={
            busy
            || factoryResetRequired
            || rebuildRequired
            || (!enabledDraft && !dependencyReady)
            || (!enabledDraft && !customMode && !settings.cloud_available)
          }
          label={t('memory.settings.enableLabel')}
        />
      </div>

      {modeDraft === 'platform' ? (
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-3.5">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-mint/15 text-mint-ink">
                <Cloud className="size-5" />
              </span>
              <div className="min-w-0">
                <h3 className="text-[13px] font-semibold text-foreground">{t('memory.settings.avibeCloudTitle')}</h3>
                <p className="text-[11.5px] leading-snug text-muted">{t('memory.settings.avibeCloudIncluded')}</p>
              </div>
            </div>
            <Badge variant="success">{t('memory.settings.avibeCloudFree')}</Badge>
          </div>
          <p className="px-4 py-4 text-[12px] leading-relaxed text-muted">{t('memory.settings.avibeCloudDescription')}</p>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border bg-background/40 px-4 py-3">
            <span className="text-[11.5px] text-muted">{t('memory.settings.customPrompt')}</span>
            <Button variant="secondary" size="sm" onClick={() => setModeDraft('custom')} disabled={busy}>
              {t('memory.settings.useCustomEndpoints')}
            </Button>
          </div>
        </div>
      ) : null}

      {modeDraft === 'organization' ? (
        <div className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-4">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-cyan/15 text-cyan-ink">
            <Building2 className="size-5" />
          </span>
          <span className="text-[13px] font-medium text-foreground">{t('memory.settings.organizationManaged')}</span>
        </div>
      ) : null}

      {customMode ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface px-4 py-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-violet/15 text-violet-ink">
                <SlidersHorizontal className="size-5" />
              </span>
              <div className="min-w-0">
                <h3 className="text-[13px] font-semibold text-foreground">{t('memory.settings.customEndpointsTitle')}</h3>
                <p className="text-[11.5px] leading-snug text-muted">{t('memory.settings.customEndpointsDescription')}</p>
              </div>
            </div>
            {settings.cloud_available && !settings.managed ? (
              <Button variant="secondary" size="sm" onClick={usePlatformMode} disabled={busy}>
                {t('memory.settings.useAvibeCloud')}
              </Button>
            ) : null}
          </div>

          <EndpointFields
            title={t('memory.settings.llmTitle')}
            help={t('memory.settings.llmHelp')}
            helpLabel={t('memory.settings.llmHelpLabel')}
            draft={llmDraft}
            original={settings.processing.llm}
            onChange={setLlmDraft}
            disabled={busy}
            canClearKey={canClearRequiredKeys}
          />

          <EndpointFields
            title={t('memory.settings.embeddingTitle')}
            help={t('memory.settings.embeddingHelp')}
            helpLabel={t('memory.settings.embeddingHelpLabel')}
            draft={embeddingDraft}
            original={settings.processing.embedding}
            onChange={setEmbeddingDraft}
            disabled={busy}
            identityHint={t('memory.settings.embeddingIdentityHint')}
            canClearKey={canClearRequiredKeys}
          />

          <EndpointFields
            title={t('memory.settings.rerankTitle')}
            help={t('memory.settings.rerankHelp')}
            helpLabel={t('memory.settings.rerankHelpLabel')}
            draft={rerankDraft}
            original={settings.processing.rerank ?? EMPTY_ENDPOINT}
            onChange={setRerankDraft}
            disabled={busy}
            identityHint={t('memory.settings.rerankIdentityHint')}
            canClearKey
            clearKeyLabel={t('memory.settings.rerankClearLabel')}
            showProvider
          />

          {settings.im_attachment_capture_available === true ? (
            <EndpointFields
              title={t('memory.settings.multimodalTitle')}
              help={t('memory.settings.multimodalHelp')}
              helpLabel={t('memory.settings.multimodalHelpLabel')}
              draft={multimodalDraft}
              original={settings.processing.multimodal ?? EMPTY_ENDPOINT}
              onChange={setMultimodalDraft}
              disabled={busy}
              identityHint={t('memory.settings.multimodalIdentityHint')}
              canClearKey
              clearKeyLabel={t('memory.settings.multimodalClearLabel')}
            />
          ) : null}
        </>
      ) : null}

      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{error}</div>
      ) : null}

      {maintenanceError ? (
        <div className="rounded-xl border border-border bg-surface px-4 py-3 text-[12px] text-muted">{maintenanceError}</div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        {customMode ? (
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={() => void save()} disabled={busy}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {saving ? t('memory.settings.saving') : t('memory.settings.save')}
            </Button>
          </div>
        ) : null}
        <Button
          className={customMode ? undefined : 'ml-auto'}
          variant="destructive"
          size="sm"
          onClick={onClearAll}
          disabled={clearing || !canClearMemory || busy || factoryResetRequired}
        >
          <Trash2 className="size-3.5" />
          {t('memory.clear.button')}
        </Button>
      </div>

      <div className="rounded-xl border border-border bg-surface p-4">
        <h3 className="mb-2 text-[13px] font-semibold text-foreground">{t('memory.settings.disclosureTitle')}</h3>
        <ul className="flex flex-col gap-1.5">
          {(t(
            customMode ? 'memory.settings.disclosure' : 'memory.settings.cloudDisclosure',
            { returnObjects: true },
          ) as string[]).map((line, idx) => (
            <li key={idx} className="flex gap-2 text-[11.5px] leading-snug text-muted">
              <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
              {line}
            </li>
          ))}
          {settings.im_attachment_capture_available === true ? (
            <li className="flex gap-2 text-[11.5px] leading-snug text-muted">
              <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
              {t(
                customMode
                  ? 'memory.settings.disclosureAttachment'
                  : 'memory.settings.cloudDisclosureAttachment',
              )}
            </li>
          ) : null}
        </ul>
      </div>

      <ConfirmDialog
        open={confirmRebuildOpen}
        onOpenChange={(open) => {
          setConfirmRebuildOpen(open);
          if (!open) setPendingPatch(null);
        }}
        title={t('memory.settings.rebuildConfirmTitle')}
        description={t('memory.settings.rebuildConfirmDescription')}
        confirmLabel={t('memory.settings.rebuildConfirmLabel')}
        onConfirm={confirmRebuild}
      />
    </div>
  );
};
