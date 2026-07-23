import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  AlertTriangle,
  ArrowUpRight,
  Brain,
  Clock,
  Database,
  Loader2,
  Lock,
  RefreshCw,
  Search as SearchIcon,
  ShieldAlert,
  Trash2,
} from 'lucide-react';

import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Card, CardContent } from '../ui/card';
import { Checkbox } from '../ui/checkbox';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { SegmentedRadio } from '../ui/segmented';
import { Switch } from '../ui/switch';
import { useApi } from '../../context/ApiContext';
import type {
  MemoryEndpointConfig,
  MemoryEndpointPatch,
  MemoryItem,
  MemorySettings,
  MemorySettingsPatch,
  MemoryStatus,
} from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';

type MemoryTab = 'status' | 'profile' | 'search' | 'settings';

// Status precedence mirrors the backend contract exactly (tech §15) — this map
// is display-only; the actual precedence is computed server-side.
const STATE_BADGE_VARIANT: Record<MemoryStatus['state'], 'success' | 'warning' | 'destructive' | 'info' | 'secondary'> = {
  disabled: 'secondary',
  starting: 'info',
  ready: 'success',
  indexing: 'info',
  degraded: 'warning',
  down: 'destructive',
  clearing: 'warning',
  error: 'destructive',
};

const POLL_MS = 4000;

const errorMessage = (t: TFunction, code: string | null | undefined): string =>
  code ? t(`errors.${code}`, { defaultValue: code }) : t('common.unknown');

// Backend forbidden path (`_memory_forbidden_response`) returns exactly this closed shape for
// every Memory route when the request isn't direct-loopback (e.g. opened via Avibe Cloud). It is
// otherwise never produced by a settings/status/profile/search/clear success or config-disabled
// path, so it's a safe signal to render the "available on this device only" static state instead
// of a generic error (plan §7).
const isForbiddenResult = (value: unknown): boolean =>
  !!value &&
  typeof value === 'object' &&
  (value as { status?: string; error?: string }).status === 'failed' &&
  (value as { status?: string; error?: string }).error === 'memory_disabled';

const isSettingsSuccess = (value: unknown): value is MemorySettings =>
  !!value && typeof value === 'object' && typeof (value as { enabled?: unknown }).enabled === 'boolean';

const isStatusSuccess = (value: unknown): value is MemoryStatus =>
  !!value && typeof value === 'object' && typeof (value as { state?: unknown }).state === 'string';

const isItemsSuccess = (
  value: unknown,
): value is { status: 'ok'; items: MemoryItem[]; warnings: string[]; profile_warning?: 'empty' | null } =>
  !!value && typeof value === 'object' && (value as { status?: string }).status === 'ok';

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

type EndpointDraft = { baseUrl: string; model: string; apiKey: string; clearKey: boolean };

const draftFromConfig = (config: MemoryEndpointConfig): EndpointDraft => ({
  baseUrl: config.base_url ?? '',
  model: config.model ?? '',
  apiKey: '',
  clearKey: false,
});

// `allowClear` gates the explicit `api_key: null` clear. Slice 2 rejects a null-key patch when the
// resulting state is enabled (memory_key_clear_while_enabled), so the caller only allows a clear
// while Memory stays disabled — a cleared-then-enabled combo in one save simply keeps the key.
function buildEndpointPatch(
  draft: EndpointDraft,
  original: MemoryEndpointConfig,
  allowClear: boolean,
): MemoryEndpointPatch | undefined {
  const patch: MemoryEndpointPatch = {};
  let changed = false;
  const baseUrl = draft.baseUrl.trim() || null;
  if (baseUrl !== (original.base_url ?? null)) {
    patch.base_url = baseUrl;
    changed = true;
  }
  const model = draft.model.trim() || null;
  if (model !== (original.model ?? null)) {
    patch.model = model;
    changed = true;
  }
  const trimmedKey = draft.apiKey.trim();
  if (trimmedKey) {
    patch.api_key = trimmedKey;
    changed = true;
  } else if (draft.clearKey && allowClear) {
    patch.api_key = null;
    changed = true;
  }
  return changed ? patch : undefined;
}

// One LLM/embedding endpoint's fields. `locked` disables base_url/model edits — used for the
// embedding endpoint once memory data exists (plan §7: changing it would mix vector spaces).
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
  const fieldsDisabled = disabled || locked;
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
            disabled={fieldsDisabled}
            placeholder={t('memory.settings.baseUrlPlaceholder')}
            onChange={(e) => onChange({ ...draft, baseUrl: e.target.value })}
            className="text-[13px]"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="text-[12px] text-muted">{t('memory.settings.model')}</Label>
          <Input
            value={draft.model}
            disabled={fieldsDisabled}
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
          // Locked (embedding + data exists) disables the key field too: the whole embedding
          // patch is discarded on save, so an editable key would falsely report a saved change.
          disabled={fieldsDisabled || draft.clearKey}
          placeholder={t('memory.settings.apiKeyPlaceholder')}
          onChange={(e) => onChange({ ...draft, apiKey: e.target.value, clearKey: false })}
          className="text-[13px]"
        />
        <p className="text-[11px] text-muted">{t('memory.settings.apiKeyClearHint')}</p>
        {canClearKey && original.has_api_key && !locked ? (
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

const StatusPanel: React.FC<{
  status: MemoryStatus | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}> = ({ status, loading, error, onRefresh }) => {
  const { t } = useTranslation();

  if (loading && !status) {
    return (
      <div className="flex items-center gap-2 px-1 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('memory.status.loading')}
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        {error}
      </div>
    );
  }
  if (!status) return null;

  const stats: Array<{ key: string; label: string; value: React.ReactNode }> = [
    { key: 'pending', label: t('memory.status.pending'), value: status.pending },
    { key: 'processing', label: t('memory.status.processingCount'), value: status.processing },
    { key: 'dead', label: t('memory.status.dead'), value: status.dead },
    { key: 'missed', label: t('memory.status.missed'), value: status.missed },
  ];

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardContent className="flex flex-col gap-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Badge variant={STATE_BADGE_VARIANT[status.state]} className="text-[12px]">
                {t(`memory.status.state.${status.state}`)}
              </Badge>
              {status.error ? (
                <span className="flex items-center gap-1 text-[12px] text-destructive">
                  <AlertTriangle className="size-3.5" />
                  {errorMessage(t, status.error)}
                </span>
              ) : null}
            </div>
            <Button variant="ghost" size="sm" onClick={onRefresh}>
              <RefreshCw className="size-3.5" />
              {t('memory.status.refresh')}
            </Button>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {stats.map((s) => (
              <div key={s.key} className="rounded-lg border border-border bg-surface px-3 py-2.5">
                <div className="text-[10px] uppercase tracking-[0.08em] text-muted">{s.label}</div>
                <div className="text-[18px] font-semibold text-foreground">{s.value}</div>
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-2 border-t border-border pt-3 text-[12.5px]">
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5 text-muted">
                <Clock className="size-3.5" />
                {t('memory.status.lastSuccess')}
              </span>
              <span className="font-mono text-foreground">
                {status.last_success_at ? new Date(status.last_success_at).toLocaleString() : t('memory.status.lastSuccessNever')}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5 text-muted">
                <Database className="size-3.5" />
                {t('memory.status.storageUsed')}
              </span>
              <span className="font-mono text-foreground">{formatBytes(status.provider_disk_bytes)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">{t('memory.status.queueBytes')}</span>
              <span className="font-mono text-foreground">{formatBytes(status.queue_plaintext_bytes)}</span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

const ProfilePanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [warning, setWarning] = useState<'empty' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.getMemoryProfile();
      if (isItemsSuccess(res)) {
        // Only a SUCCESSFUL response is the benign Provider-A case: `profile_warning:'empty'`
        // (or simply zero items) renders as the graceful "not available"/empty copy.
        setItems(res.items);
        setWarning(res.profile_warning ?? null);
        setError(null);
      } else {
        // A closed failure — sidecar down, provider outage, timeout, etc. — is a real ERROR, not
        // the accepted empty-profile warning. Surface it distinctly per its code.
        setItems(null);
        setWarning(null);
        setError(errorMessage(t, (res as { error?: string })?.error) || t('memory.profile.loadFailed'));
      }
    } catch {
      setItems(null);
      setWarning(null);
      setError(t('memory.profile.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [api, enabled, t]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.profile.disabledHint')}</div>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12.5px] text-muted">{t('memory.profile.description')}</p>
        <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
          {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
          {t('memory.profile.refresh')}
        </Button>
      </div>
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : loading && items === null ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('memory.profile.loading')}
        </div>
      ) : warning === 'empty' ? (
        <div className="rounded-2xl border border-gold/30 bg-gold/[0.06] p-6 text-center text-[13px] text-foreground">
          {t('memory.profile.warningUnavailable')}
        </div>
      ) : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.profile.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, idx) => (
            // Inert text nodes only (tech §14.1) — never Markdown/HTML rendering of provider content.
            <div key={idx} className="rounded-xl border border-border bg-surface px-4 py-3">
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
              </div>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
            </div>
          ))}
          <p className="px-1 text-[11px] text-muted">{t('memory.profile.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};

const SearchPanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const runSearch = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed) return;
    setSearching(true);
    setError(null);
    try {
      const res = await api.searchMemory(trimmed, 20);
      if (isItemsSuccess(res)) {
        setItems(res.items);
      } else {
        setItems([]);
        setError(errorMessage(t, res.error));
      }
    } catch {
      setError(t('memory.search.searchFailed'));
    } finally {
      setSearching(false);
      setSearched(true);
    }
  }, [api, query, t]);

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.search.disabledHint')}</div>;
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[12.5px] text-muted">{t('memory.search.description')}</p>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <SearchIcon size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void runSearch();
            }}
            placeholder={t('memory.search.placeholder')}
            className="pl-9 text-[13px]"
          />
        </div>
        <Button onClick={() => void runSearch()} disabled={searching || !query.trim()}>
          {searching ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {searching ? t('memory.search.searching') : t('memory.search.button')}
        </Button>
      </div>
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : !searched ? null : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.search.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, idx) => (
            <div key={idx} className="rounded-xl border border-border bg-surface px-4 py-3">
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
              </div>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
            </div>
          ))}
          <p className="px-1 text-[11px] text-muted">{t('memory.search.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};

const SettingsPanel: React.FC<{
  settings: MemorySettings;
  status: MemoryStatus | null;
  dependencyReady: boolean;
  onSaved: (next: MemorySettings) => void;
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
  // vector spaces, so the backend rejects it (plan §7) — lock those fields here too, proactively.
  const embeddingDataLock = !!status?.data_exists;
  const embeddingLocked = !statusKnown || embeddingDataLock;
  const canClearKeys = !enabledDraft;

  // If data_exists transitions to true while the user has an unsaved embedding draft
  // (e.g. they edited it while data_exists was false, then a poll reports data_exists
  // true), discard that draft back to the persisted settings. Otherwise save() would
  // drop the embedding patch (locked) yet report success — a silent discard.
  useEffect(() => {
    if (embeddingDataLock) {
      setEmbeddingDraft(draftFromConfig(settings.processing.embedding));
    }
  }, [embeddingDataLock, settings]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const patch: MemorySettingsPatch = {};
      if (enabledDraft !== settings.enabled) patch.enabled = enabledDraft;
      // A key clear is accepted only while the resulting state stays disabled (Slice 2).
      const allowClear = !enabledDraft;
      const llmPatch = buildEndpointPatch(llmDraft, settings.processing.llm, allowClear);
      // Never build an embedding patch while the endpoint is locked (data exists OR status not yet
      // resolved) — otherwise a change would be dropped here while the save still reports success.
      const embeddingPatch = embeddingLocked
        ? undefined
        : buildEndpointPatch(embeddingDraft, settings.processing.embedding, allowClear);
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
      if (isSettingsSuccess(res)) {
        onSaved(res);
        showToast(t('memory.settings.saved'), 'success');
      } else {
        setError(errorMessage(t, (res as { error?: string })?.error));
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

export const MemoryPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [tab, setTab] = useState<MemoryTab>('status');
  const [remoteUnavailable, setRemoteUnavailable] = useState(false);
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [loadingSettings, setLoadingSettings] = useState(true);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [dependencyReady, setDependencyReady] = useState(true);

  const loadSettings = useCallback(async () => {
    setLoadingSettings(true);
    try {
      const res = await api.getMemorySettings();
      if (isForbiddenResult(res)) {
        setRemoteUnavailable(true);
      } else if (isSettingsSuccess(res)) {
        setSettings(res);
        setSettingsError(null);
      } else {
        setSettingsError(errorMessage(t, (res as { error?: string })?.error));
      }
    } catch {
      setSettingsError(t('memory.settings.loadFailed'));
    } finally {
      setLoadingSettings(false);
    }
  }, [api, t]);

  const loadStatus = useCallback(async () => {
    try {
      const res = await api.getMemoryStatus();
      if (isForbiddenResult(res)) {
        setRemoteUnavailable(true);
      } else if (isStatusSuccess(res)) {
        setStatus(res);
        setStatusError(null);
      } else {
        setStatusError(errorMessage(t, (res as { error?: string })?.error));
      }
    } catch {
      setStatusError(t('memory.status.loadFailed'));
    } finally {
      setLoadingStatus(false);
    }
  }, [api, t]);

  // Dependency readiness comes from the authoritative Dependencies source (plan §5), NOT the
  // memory status: after a failed enable the backend rolls the setting back to disabled and a
  // disabled status omits the runtime error, so status alone would falsely read "ready".
  const loadDependency = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      const dep = res.deps?.find((d) => d.id === 'memory-runtime');
      // Absent row (older backend) → don't block enablement; only a present, non-ready row does.
      if (dep) setDependencyReady(dep.installed && dep.status === 'ready');
    } catch {
      // Best-effort: leave the prior readiness rather than falsely blocking the toggle.
    }
  }, [api]);

  useEffect(() => {
    void loadSettings();
    void loadStatus();
    void loadDependency();
  }, [loadSettings, loadStatus, loadDependency]);

  // Poll status while the page is open so queue/state transitions (starting → ready, clearing →
  // enabled, etc.) show up without a manual refresh. Settings/profile/search stay explicit-refresh.
  const remoteUnavailableRef = useRef(remoteUnavailable);
  remoteUnavailableRef.current = remoteUnavailable;
  useEffect(() => {
    const id = window.setInterval(() => {
      if (!remoteUnavailableRef.current) void loadStatus();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [loadStatus]);

  const confirmClear = async () => {
    setClearing(true);
    try {
      const res = await api.clearMemory();
      if (res.status === 'completed') {
        showToast(t('memory.clear.cleared'), 'success');
        setClearOpen(false);
        void loadStatus();
        void loadSettings();
      } else {
        showToast(errorMessage(t, res.error), 'error');
      }
    } catch {
      showToast(t('memory.clear.failed'), 'error');
    } finally {
      setClearing(false);
    }
  };

  const tabs = useMemo(
    () => [
      { id: 'status' as const, label: t('memory.tabs.status') },
      { id: 'profile' as const, label: t('memory.tabs.profile') },
      { id: 'search' as const, label: t('memory.tabs.search') },
      { id: 'settings' as const, label: t('memory.tabs.settings') },
    ],
    [t],
  );

  return (
    <div className="mx-auto flex w-full max-w-[900px] flex-col gap-5 py-2">
      <CapabilityTabs />
      <WorkbenchPageHeader
        icon={<Brain className="size-6" />}
        accent="violet"
        title={t('memory.title')}
        subtitle={t('memory.subtitle')}
      />

      {remoteUnavailable ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-border bg-surface p-10 text-center">
          <ShieldAlert className="size-6 text-muted" />
          <span className="text-[14px] font-semibold text-foreground">{t('memory.remoteUnavailable.title')}</span>
          <span className="max-w-md text-[12.5px] text-muted">{t('memory.remoteUnavailable.description')}</span>
        </div>
      ) : (
        <>
          <SegmentedRadio value={tab} onChange={setTab} options={tabs} ariaLabel={t('memory.title')} tone="mint" />

          {tab === 'status' && (
            <StatusPanel status={status} loading={loadingStatus} error={statusError} onRefresh={() => void loadStatus()} />
          )}

          {tab === 'profile' && <ProfilePanel enabled={!!settings?.enabled} />}

          {tab === 'search' && <SearchPanel enabled={!!settings?.enabled} />}

          {tab === 'settings' &&
            (loadingSettings && !settings ? (
              <div className="flex items-center gap-2 px-1 text-sm text-muted">
                <Loader2 className="size-4 animate-spin" />
                {t('memory.settings.loading')}
              </div>
            ) : settingsError && !settings ? (
              <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {settingsError}
              </div>
            ) : settings ? (
              <SettingsPanel
                settings={settings}
                status={status}
                dependencyReady={dependencyReady}
                onSaved={(next) => {
                  setSettings(next);
                  void loadStatus();
                  void loadDependency();
                }}
                onReloadStatus={() => {
                  void loadStatus();
                  void loadDependency();
                }}
                onClearAll={() => setClearOpen(true)}
                clearing={clearing}
              />
            ) : null)}
        </>
      )}

      <ConfirmDialog
        open={clearOpen}
        onOpenChange={setClearOpen}
        destructive
        holdSeconds={5}
        title={t('memory.clear.confirmTitle')}
        description={t('memory.clear.confirmDescription')}
        confirmLabel={t('memory.clear.confirmLabel')}
        onConfirm={confirmClear}
      >
        <div className="flex flex-col gap-3 text-[12.5px] leading-snug">
          <div className="rounded-[10px] border border-border bg-surface-2 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.clear.removesTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.clear.removes', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-[10px] border border-warning/30 bg-warning/5 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.clear.keepsTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.clear.keeps', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </ConfirmDialog>
    </div>
  );
};
