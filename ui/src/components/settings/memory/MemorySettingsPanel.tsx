import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Loader2, Lock, ShieldAlert, Trash2 } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Checkbox } from '../../ui/checkbox';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { Switch } from '../../ui/switch';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryEndpointConfig,
  MemorySettings,
  MemorySettingsPatch,
  MemorySettingsResult,
  MemoryStatus,
} from '../../../context/ApiContext';
import { useToast } from '../../../context/ToastContext';
import { buildEndpointPatch, draftFromConfig } from '../../../lib/memorySettings';
import type { EndpointDraft } from '../../../lib/memorySettings';
import { isMemoryOk, memoryErrorMessage } from '../../../lib/memoryRead';

type MemorySettingsOk = Extract<MemorySettingsResult, { status: 'ok' }>;

// One LLM/embedding endpoint's fields. `locked` disables base_url/model edits — used for the
// embedding endpoint once memory data exists, because changing it would mix vector spaces.
const EndpointFields: React.FC<{
  title: string;
  draft: EndpointDraft;
  original: MemoryEndpointConfig;
  onChange: (next: EndpointDraft) => void;
  disabled: boolean;
  locked: boolean;
  lockedHint?: string;
  canClearKey: boolean;
}> = ({ title, draft, original, onChange, disabled, locked, lockedHint, canClearKey }) => {
  const { t } = useTranslation();
  const identityFieldsDisabled = disabled || locked;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold text-foreground">{title}</span>
        {original.has_api_key ? (
          <Badge variant="success">{t('memory.settings.apiKeySet')}</Badge>
        ) : (
          <Badge variant="secondary">{t('memory.settings.apiKeyNotSet')}</Badge>
        )}
        {locked ? (
          <Badge variant="warning" className="gap-1">
            <Lock className="size-3" />
            {t('common.locked')}
          </Badge>
        ) : null}
      </div>
      {lockedHint ? <p className="text-[11.5px] leading-snug text-muted">{lockedHint}</p> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.baseUrl')}</Label>
          <Input
            value={draft.baseUrl}
            disabled={identityFieldsDisabled}
            placeholder={t('memory.settings.baseUrlPlaceholder')}
            onChange={(e) => onChange({ ...draft, baseUrl: e.target.value })}
            className="text-[13px]"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.model')}</Label>
          <Input
            value={draft.model}
            disabled={identityFieldsDisabled}
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
          placeholder={t('memory.settings.apiKeyPlaceholder')}
          onChange={(e) => onChange({ ...draft, apiKey: e.target.value, clearKey: false })}
          className="text-[13px]"
        />
        <p className="text-[11px] text-muted">{t('memory.settings.apiKeyClearHint')}</p>
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

export const MemorySettingsPanel: React.FC<{
  settings: MemorySettings;
  status: MemoryStatus | null;
  dependencyReady: boolean;
  onSaved: (next: MemorySettingsOk) => void;
  onReloadStatus: () => void;
  onClearAll: () => void;
  clearing: boolean;
}> = ({ settings, status, dependencyReady, onSaved, onReloadStatus, onClearAll, clearing }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [enabledDraft, setEnabledDraft] = useState(settings.enabled);
  const [llmDraft, setLlmDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.llm));
  const [embeddingDraft, setEmbeddingDraft] = useState<EndpointDraft>(() => draftFromConfig(settings.processing.embedding));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset drafts whenever a fresh settings snapshot lands (initial load or after a save).
  useEffect(() => {
    setEnabledDraft(settings.enabled);
    setLlmDraft(draftFromConfig(settings.processing.llm));
    setEmbeddingDraft(draftFromConfig(settings.processing.embedding));
  }, [settings]);

  // `data_exists` is only known once status resolves. Settings can render first (the two loads run
  // concurrently), so until status is known we must NOT let the embedding endpoint be edited: a
  // change made in that window would be silently discarded once the lock activates yet still report
  // success. Fail closed — treat the embedding endpoint as locked while status is unknown, and only
  // unlock it after a resolved status reports data_exists=false.
  const statusKnown = status != null;
  // Data already exists in the local Memory root: changing the embedding endpoint/model would mix
  // vector spaces, so the backend rejects it; lock those fields here too, proactively.
  const embeddingDataLock = !!status?.data_exists;
  const embeddingLocked = !statusKnown || embeddingDataLock;
  const canClearKeys = !enabledDraft;

  // If data_exists transitions to true while the user has an unsaved embedding draft
  // (e.g. they edited it while data_exists was false, then a poll reports data_exists
  // true), discard that draft back to the persisted settings. Otherwise save() would
  // drop the embedding patch (locked) yet report success — a silent discard.
  useEffect(() => {
    if (embeddingDataLock) {
      setEmbeddingDraft((draft) => ({
        ...draftFromConfig(settings.processing.embedding),
        apiKey: draft.apiKey,
        clearKey: draft.clearKey,
      }));
    }
  }, [embeddingDataLock, settings]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const patch: MemorySettingsPatch = {};
      if (enabledDraft !== settings.enabled) patch.enabled = enabledDraft;
      // A key clear is accepted only while the resulting state stays disabled.
      const allowClear = !enabledDraft;
      const llmPatch = buildEndpointPatch(llmDraft, settings.processing.llm, allowClear);
      const embeddingPatch = buildEndpointPatch(
        embeddingDraft,
        settings.processing.embedding,
        allowClear,
        embeddingLocked,
      );
      if (llmPatch || embeddingPatch) {
        patch.processing = {};
        if (llmPatch) patch.processing.llm = llmPatch;
        if (embeddingPatch) patch.processing.embedding = embeddingPatch;
      }
      if (Object.keys(patch).length === 0) {
        showToast(t('memory.settings.saved'), 'success');
        return;
      }
      const res = await api.saveMemorySettings(patch);
      if (isMemoryOk(res)) {
        onSaved(res);
        showToast(t('memory.settings.saved'), 'success');
      } else {
        setError(memoryErrorMessage(t, (res as { error?: string })?.error));
        // A failed enable did not persist — revert the toggle to the stored state so it reflects
        // reality, and refresh status so a runtime-dependency blocker (and its Dependencies
        // affordance) reappears instead of a stale "enabled" toggle hiding it.
        setEnabledDraft(settings.enabled);
        onReloadStatus();
      }
    } catch {
      setError(t('memory.settings.saveFailed'));
      setEnabledDraft(settings.enabled);
      onReloadStatus();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-4 rounded-xl border border-border bg-surface px-4 py-3.5">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-[13px] font-semibold text-foreground">{t('memory.settings.enableLabel')}</span>
          <span className="text-[11.5px] leading-snug text-muted">{t('memory.settings.enableHint')}</span>
          {!dependencyReady && !enabledDraft ? (
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11.5px] text-gold">
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
          disabled={saving || (!enabledDraft && !dependencyReady)}
          label={t('memory.settings.enableLabel')}
        />
      </div>

      <EndpointFields
        title={t('memory.settings.llmTitle')}
        draft={llmDraft}
        original={settings.processing.llm}
        onChange={setLlmDraft}
        disabled={saving}
        locked={false}
        canClearKey={canClearKeys}
      />
      <EndpointFields
        title={t('memory.settings.embeddingTitle')}
        draft={embeddingDraft}
        original={settings.processing.embedding}
        onChange={setEmbeddingDraft}
        disabled={saving}
        locked={embeddingLocked}
        // Distinguish the two lock reasons: data-exists (permanent until Clear all) vs status not
        // yet resolved (transient — re-enables once status confirms no data exists).
        lockedHint={
          embeddingDataLock
            ? t('memory.settings.embeddingLocked')
            : !statusKnown
              ? t('memory.settings.embeddingStatusPending')
              : undefined
        }
        canClearKey={canClearKeys}
      />

      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : null}

      <div className="flex items-center justify-between gap-3">
        <Button onClick={() => void save()} disabled={saving}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {saving ? t('memory.settings.saving') : t('memory.settings.save')}
        </Button>
        <Button variant="destructive" size="sm" onClick={onClearAll} disabled={clearing}>
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
    </div>
  );
};
