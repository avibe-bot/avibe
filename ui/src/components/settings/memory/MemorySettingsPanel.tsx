import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Loader2, RefreshCw, ShieldAlert, Trash2 } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Checkbox } from '../../ui/checkbox';
import { ConfirmDialog } from '../../ui/confirm-dialog';
import { InfoHint } from '../../ui/info-hint';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { Switch } from '../../ui/switch';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryEndpointConfig,
  MemoryMaintenance,
  MemorySettings,
  MemorySettingsPatch,
  MemorySettingsResult,
} from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { buildEndpointPatch, draftFromConfig } from '../../../lib/memorySettings';
import type { EndpointDraft } from '../../../lib/memorySettings';
import { isMemoryOk, memoryErrorMessage } from '../../../lib/memoryRead';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;

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
}) => {
  const { t } = useTranslation();
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
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.baseUrl')}</Label>
          <Input
            value={draft.baseUrl}
            disabled={disabled}
            placeholder={t('memory.settings.baseUrlPlaceholder')}
            onChange={(e) => onChange({ ...draft, baseUrl: e.target.value })}
            className="text-[13px]"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.model')}</Label>
          <Input
            value={draft.model}
            disabled={disabled}
            placeholder={t('memory.settings.modelPlaceholder')}
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
            aria-label={t('memory.settings.clearKeyLabel')}
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
            {t('memory.settings.clearKeyLabel')}
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
  const [llmDraft, setLlmDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.llm));
  const [embeddingDraft, setEmbeddingDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.embedding));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmRebuildOpen, setConfirmRebuildOpen] = useState(false);
  const [pendingPatch, setPendingPatch] = useState<MemorySettingsPatch | null>(null);

  // Reset drafts whenever a fresh settings snapshot lands (initial load or after a save).
  useEffect(() => {
    setEnabledDraft(settings.enabled);
    setLlmDraft(draftFromConfig(settings.processing.llm));
    setEmbeddingDraft(draftFromConfig(settings.processing.embedding));
  }, [settings]);

  const rebuildRequired = settings.rebuild_required === true;
  const factoryResetRequired = settings.factory_reset_required === true;
  const canClearKeys = !enabledDraft;
  const canClearMemory = maintenance?.can_clear === true;
  const busy = saving || rebuildBusy || repairBusy || mutationBusy;

  const buildPatch = (): MemorySettingsPatch => {
    const patch: MemorySettingsPatch = {};
    if (enabledDraft !== settings.enabled) patch.enabled = enabledDraft;
    // A key clear is accepted only while the resulting state stays disabled.
    const allowClear = !enabledDraft;
    const llmPatch = buildEndpointPatch(llmDraft, settings.processing.llm, allowClear);
    // Identity fields stay editable even when data exists; rebuild confirmation
    // is the safety gate instead of a silent lock.
    const embeddingPatch = buildEndpointPatch(
      embeddingDraft,
      settings.processing.embedding,
      allowClear,
      false,
    );
    if (llmPatch || embeddingPatch) {
      patch.processing = {};
      if (llmPatch) patch.processing.llm = llmPatch;
      if (embeddingPatch) patch.processing.embedding = embeddingPatch;
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
        // Confirmed rebuild keeps the candidate on failure. Exit the confirm
        // modal so a second click cannot re-submit a now-non-identity patch;
        // Retry rebuild is the recovery control under the pending marker.
        setConfirmRebuildOpen(false);
        setPendingPatch(null);
        const validationFailure =
          failure.error === 'memory_embedding_unavailable' ||
          failure.error === 'memory_llm_unavailable';
        if (!validationFailure || failure.rebuild_required === true) {
          onReloadSettings();
          onReloadMaintenance();
        }
      }
    } catch {
      setError(t('memory.settings.saveFailed'));
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
      identityChanged(embeddingDraft, settings.processing.embedding)
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

  return (
    <div className="flex flex-col gap-4">
      {rebuildRequired ? (
        <div className="flex flex-col gap-1 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3">
          <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.rebuildRequiredTitle')}</span>
          <span className="text-[12px] leading-snug text-muted">{t('memory.settings.rebuildRequiredDescription')}</span>
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
                <Link to="/admin/settings/dependencies">
                  {t('memory.settings.goToDependencies')}
                  <ArrowUpRight className="size-3.5" />
                </Link>
              </Button>
            </div>
          ) : null}
        </div>
        <Switch
          checked={enabledDraft}
          onCheckedChange={setEnabledDraft}
          disabled={busy || factoryResetRequired || rebuildRequired || (!enabledDraft && !dependencyReady)}
          label={t('memory.settings.enableLabel')}
        />
      </div>

      <EndpointFields
        title={t('memory.settings.llmTitle')}
        help={t('memory.settings.llmHelp')}
        helpLabel={t('memory.settings.llmHelpLabel')}
        draft={llmDraft}
        original={settings.processing.llm}
        onChange={setLlmDraft}
        disabled={busy}
        canClearKey={canClearKeys}
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
        canClearKey={canClearKeys}
      />

      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{error}</div>
      ) : null}

      {maintenanceError ? (
        <div className="rounded-xl border border-border bg-surface px-4 py-3 text-[12px] text-muted">{maintenanceError}</div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void save()} disabled={busy}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {saving ? t('memory.settings.saving') : t('memory.settings.save')}
          </Button>
          {rebuildRequired && !factoryResetRequired ? (
            <Button variant="secondary" onClick={() => void retryRebuild()} disabled={busy}>
              {rebuildBusy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {rebuildBusy ? t('memory.settings.retryingRebuild') : t('memory.settings.retryRebuild')}
            </Button>
          ) : null}
        </div>
        <Button
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
          {(t('memory.settings.disclosure', { returnObjects: true }) as string[]).map((line, idx) => (
            <li key={idx} className="flex gap-2 text-[11.5px] leading-snug text-muted">
              <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
              {line}
            </li>
          ))}
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
