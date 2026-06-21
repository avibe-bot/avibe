import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  Cpu,
  Plus,
  Info,
  KeyRound,
  Pencil,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Server,
  Star,
  Terminal,
  Trash2,
  X,
} from 'lucide-react';
import clsx from 'clsx';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { Checkbox } from '../../ui/checkbox';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '../../ui/popover';
import { BackendOAuthPanel } from '../BackendOAuthPanel';
import { OpencodeProviderTestPanel } from '../OpencodeProviderTestPanel';
import { BackendRuntimeCard } from '../shared/BackendRuntimeCard';
import { OpencodePermissionSetup } from '../shared/OpencodePermissionSetup';
import { useBackendRuntime } from '../shared/useBackendRuntime';
import { useOpencodePermission } from '../shared/useOpencodePermission';
import { useApi } from '@/context/ApiContext';
import type {
  OpencodeMutationResult,
  OpencodeProvider,
  OpencodeProviderListResult,
} from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

type FilterMode = 'all' | 'configured' | 'oauth' | 'local';
type CustomProviderAdapter = 'openai-compatible' | 'anthropic-compatible';

// Per-provider edit state — kept in a record so the page can render the
// grid statelessly and only allocate inputs for the expanded card.
type ProviderEditState = {
  apiKey: string;
  baseUrl: string;
  modelId: string;
  reasoningEfforts: string[];
  modelSaving: boolean;
  removingModelId: string | null;
  saving: boolean;
  removing: boolean;
  deletingProvider: boolean;
  error: string | null;
  // Mirrors the Codex / Claude pattern: false = show ``api_key_masked``
  // read-only with a Replace button; true = empty editable input ready
  // for a fresh key. Toggled by the pencil button next to the masked
  // preview. Reset to false on successful save / remove / reload.
  editingKey: boolean;
};

type CustomProviderDraft = {
  name: string;
  providerId: string;
  providerIdEdited: boolean;
  adapter: CustomProviderAdapter;
  baseUrl: string;
  apiKey: string;
  saving: boolean;
  error: string | null;
};

const BACKEND_ID = 'opencode';
const DEFAULT_CLI = 'opencode';
// Server startup is asynchronous (OpenCode app spawn + port bind). Retry
// the introspection fan-out a handful of times before falling back to the
// "server not reachable" banner so the user can still configure values.
const SERVER_START_MAX_RETRIES = 5;
const SERVER_START_RETRY_DELAY_MS = 3000;

const FILTER_MODES: ReadonlyArray<FilterMode> = ['all', 'configured', 'oauth', 'local'];
const REASONING_EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

const defaultReasoningEfforts = () => [...REASONING_EFFORTS];

const notifyOpenCodeModelOptionsChanged = () => {
  window.dispatchEvent(new CustomEvent('avibe:opencode-model-options-changed'));
};

const emptyEdit = (): ProviderEditState => ({
  apiKey: '',
  baseUrl: '',
  modelId: '',
  reasoningEfforts: defaultReasoningEfforts(),
  modelSaving: false,
  removingModelId: null,
  saving: false,
  removing: false,
  deletingProvider: false,
  error: null,
  editingKey: false,
});

const CUSTOM_PROVIDER_ADAPTERS: CustomProviderAdapter[] = [
  'openai-compatible',
  'anthropic-compatible',
];

const emptyCustomProviderDraft = (): CustomProviderDraft => ({
  name: '',
  providerId: '',
  providerIdEdited: false,
  adapter: 'openai-compatible',
  baseUrl: '',
  apiKey: '',
  saving: false,
  error: null,
});

const slugProviderId = (value: string): string =>
  value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 64);

const providerMatchesFilter = (provider: OpencodeProvider, mode: FilterMode): boolean => {
  switch (mode) {
    case 'configured':
      return provider.configured;
    case 'oauth':
      return provider.oauth_available;
    case 'local':
      return provider.local;
    case 'all':
    default:
      return true;
  }
};

const providerHasAuth = (provider: OpencodeProvider): boolean =>
  provider.has_auth ?? Boolean(provider.api_key_masked || provider.active_auth_type);

const providerMatchesSearch = (provider: OpencodeProvider, q: string): boolean => {
  if (!q) return true;
  const needle = q.toLowerCase();
  if (provider.id.toLowerCase().includes(needle)) return true;
  if (provider.name.toLowerCase().includes(needle)) return true;
  if (provider.description.toLowerCase().includes(needle)) return true;
  return provider.models.some((m) => m.toLowerCase().includes(needle));
};

export const OpencodeProviderConfig: React.FC<{
  hideEnableToggle?: boolean;
  deferRestart?: boolean;
}> = ({ hideEnableToggle, deferRestart } = {}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  // Runtime state — shared with Claude / Codex pages via the hook.
  const runtime = useBackendRuntime({
    backend: BACKEND_ID,
    defaultCli: DEFAULT_CLI,
    deferRestart,
  });
  const permission = useOpencodePermission();
  const { setPermissionAllowed } = permission;

  // Provider catalog state.
  const [providers, setProviders] = useState<OpencodeProvider[] | null>(null);
  const [defaultProvider, setDefaultProvider] = useState<string | null>(null);
  const [providersLoading, setProvidersLoading] = useState(false);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [serverStartAttempts, setServerStartAttempts] = useState(0);

  // Toolbar state.
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMode, setFilterMode] = useState<FilterMode>('all');

  // Inline-expansion state — only one card open at a time.
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editByProvider, setEditByProvider] = useState<Record<string, ProviderEditState>>({});
  const [showCustomProviderForm, setShowCustomProviderForm] = useState(false);
  const [customProviderDraft, setCustomProviderDraft] = useState<CustomProviderDraft>(() =>
    emptyCustomProviderDraft(),
  );

  // Default-provider popover state.
  const [defaultPopoverOpen, setDefaultPopoverOpen] = useState(false);
  const [defaultSearchQuery, setDefaultSearchQuery] = useState('');
  const [settingDefault, setSettingDefault] = useState(false);

  // Server-start retry timer cleanup.
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // useEffect closure captures the initial loadProviders; ref lets the
  // retry timer call the latest implementation without re-arming on every
  // state update.
  const loadProvidersRef = useRef<(() => Promise<void>) | null>(null);

  const loadProviders = useCallback(async () => {
    setProvidersLoading(true);
    setProvidersError(null);
    try {
      const result = await api.getOpencodeProviders();
      if (result.ok && result.providers) {
        setProviders(result.providers);
        setDefaultProvider(result.default_provider || null);
        setPermissionAllowed(result.permission_allowed === true);
        setServerStartAttempts(0);
        setProvidersError(null);
      } else {
        setProviders((prev) => prev ?? []);
        setProvidersError(result.message || t('settings.backends.opencodeProvidersError'));
      }
    } catch (e: any) {
      setProviders((prev) => prev ?? []);
      setProvidersError(e?.message || t('settings.backends.opencodeProvidersError'));
    } finally {
      setProvidersLoading(false);
    }
  }, [api, t, setPermissionAllowed]);

  const applyProvidersResult = useCallback(
    (result?: OpencodeProviderListResult | null): boolean => {
      if (result?.ok && Array.isArray(result.providers)) {
        setProviders(result.providers);
        setDefaultProvider(result.default_provider || null);
        setPermissionAllowed(result.permission_allowed === true);
        setServerStartAttempts(0);
        setProvidersError(null);
        return true;
      }
      return false;
    },
    [],
  );

  const applyMutationCatalogRefresh = useCallback(
    (result: OpencodeMutationResult): boolean => {
      const refresh = result?.catalog_refresh;
      const catalog = refresh?.catalog;
      if (applyProvidersResult(catalog)) {
        if (refresh?.ok === false) {
          showToast(
            refresh.message || (t('settings.backends.opencodeProviderCatalogRefreshPending') as string),
            'warning',
          );
        }
        return true;
      }
      if (refresh?.ok === false) {
        showToast(
          refresh.message || (t('settings.backends.opencodeProviderCatalogRefreshPending') as string),
          'warning',
        );
      }
      return false;
    },
    [applyProvidersResult, showToast, t],
  );

  useEffect(() => {
    loadProvidersRef.current = loadProviders;
  }, [loadProviders]);

  // Runtime initial-load + cli_path/detect/install/save/toggle live in
  // ``useBackendRuntime``. This page-specific effect just drives the
  // providers fan-out: load when the runtime is enabled, clear when
  // it's not. Triggered whenever the optimistic enabled flag changes
  // (matches the original "toggle then reload" UX exactly).
  useEffect(() => {
    if (!runtime.loaded) return;
    // If the initial getConfig() failed, ``runtime.enabled`` is still
    // the pre-load default rather than persisted state. Don't trigger
    // provider fetches in that case — the toast in the runtime hook
    // already informed the user; firing requests now would just emit
    // misleading "server stopped" errors.
    if (runtime.configError) return;
    if (runtime.enabled) {
      setServerStartAttempts(0);
      void loadProviders();
    } else {
      setProviders(null);
      setProvidersError(null);
    }
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtime.enabled, runtime.loaded, runtime.configError]);

  // Auto-retry server-start failures — common when the user has just
  // enabled the backend. After SERVER_START_MAX_RETRIES the user can hit
  // Refresh manually; we stop the implicit retry to avoid background
  // request loops.
  useEffect(() => {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (!runtime.enabled) return;
    if (!providersError) return;
    if (providersLoading) return;
    if (serverStartAttempts >= SERVER_START_MAX_RETRIES) return;
    retryTimerRef.current = setTimeout(() => {
      setServerStartAttempts((n) => n + 1);
      void loadProvidersRef.current?.();
    }, SERVER_START_RETRY_DELAY_MS);
    return () => {
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [runtime.enabled, providersError, providersLoading, serverStartAttempts]);

  // ``onSaveRuntime`` and ``toggleEnabled`` are owned by
  // ``useBackendRuntime``; the providers reload/clear side-effect
  // that used to live inside the page's toggle handler is now driven
  // by the ``useEffect([runtime.enabled, runtime.loaded])`` above.

  // ---- Expansion / per-provider editing ----

  const openProvider = (provider: OpencodeProvider) => {
    setExpandedId(provider.id);
    setEditByProvider((prev) => {
      if (prev[provider.id]) return prev;
      // Pre-populate baseUrl from the persisted opencode.json override so
      // the input round-trips: open card → see existing value → edit →
      // save. Leaving it blank on every open would let a re-save wipe the
      // value because the form is the only baseURL signal we forward.
      const seeded = emptyEdit();
      if (provider.base_url) {
        seeded.baseUrl = provider.base_url;
      }
      return { ...prev, [provider.id]: seeded };
    });
  };

  const closeProvider = () => {
    // Wipe the in-memory plaintext key/base-url for the closing card so
    // collapsing acts as a panic-revert: defense-in-depth in case the
    // user typed a key but did not save it.
    if (expandedId) {
      setEditByProvider((prev) => {
        if (!prev[expandedId]) return prev;
        return { ...prev, [expandedId]: emptyEdit() };
      });
    }
    setExpandedId(null);
  };

  const onToggleProvider = (provider: OpencodeProvider) => {
    if (expandedId === provider.id) {
      closeProvider();
    } else {
      openProvider(provider);
    }
  };

  const updateEdit = (providerId: string, patch: Partial<ProviderEditState>) => {
    setEditByProvider((prev) => ({
      ...prev,
      [providerId]: { ...(prev[providerId] || emptyEdit()), ...patch },
    }));
  };

  const updateCustomProviderDraft = (patch: Partial<CustomProviderDraft>) => {
    setCustomProviderDraft((prev) => ({ ...prev, ...patch }));
  };

  const onSaveCustomProvider = async () => {
    const name = customProviderDraft.name.trim();
    const providerId = customProviderDraft.providerId.trim();
    const baseUrl = customProviderDraft.baseUrl.trim();
    const apiKey = customProviderDraft.apiKey.trim();
    if (!name || !providerId || !baseUrl) {
      updateCustomProviderDraft({
        error: t('settings.backends.opencodeCustomProviderRequired') as string,
      });
      return;
    }
    if (!/^[a-z0-9][a-z0-9_.-]*$/.test(providerId)) {
      updateCustomProviderDraft({
        error: t('settings.backends.opencodeCustomProviderIdInvalid') as string,
      });
      return;
    }
    if (!baseUrl.toLowerCase().startsWith('http://') && !baseUrl.toLowerCase().startsWith('https://')) {
      updateCustomProviderDraft({
        error: t('settings.backends.opencodeCustomProviderBaseUrlInvalid') as string,
      });
      return;
    }

    updateCustomProviderDraft({ saving: true, error: null });
    try {
      const result = await api.saveOpencodeCustomProvider({
        provider_id: providerId,
        name,
        adapter: customProviderDraft.adapter,
        base_url: baseUrl,
        ...(apiKey ? { api_key: apiKey } : {}),
      });
      if (!result.ok) {
        updateCustomProviderDraft({
          saving: false,
          error: result.message || (t('settings.backends.opencodeCustomProviderSaveFailed') as string),
        });
        return;
      }
      setCustomProviderDraft(emptyCustomProviderDraft());
      setShowCustomProviderForm(false);
      showToast(t('settings.backends.opencodeCustomProviderSaved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      if (!applyMutationCatalogRefresh(result)) {
        await loadProviders();
      }
      const nextId = result.provider_id || providerId;
      setExpandedId(nextId);
      setEditByProvider((prev) => ({
        ...prev,
        [nextId]: { ...(prev[nextId] || emptyEdit()), baseUrl },
      }));
    } catch (e: any) {
      updateCustomProviderDraft({
        saving: false,
        error: e?.message || (t('settings.backends.opencodeCustomProviderSaveFailed') as string),
      });
    }
  };

  const onDeleteCustomProvider = async (provider: OpencodeProvider) => {
    const confirmed = window.confirm(
      t('settings.backends.opencodeCustomProviderRemoveConfirm', { name: provider.name }) as string,
    );
    if (!confirmed) return;
    updateEdit(provider.id, { deletingProvider: true, error: null });
    try {
      const result = await api.deleteOpencodeCustomProvider(provider.id);
      if (!result.ok) {
        updateEdit(provider.id, {
          deletingProvider: false,
          error: result.message || (t('settings.backends.opencodeCustomProviderRemoveFailed') as string),
        });
        return;
      }
      updateEdit(provider.id, { deletingProvider: false, error: null });
      showToast(t('settings.backends.opencodeCustomProviderRemoved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      if (expandedId === provider.id) setExpandedId(null);
      await loadProviders();
    } catch (e: any) {
      updateEdit(provider.id, {
        deletingProvider: false,
        error: e?.message || (t('settings.backends.opencodeCustomProviderRemoveFailed') as string),
      });
    }
  };

  const onSaveProviderAuth = async (provider: OpencodeProvider) => {
    const state = editByProvider[provider.id] || emptyEdit();
    const key = state.apiKey.trim();
    // Reject an empty key only when the provider has no saved
    // credentials. For already-configured providers the UI hides the
    // plaintext key behind a "Replace" pencil; the backend save
    // endpoint reuses the on-disk key in that case so a base-URL-only
    // edit can land without re-typing the secret. Without this branch
    // a user with a saved key can never persist a relay-URL fix.
    if (!key && !provider.configured) {
      updateEdit(provider.id, {
        error: t('settings.backends.opencodeProviderApiKeyRequired') as string,
      });
      return;
    }
    // The Base URL field is the only signal the form sends for the
    // ``provider.<id>.options.baseURL`` override in ``opencode.json``.
    // Forwarding the trimmed value verbatim — including the empty
    // string for "clear" — is critical: if we dropped to ``undefined``
    // for blanks, the server would interpret it as "leave unchanged"
    // and a user who removed the value in the form would silently keep
    // the old override on disk.
    const baseUrl = state.baseUrl.trim();
    if (provider.custom && !baseUrl) {
      updateEdit(provider.id, {
        error: t('settings.backends.opencodeProviderBaseUrlRequired') as string,
      });
      return;
    }
    updateEdit(provider.id, { saving: true, error: null });
    try {
      const result = await api.setOpencodeProviderAuth(provider.id, key, baseUrl);
      if (!result.ok) {
        updateEdit(provider.id, {
          saving: false,
          error: result.message || (t('settings.backends.opencodeProviderSaveFailed') as string),
        });
        return;
      }
      updateEdit(provider.id, {
        saving: false,
        apiKey: '',
        editingKey: false,
        error: null,
      });
      showToast(t('settings.backends.opencodeProviderSaved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      if (!applyMutationCatalogRefresh(result)) {
        await loadProviders();
      }
    } catch (e: any) {
      updateEdit(provider.id, {
        saving: false,
        error: e?.message || (t('settings.backends.opencodeProviderSaveFailed') as string),
      });
    }
  };

  const onRemoveProviderAuth = async (provider: OpencodeProvider) => {
    // Confirm copy matches what's actually about to be removed.
    const confirmKey =
      provider.active_auth_type === 'oauth'
        ? 'settings.backends.opencodeProviderRemoveOauthConfirm'
        : 'settings.backends.opencodeProviderRemoveConfirm';
    const confirmed = window.confirm(
      t(confirmKey, { name: provider.name }) as string,
    );
    if (!confirmed) return;
    updateEdit(provider.id, { removing: true, error: null });
    try {
      const result = await api.deleteOpencodeProviderAuth(provider.id);
      if (!result.ok) {
        updateEdit(provider.id, {
          removing: false,
          error: result.message || (t('settings.backends.opencodeProviderRemoveFailed') as string),
        });
        return;
      }
      updateEdit(provider.id, { removing: false, error: null });
      showToast(t('settings.backends.opencodeProviderRemoved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      await loadProviders();
    } catch (e: any) {
      updateEdit(provider.id, {
        removing: false,
        error: e?.message || (t('settings.backends.opencodeProviderRemoveFailed') as string),
      });
    }
  };

  const onSaveProviderModel = async (provider: OpencodeProvider) => {
    const state = editByProvider[provider.id] || emptyEdit();
    const modelId = state.modelId.trim();
    if (!modelId) {
      updateEdit(provider.id, {
        error: t('settings.backends.opencodeProviderModelRequired') as string,
      });
      return;
    }
    if (modelId.toLowerCase().startsWith(`${provider.id.toLowerCase()}/`)) {
      updateEdit(provider.id, {
        error: t('settings.backends.opencodeProviderModelNoProviderPrefix') as string,
      });
      return;
    }
    if (provider.models.includes(modelId)) {
      const entry = provider.model_entries?.find((item) => item.id === modelId);
      if (!entry?.user_managed) {
        updateEdit(provider.id, {
          error: t('settings.backends.opencodeProviderModelDuplicate') as string,
        });
        return;
      }
    }

    updateEdit(provider.id, { modelSaving: true, error: null });
    try {
      const result = await api.saveOpencodeProviderModel(provider.id, {
        model_id: modelId,
        reasoning_efforts: state.reasoningEfforts,
      });
      if (!result.ok) {
        updateEdit(provider.id, {
          modelSaving: false,
          error: result.message || (t('settings.backends.opencodeProviderModelSaveFailed') as string),
        });
        return;
      }
      updateEdit(provider.id, {
        modelId: '',
        reasoningEfforts: defaultReasoningEfforts(),
        modelSaving: false,
        error: null,
      });
      showToast(t('settings.backends.opencodeProviderModelSaved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      await loadProviders();
    } catch (e: any) {
      updateEdit(provider.id, {
        modelSaving: false,
        error: e?.message || (t('settings.backends.opencodeProviderModelSaveFailed') as string),
      });
    }
  };

  const onDeleteProviderModel = async (provider: OpencodeProvider, modelId: string) => {
    updateEdit(provider.id, { removingModelId: modelId, error: null });
    try {
      const result = await api.deleteOpencodeProviderModel(provider.id, modelId);
      if (!result.ok) {
        updateEdit(provider.id, {
          removingModelId: null,
          error: result.message || (t('settings.backends.opencodeProviderModelRemoveFailed') as string),
        });
        return;
      }
      updateEdit(provider.id, { removingModelId: null, error: null });
      showToast(t('settings.backends.opencodeProviderModelRemoved'), 'success');
      notifyOpenCodeModelOptionsChanged();
      await loadProviders();
    } catch (e: any) {
      updateEdit(provider.id, {
        removingModelId: null,
        error: e?.message || (t('settings.backends.opencodeProviderModelRemoveFailed') as string),
      });
    }
  };

  const toggleReasoningEffort = (providerId: string, effort: string) => {
    const current = editByProvider[providerId] || emptyEdit();
    const next = current.reasoningEfforts.includes(effort)
      ? current.reasoningEfforts.filter((item) => item !== effort)
      : [...current.reasoningEfforts, effort];
    updateEdit(providerId, { reasoningEfforts: next });
  };

  // ---- Default-provider selection ----

  const pickDefaultProvider = async (provider: OpencodeProvider) => {
    if (!provider.configured) {
      // Surfacing "set the key first" is more discoverable than silently
      // ignoring the click — expand the card and let the user fill it in.
      setDefaultPopoverOpen(false);
      setDefaultSearchQuery('');
      openProvider(provider);
      showToast(
        t('settings.backends.opencodeDefaultNeedsKey', { name: provider.name }),
        'warning'
      );
      return;
    }
    if (provider.id === defaultProvider) {
      setDefaultPopoverOpen(false);
      setDefaultSearchQuery('');
      return;
    }
    setSettingDefault(true);
    try {
      const result = await api.setOpencodeDefaultProvider(provider.id);
      if (!result.ok) {
        showToast(
          result.message || (t('settings.backends.opencodeDefaultSaveFailed') as string),
          'error'
        );
        return;
      }
      setDefaultProvider(result.default_provider || provider.id);
      showToast(t('settings.backends.opencodeDefaultSaved', { name: provider.name }), 'success');
      setDefaultPopoverOpen(false);
      setDefaultSearchQuery('');
    } catch (e: any) {
      showToast(e?.message || (t('settings.backends.opencodeDefaultSaveFailed') as string), 'error');
    } finally {
      setSettingDefault(false);
    }
  };

  // ---- Derived collections ----

  const visibleProviders = useMemo(() => {
    if (!providers) return [];
    return providers.filter(
      (p) => providerMatchesFilter(p, filterMode) && providerMatchesSearch(p, searchQuery)
    );
  }, [providers, filterMode, searchQuery]);

  const popoverProviders = useMemo(() => {
    if (!providers) return [];
    return providers.filter((p) => providerMatchesSearch(p, defaultSearchQuery));
  }, [providers, defaultSearchQuery]);

  const configuredCount = providers?.filter((p) => p.configured).length || 0;
  const totalCount = providers?.length || 0;
  const defaultProviderObj = useMemo(
    () => providers?.find((p) => p.id === defaultProvider) || null,
    [providers, defaultProvider]
  );

  const filterLabel = (mode: FilterMode) => {
    switch (mode) {
      case 'all':
        return t('settings.backends.opencodeFilterAll');
      case 'configured':
        return t('settings.backends.opencodeFilterConfigured');
      case 'oauth':
        return t('settings.backends.opencodeFilterOauth');
      case 'local':
        return t('settings.backends.opencodeFilterLocal');
    }
  };

  const customProviderAdapterLabel = (adapter: CustomProviderAdapter) => {
    switch (adapter) {
      case 'anthropic-compatible':
        return t('settings.backends.opencodeCustomProviderAdapterAnthropic');
      case 'openai-compatible':
      default:
        return t('settings.backends.opencodeCustomProviderAdapterOpenAI');
    }
  };

  // ---- Status badge selection ----
  //
  // The order here matches the plan doc: configured wins, then OAuth
  // availability, then local, finally "not set" for unconfigured cloud
  // providers. Multi-flag providers therefore surface the most useful
  // signal first.
  const renderProviderBadge = (provider: OpencodeProvider) => {
    if (provider.configured) {
      // Show *which* auth source is live so the user can distinguish
      // OAuth-signed-in from API-key-saved without expanding the card.
      // Dual-mode providers (openai supports both) carry only one entry
      // in auth.json at a time, so this matches what OpenCode uses.
      const activeLabel =
        provider.active_auth_type === 'oauth'
          ? t('settings.backends.opencodeBadgeConfiguredOauth')
          : provider.active_auth_type === 'api'
          ? t('settings.backends.opencodeBadgeConfiguredApiKey')
          : t('settings.backends.opencodeBadgeConfigured');
      return (
        <Badge variant="success" className="gap-1">
          <Check className="size-3" />
          {activeLabel}
        </Badge>
      );
    }
    if (provider.oauth_available) {
      return (
        <Badge variant="info">{t('settings.backends.opencodeBadgeOauth')}</Badge>
      );
    }
    if (provider.local) {
      return (
        <Badge variant="secondary">{t('settings.backends.opencodeBadgeLocal')}</Badge>
      );
    }
    return <Badge variant="outline">{t('settings.backends.opencodeBadgeUnset')}</Badge>;
  };

  // ---- Render ----

  if (!runtime.loaded) {
    return <div className="text-sm text-muted">{t('common.loading')}</div>;
  }

  return (
    <div className="flex flex-col gap-4">
      <BackendRuntimeCard
        backend={BACKEND_ID}
        label="OpenCode"
        description={t('settings.backends.opencodeDescription')}
        Icon={Terminal}
        iconTileClassName="bg-violet-soft"
        iconClassName="text-violet"
        runtime={runtime}
        hideEnableToggle={hideEnableToggle}
        extraSlot={
          // ``permission: "allow"`` setup is OpenCode-specific: without it the
          // daemon prompts on every tool call and avibe can't reply.
          // Shared with the setup wizard; hidden once opencode.json carries it.
          <OpencodePermissionSetup
            cliReady={runtime.cliStatus === 'ok'}
            permissionAllowed={permission.permissionAllowed}
            state={permission.state}
            message={permission.message}
            onSetup={() => void permission.setupPermission()}
          />
        }
      />

      {/* Card 2 — provider catalog. */}
      <Card>
        <CardContent className="flex flex-col gap-5 p-6">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div className="flex items-center gap-3">
              <div className="flex size-11 shrink-0 items-center justify-center rounded-[10px] bg-cyan-soft">
                <Server size={22} className="text-cyan" />
              </div>
              <div className="flex flex-col gap-0.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[15px] font-semibold text-foreground">
                    {t('settings.backends.opencodeProvidersTitle')}
                  </span>
                  {runtime.enabled && providers && (
                    <Badge variant={providersError ? 'warning' : 'success'}>
                      {providersError
                        ? t('settings.backends.opencodeServerStopped')
                        : t('settings.backends.opencodeServerRunning')}
                    </Badge>
                  )}
                  {!runtime.enabled && (
                    <Badge variant="secondary">
                      {t('settings.backends.opencodeBackendDisabled')}
                    </Badge>
                  )}
                  {runtime.enabled && providers && providers.length > 0 && (
                    <Badge variant="outline">
                      {t('settings.backends.opencodeProvidersCount', {
                        configured: configuredCount,
                        total: totalCount,
                      })}
                    </Badge>
                  )}
                </div>
                <span className="text-[12px] text-muted">
                  {t('settings.backends.opencodeProvidersSubtitle')}
                </span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="brand"
                size="sm"
                onClick={() => setShowCustomProviderForm((open) => !open)}
                disabled={!runtime.enabled}
              >
                <Plus className="size-3.5" />
                {t('settings.backends.opencodeCustomProviderAdd')}
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => {
                  setServerStartAttempts(0);
                  void loadProviders();
                }}
                disabled={!runtime.enabled || providersLoading}
              >
                <RefreshCw
                  className={clsx('size-3.5', providersLoading && 'animate-spin')}
                />
                {t('settings.backends.opencodeRefresh')}
              </Button>
            </div>
          </div>

          {!runtime.enabled && (
            <div className="rounded-lg border border-border bg-surface-2/60 px-3 py-2.5 text-[12px] text-muted">
              {t('settings.backends.opencodeDisabledBanner')}
            </div>
          )}

          {runtime.enabled && (
            <>
              {showCustomProviderForm && (
                <div className="rounded-lg border border-mint/30 bg-mint-soft/10 px-4 py-4">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-[13px] font-semibold text-foreground">
                        {t('settings.backends.opencodeCustomProviderTitle')}
                      </p>
                      <p className="text-[12px] text-muted">
                        {t('settings.backends.opencodeCustomProviderSubtitle')}
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={() => {
                        setShowCustomProviderForm(false);
                        setCustomProviderDraft(emptyCustomProviderDraft());
                      }}
                      disabled={customProviderDraft.saving}
                    >
                      <X className="size-3.5" />
                      {t('common.cancel')}
                    </Button>
                  </div>
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    <div className="flex flex-col gap-1.5">
                      <Label className="text-[11px] font-medium uppercase text-muted">
                        {t('settings.backends.opencodeCustomProviderName')}
                      </Label>
                      <Input
                        type="text"
                        value={customProviderDraft.name}
                        onChange={(e) => {
                          const nextName = e.target.value;
                          setCustomProviderDraft((prev) => ({
                            ...prev,
                            name: nextName,
                            providerId: prev.providerIdEdited
                              ? prev.providerId
                              : slugProviderId(nextName),
                            error: null,
                          }));
                        }}
                        placeholder={t('settings.backends.opencodeCustomProviderNamePlaceholder') as string}
                        disabled={customProviderDraft.saving}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label className="text-[11px] font-medium uppercase text-muted">
                        {t('settings.backends.opencodeCustomProviderId')}
                      </Label>
                      <Input
                        type="text"
                        value={customProviderDraft.providerId}
                        onChange={(e) =>
                          updateCustomProviderDraft({
                            providerId: e.target.value.trim().toLowerCase(),
                            providerIdEdited: true,
                            error: null,
                          })
                        }
                        placeholder={t('settings.backends.opencodeCustomProviderIdPlaceholder') as string}
                        className="font-mono"
                        disabled={customProviderDraft.saving}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label className="text-[11px] font-medium uppercase text-muted">
                        {t('settings.backends.opencodeCustomProviderAdapter')}
                      </Label>
                      <div className="flex flex-wrap gap-2">
                        {CUSTOM_PROVIDER_ADAPTERS.map((adapter) => {
                          const active = customProviderDraft.adapter === adapter;
                          return (
                            <Button
                              key={adapter}
                              type="button"
                              variant={active ? 'accent' : 'outline'}
                              size="sm"
                              onClick={() => updateCustomProviderDraft({ adapter, error: null })}
                              disabled={customProviderDraft.saving}
                            >
                              {customProviderAdapterLabel(adapter)}
                            </Button>
                          );
                        })}
                      </div>
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label className="text-[11px] font-medium uppercase text-muted">
                        {t('settings.backends.opencodeProviderBaseUrl')}
                      </Label>
                      <Input
                        type="url"
                        value={customProviderDraft.baseUrl}
                        onChange={(e) =>
                          updateCustomProviderDraft({ baseUrl: e.target.value, error: null })
                        }
                        placeholder={t('settings.backends.opencodeProviderBaseUrlPlaceholder') as string}
                        className="font-mono"
                        disabled={customProviderDraft.saving}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5 lg:col-span-2">
                      <Label className="text-[11px] font-medium uppercase text-muted">
                        {t('settings.backends.opencodeProviderApiKey')}
                      </Label>
                      <Input
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        value={customProviderDraft.apiKey}
                        onChange={(e) =>
                          updateCustomProviderDraft({ apiKey: e.target.value, error: null })
                        }
                        placeholder={t('settings.backends.opencodeProviderApiKeyPlaceholder') as string}
                        className="font-mono"
                        disabled={customProviderDraft.saving}
                      />
                    </div>
                  </div>
                  {customProviderDraft.error && (
                    <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
                      {customProviderDraft.error}
                    </div>
                  )}
                  <div className="mt-3 flex justify-end">
                    <Button
                      type="button"
                      variant="brand"
                      size="sm"
                      onClick={() => void onSaveCustomProvider()}
                      disabled={customProviderDraft.saving}
                    >
                      {customProviderDraft.saving ? (
                        <RefreshCw className="size-3.5 animate-spin" />
                      ) : (
                        <Save className="size-3.5" />
                      )}
                      {customProviderDraft.saving ? t('common.saving') : t('settings.backends.opencodeCustomProviderSave')}
                    </Button>
                  </div>
                </div>
              )}

              {/* Toolbar — search + filter chips + default-provider pill. */}
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex flex-1 flex-col gap-3 sm:flex-row sm:items-center">
                  <div className="relative w-full sm:max-w-xs">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
                    <Input
                      type="text"
                      placeholder={t('settings.backends.opencodeSearchPlaceholder') as string}
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      className="h-9 pl-8 text-[12px]"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {FILTER_MODES.map((mode) => {
                      const active = filterMode === mode;
                      return (
                        <Button
                          key={mode}
                          type="button"
                          variant={active ? 'accent' : 'outline'}
                          size="xs"
                          onClick={() => setFilterMode(mode)}
                          className="h-7 rounded-full px-3 text-[11px] font-semibold"
                        >
                          {filterLabel(mode)}
                        </Button>
                      );
                    })}
                  </div>
                </div>

                {/* Default-provider pill. */}
                <Popover
                  open={defaultPopoverOpen}
                  onOpenChange={(open) => {
                    setDefaultPopoverOpen(open);
                    if (!open) setDefaultSearchQuery('');
                  }}
                >
                  <PopoverTrigger asChild>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!providers || providers.length === 0}
                      className="justify-between gap-2 text-[12px]"
                    >
                      <Star className="size-3.5 text-mint" />
                      <span className="text-muted">
                        {t('settings.backends.opencodeDefaultLabel')}:
                      </span>
                      <span className="font-mono text-[12px] text-foreground">
                        {defaultProviderObj
                          ? defaultProviderObj.name
                          : defaultProvider
                            ? defaultProvider
                            : t('settings.backends.opencodeDefaultUnset')}
                      </span>
                      <ChevronDown className="size-3.5 text-muted" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent align="end" className="w-80 p-0">
                    <div className="flex flex-col gap-2 p-3">
                      <Label className="text-xs font-medium uppercase text-muted">
                        {t('settings.backends.opencodeDefaultPopoverTitle')}
                      </Label>
                      <div className="relative">
                        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
                        <Input
                          type="text"
                          placeholder={
                            t('settings.backends.opencodeSearchPlaceholder') as string
                          }
                          value={defaultSearchQuery}
                          onChange={(e) => setDefaultSearchQuery(e.target.value)}
                          className="h-8 pl-8 text-[12px]"
                          autoComplete="off"
                          spellCheck={false}
                        />
                      </div>
                    </div>
                    <div className="max-h-72 overflow-y-auto border-t border-border">
                      {popoverProviders.length === 0 ? (
                        <div className="px-3 py-2 text-[12px] text-muted">
                          {t('settings.backends.opencodeDefaultPopoverEmpty')}
                        </div>
                      ) : (
                        <ul className="flex flex-col">
                          {popoverProviders.map((provider) => {
                            const isCurrent = provider.id === defaultProvider;
                            return (
                              <li key={provider.id}>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => void pickDefaultProvider(provider)}
                                  disabled={settingDefault}
                                  className={clsx(
                                    'h-auto w-full justify-between whitespace-normal rounded-none px-3 py-2 text-left text-[12px]',
                                    isCurrent && 'bg-mint-soft/40'
                                  )}
                                >
                                  <span className="flex flex-col gap-0.5">
                                    <span className="font-medium text-foreground">
                                      {provider.name}
                                    </span>
                                    <span className="font-mono text-[11px] text-muted">
                                      {provider.id}
                                    </span>
                                  </span>
                                  <span className="flex items-center gap-2">
                                    {provider.configured ? (
                                      <Badge variant="success" className="gap-1">
                                        <Check className="size-3" />
                                        {t('settings.backends.opencodeBadgeConfigured')}
                                      </Badge>
                                    ) : provider.oauth_available ? (
                                      <Badge variant="info">
                                        {t('settings.backends.opencodeBadgeOauth')}
                                      </Badge>
                                    ) : provider.local ? (
                                      <Badge variant="secondary">
                                        {t('settings.backends.opencodeBadgeLocal')}
                                      </Badge>
                                    ) : (
                                      <Badge variant="outline">
                                        {t('settings.backends.opencodeBadgeUnset')}
                                      </Badge>
                                    )}
                                    {isCurrent && <Check className="size-3.5 text-mint" />}
                                  </span>
                                </Button>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </div>
                  </PopoverContent>
                </Popover>
              </div>

              {/* Server-starting / error banner. */}
              {providersError && (
                <div className="flex items-start gap-2 rounded-lg border border-gold/30 bg-gold/[0.08] px-3 py-2.5">
                  <AlertCircle className="mt-0.5 size-3.5 shrink-0 text-gold" />
                  <div className="flex flex-1 flex-col gap-1">
                    <p className="text-[12px] font-medium text-gold">
                      {serverStartAttempts < SERVER_START_MAX_RETRIES
                        ? t('settings.backends.opencodeServerStarting')
                        : t('settings.backends.opencodeServerUnreachable')}
                    </p>
                    <p className="text-[12px] leading-relaxed text-muted">{providersError}</p>
                  </div>
                  {serverStartAttempts < SERVER_START_MAX_RETRIES && (
                    <span className="shrink-0 text-[11px] text-muted">
                      {t('settings.backends.opencodeServerRetryCount', {
                        attempt: serverStartAttempts + 1,
                        max: SERVER_START_MAX_RETRIES,
                      })}
                    </span>
                  )}
                </div>
              )}

              {/* Initial loading skeleton. */}
              {providersLoading && !providers && (
                <div className="rounded-lg border border-border bg-surface-2/60 px-3 py-6 text-center text-[12px] text-muted">
                  <RefreshCw className="mx-auto mb-2 size-4 animate-spin text-cyan" />
                  {t('settings.backends.opencodeProvidersLoading')}
                </div>
              )}

              {/* Empty state — server reports no providers at all. */}
              {!providersLoading && providers && providers.length === 0 && !providersError && (
                <div className="rounded-lg border border-border bg-surface-2/60 px-3 py-6 text-center text-[12px] text-muted">
                  {t('settings.backends.opencodeProvidersEmpty')}
                </div>
              )}

              {/* Empty state — search/filter excluded everything. */}
              {!providersLoading &&
                providers &&
                providers.length > 0 &&
                visibleProviders.length === 0 && (
                  <div className="rounded-lg border border-border bg-surface-2/60 px-3 py-6 text-center text-[12px] text-muted">
                    {t('settings.backends.opencodeProvidersFilterEmpty')}
                  </div>
                )}

              {/* Grid. */}
              {visibleProviders.length > 0 && (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {visibleProviders.map((provider) => {
                    const expanded = expandedId === provider.id;
                    const edit = editByProvider[provider.id] || emptyEdit();
                    const isDefault = defaultProvider === provider.id;
                    return (
                      <div
                        key={provider.id}
                        className={clsx(
                          'flex flex-col rounded-lg border bg-surface transition-colors',
                          expanded ? 'border-mint/40 shadow-[0_0_24px_-12px_rgba(91,255,160,0.6)]' : 'border-border hover:border-border-strong',
                          expanded && 'md:col-span-2'
                        )}
                      >
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => onToggleProvider(provider)}
                          className="h-auto w-full items-start justify-between gap-3 whitespace-normal rounded-none px-4 py-3 text-left"
                        >
                          <div className="flex flex-1 flex-col gap-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-[13px] font-semibold text-foreground">
                                {provider.name}
                              </span>
                              {renderProviderBadge(provider)}
                              {provider.custom && (
                                <Badge variant="info">
                                  {t('settings.backends.opencodeCustomProviderBadge')}
                                </Badge>
                              )}
                              {isDefault && (
                                <Badge variant="warning" className="gap-1">
                                  <Star className="size-3" />
                                  {t('settings.backends.opencodeDefaultBadge')}
                                </Badge>
                              )}
                            </div>
                            <span className="font-mono text-[11px] text-muted">
                              {provider.id} ·{' '}
                              {t('settings.backends.opencodeProviderModelsCount', {
                                count: provider.models.length,
                              })}
                            </span>
                            {provider.description && (
                              <span className="text-[12px] leading-relaxed text-muted">
                                {provider.description}
                              </span>
                            )}
                          </div>
                          {expanded ? (
                            <ChevronUp className="mt-0.5 size-4 shrink-0 text-muted" />
                          ) : (
                            <ChevronDown className="mt-0.5 size-4 shrink-0 text-muted" />
                          )}
                        </Button>

                        {expanded && (
                          <div className="flex flex-col gap-4 border-t border-border bg-background px-4 py-4">
                            <div className="flex flex-wrap items-center gap-2">
                              <Button
                                type="button"
                                variant="secondary"
                                size="xs"
                                onClick={closeProvider}
                              >
                                <X className="size-3.5" />
                                {t('settings.backends.opencodeProviderCollapse')}
                              </Button>
                              {providerHasAuth(provider) && (
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="xs"
                                  onClick={() => void onRemoveProviderAuth(provider)}
                                  disabled={edit.deletingProvider || edit.saving}
                                  className="text-destructive"
                                >
                                  {edit.deletingProvider ? (
                                    <RefreshCw className="size-3.5 animate-spin" />
                                  ) : (
                                    <Trash2 className="size-3.5" />
                                  )}
                                  {/* Label reflects what's actually
                                      about to be removed (OpenCode's
                                      auth.json carries exactly one
                                      entry per provider at a time —
                                      ``api`` or ``oauth`` — so the
                                      single DELETE drops whichever
                                      is set; we just need to be
                                      honest in the label). */}
                                  {provider.active_auth_type === 'oauth'
                                    ? t('settings.backends.opencodeProviderRemoveOauth')
                                    : t('settings.backends.opencodeProviderRemove')}
                                </Button>
                              )}
                              {provider.custom && (
                                <Button
                                  type="button"
                                  variant="outline"
                                  size="xs"
                                  onClick={() => void onDeleteCustomProvider(provider)}
                                  disabled={edit.removing || edit.saving}
                                  className="text-destructive"
                                >
                                  {edit.removing ? (
                                    <RefreshCw className="size-3.5 animate-spin" />
                                  ) : (
                                    <Trash2 className="size-3.5" />
                                  )}
                                  {t('settings.backends.opencodeCustomProviderRemove')}
                                </Button>
                              )}
                              {!provider.configured && !isDefault && provider.local && (
                                <span className="text-[11px] text-muted">
                                  {t('settings.backends.opencodeProviderLocalHint')}
                                </span>
                              )}
                            </div>

                            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                              <div className="flex flex-col gap-3">
                                {provider.oauth_available && (
                                  // In-card OAuth panel: reuses the
                                  // same component Claude / Codex use,
                                  // configured for OpenCode's
                                  // per-provider HTTP flow. Drives the
                                  // ``/api/backend/opencode/provider/<id>/auth/oauth/start``
                                  // endpoint and polls for completion
                                  // — no terminal commands required.
                                  <BackendOAuthPanel
                                    backend="opencode"
                                    opencodeProviderId={provider.id}
                                    signedIn={provider.configured}
                                    title={t('settings.backends.opencodeProviderOauthPanelTitle', {
                                      name: provider.name,
                                    })}
                                    subtitle={t('settings.backends.opencodeProviderOauthPanelSubtitle')}
                                    hideRemove
                                    onSuccess={() => {
                                      notifyOpenCodeModelOptionsChanged();
                                      // Refresh the providers list so
                                      // the just-authorised provider
                                      // flips to "configured" and the
                                      // masked-key block (if any)
                                      // appears.
                                      void loadProviders();
                                    }}
                                  />
                                )}

                                <div className="flex flex-col gap-1.5">
                                  <Label
                                    htmlFor={`opencode-key-${provider.id}`}
                                    className="text-[11px] font-medium uppercase text-muted"
                                  >
                                    {t('settings.backends.opencodeProviderApiKey')}
                                  </Label>
                                  {providerHasAuth(provider) && provider.api_key_masked && !edit.editingKey ? (
                                    // Masked-preview affordance ported from
                                    // the Claude / Codex pages: show the
                                    // saved key as a read-only mono-typed
                                    // value with a pencil to swap in a
                                    // fresh one. Saves the user from
                                    // re-typing the secret on baseURL-only
                                    // edits.
                                    <div className="flex items-center gap-2 rounded-md border border-border bg-foreground/[0.04] px-3 py-2">
                                      <KeyRound className="size-4 shrink-0 text-muted" />
                                      <code className="flex-1 truncate font-mono text-[12px] text-foreground">
                                        {provider.api_key_masked}
                                      </code>
                                      <Button
                                        type="button"
                                        variant="ghost"
                                        size="xs"
                                        onClick={() =>
                                          updateEdit(provider.id, {
                                            editingKey: true,
                                            apiKey: '',
                                          })
                                        }
                                        disabled={edit.saving || edit.removing}
                                      >
                                        <Pencil className="size-3" />
                                        {t('settings.backends.replaceApiKey')}
                                      </Button>
                                    </div>
                                  ) : (
                                    <div className="relative">
                                      <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                                      <Input
                                        id={`opencode-key-${provider.id}`}
                                        type="password"
                                        autoComplete="off"
                                        spellCheck={false}
                                        placeholder={
                                          providerHasAuth(provider)
                                            ? (t(
                                                'settings.backends.opencodeProviderApiKeyPlaceholderStored'
                                              ) as string)
                                            : (t(
                                                'settings.backends.opencodeProviderApiKeyPlaceholder'
                                              ) as string)
                                        }
                                        value={edit.apiKey}
                                        onChange={(e) =>
                                          updateEdit(provider.id, { apiKey: e.target.value })
                                        }
                                        className="pl-9 font-mono"
                                        disabled={edit.saving}
                                        autoFocus={edit.editingKey}
                                      />
                                    </div>
                                  )}
                                  <div className="flex items-center justify-between gap-2">
                                    <p className="text-[11px] text-muted">
                                      {providerHasAuth(provider)
                                        ? t('settings.backends.opencodeProviderApiKeyStored')
                                        : t('settings.backends.opencodeProviderApiKeyMissing')}
                                    </p>
                                    {providerHasAuth(provider) && edit.editingKey && (
                                      <Button
                                        type="button"
                                        variant="link"
                                        size="xs"
                                        className="h-auto px-0 text-[11px] text-muted"
                                        onClick={() =>
                                          updateEdit(provider.id, {
                                            editingKey: false,
                                            apiKey: '',
                                          })
                                        }
                                      >
                                        {t('common.cancel')}
                                      </Button>
                                    )}
                                  </div>
                                </div>

                                <div className="flex flex-col gap-1.5">
                                  <Label
                                    htmlFor={`opencode-base-url-${provider.id}`}
                                    className="text-[11px] font-medium uppercase text-muted"
                                  >
                                    {t('settings.backends.opencodeProviderBaseUrl')}
                                  </Label>
                                  <div className="flex gap-2">
                                    <Input
                                      id={`opencode-base-url-${provider.id}`}
                                      type="url"
                                      autoComplete="off"
                                      spellCheck={false}
                                      placeholder={
                                        t(
                                          'settings.backends.opencodeProviderBaseUrlPlaceholder'
                                        ) as string
                                      }
                                      value={edit.baseUrl}
                                      onChange={(e) =>
                                        updateEdit(provider.id, { baseUrl: e.target.value })
                                      }
                                      className="font-mono"
                                      disabled={edit.saving}
                                    />
                                    <Button
                                      type="button"
                                      variant="secondary"
                                      size="sm"
                                      onClick={() => updateEdit(provider.id, { baseUrl: '' })}
                                      disabled={!edit.baseUrl || edit.saving}
                                    >
                                      <RotateCcw className="size-3.5" />
                                    </Button>
                                  </div>
                                  <p className="text-[11px] text-muted">
                                    {t('settings.backends.opencodeProviderBaseUrlHint')}
                                  </p>
                                </div>

                                {edit.error && (
                                  <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
                                    {edit.error}
                                  </div>
                                )}

                                {(() => {
                                  // Save button only renders when the
                                  // user has something to commit:
                                  // typed a fresh key, or modified
                                  // the Base URL relative to what's
                                  // saved on the provider. Mirrors
                                  // the Claude / Codex dirty-state
                                  // pattern — a permanent Save button
                                  // is noisy and confusing.
                                  const keyDirty = edit.apiKey.trim().length > 0;
                                  const savedBase = (provider.base_url || '').trim();
                                  const baseDirty = edit.baseUrl.trim() !== savedBase;
                                  const dirty = keyDirty || baseDirty;
                                  if (!dirty) return null;
                                  return (
                                    <div className="flex flex-wrap items-center justify-end gap-2">
                                      <Button
                                        type="button"
                                        variant="brand"
                                        size="sm"
                                        onClick={() => void onSaveProviderAuth(provider)}
                                        disabled={edit.saving}
                                      >
                                        {edit.saving ? (
                                          <RefreshCw className="size-3.5 animate-spin" />
                                        ) : (
                                          <Save className="size-3.5" />
                                        )}
                                        {edit.saving
                                          ? t('common.saving')
                                          : t('settings.backends.opencodeProviderSave')}
                                      </Button>
                                    </div>
                                  );
                                })()}

                                {/* Per-provider connectivity probe.
                                    Gated on ``configured`` because
                                    testing an unconfigured provider
                                    always fails with
                                    ``invalid_credentials`` — adds
                                    noise without signal. Each card
                                    owns its own panel so users can
                                    debug providers independently. */}
                                {provider.configured && (
                                  <OpencodeProviderTestPanel
                                    providerId={provider.id}
                                    providerName={provider.name}
                                    models={provider.models}
                                    defaultModel={provider.default_model}
                                  />
                                )}
                              </div>

                              <div className="flex flex-col gap-3">
                                <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface px-3 py-3">
                                  <div className="flex flex-wrap items-center justify-between gap-2">
                                    <Label
                                      htmlFor={`opencode-model-${provider.id}`}
                                      className="text-[11px] font-medium uppercase text-muted"
                                    >
                                      {t('settings.backends.opencodeProviderAddModel')}
                                    </Label>
                                    <Badge variant="secondary" className="font-mono">
                                      {t('settings.backends.opencodeProviderUserModelsCount', {
                                        count:
                                          provider.model_entries?.filter((item) => item.user_managed)
                                            .length || 0,
                                      })}
                                    </Badge>
                                  </div>
                                  <div className="flex flex-col gap-2">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <Input
                                        id={`opencode-model-${provider.id}`}
                                        type="text"
                                        autoComplete="off"
                                        spellCheck={false}
                                        placeholder={
                                          t('settings.backends.opencodeProviderModelPlaceholder') as string
                                        }
                                        value={edit.modelId}
                                        onChange={(e) =>
                                          updateEdit(provider.id, { modelId: e.target.value })
                                        }
                                        className="min-w-56 flex-1 font-mono"
                                        disabled={edit.modelSaving}
                                      />
                                      <Button
                                        type="button"
                                        variant="brand"
                                        size="sm"
                                        onClick={() => void onSaveProviderModel(provider)}
                                        disabled={edit.modelSaving}
                                      >
                                        {edit.modelSaving ? (
                                          <RefreshCw className="size-3.5 animate-spin" />
                                        ) : (
                                          <Plus className="size-3.5" />
                                        )}
                                        {t('settings.backends.opencodeProviderModelAdd')}
                                      </Button>
                                    </div>
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="text-[11px] font-medium uppercase text-muted">
                                        {t('settings.backends.opencodeProviderModelReasoning')}
                                      </span>
                                      {REASONING_EFFORTS.map((effort) => (
                                        <Button
                                          key={effort}
                                          type="button"
                                          variant={
                                            edit.reasoningEfforts.includes(effort)
                                              ? 'accent'
                                              : 'outline'
                                          }
                                          size="xs"
                                          className="px-2 text-[11px]"
                                          onClick={() => toggleReasoningEffort(provider.id, effort)}
                                          disabled={edit.modelSaving}
                                        >
                                          <Checkbox
                                            checked={edit.reasoningEfforts.includes(effort)}
                                            presentational
                                          />
                                          {effort}
                                        </Button>
                                      ))}
                                    </div>
                                    <p className="text-[11px] text-muted">
                                      {t('settings.backends.opencodeProviderModelHint')}
                                    </p>
                                  </div>
                                </div>

                                <div className="flex flex-col gap-2">
                                  <Label className="text-[11px] font-medium uppercase text-muted">
                                    {t('settings.backends.opencodeProviderModels')}
                                  </Label>
                                  <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-surface px-3 py-2">
                                    {provider.models.length === 0 ? (
                                      <p className="text-[12px] text-muted">
                                        {t('settings.backends.opencodeProviderModelsEmpty')}
                                      </p>
                                    ) : (
                                      <ul className="flex flex-col gap-1">
                                        {provider.models.map((model) => {
                                          const entry = provider.model_entries?.find(
                                            (item) => item.id === model,
                                          );
                                          const reasoningEfforts = entry?.reasoning_efforts || [];
                                          return (
                                          <li
                                            key={model}
                                            className="flex items-center gap-2 font-mono text-[12px] text-foreground"
                                          >
                                            <Cpu className="size-3 text-muted" />
                                            <span className="truncate">{model}</span>
                                            {entry?.user_managed && (
                                              <Badge variant="info" className="ml-auto">
                                                {t('settings.backends.opencodeProviderUserModel')}
                                              </Badge>
                                            )}
                                            {reasoningEfforts.length > 0 && (
                                              <span className="text-[10px] text-muted">
                                                {reasoningEfforts.join('/')}
                                              </span>
                                            )}
                                            {provider.default_model === model && (
                                              <Badge
                                                variant="success"
                                                className={entry?.user_managed ? undefined : 'ml-auto'}
                                              >
                                                {t(
                                                  'settings.backends.opencodeProviderDefaultModel'
                                                )}
                                              </Badge>
                                            )}
                                            {entry?.user_managed && (
                                              <Button
                                                type="button"
                                                variant="ghost"
                                                size="icon"
                                                className="size-6"
                                                onClick={() => void onDeleteProviderModel(provider, model)}
                                                disabled={edit.removingModelId === model}
                                              >
                                                {edit.removingModelId === model ? (
                                                  <RefreshCw className="size-3 animate-spin" />
                                                ) : (
                                                  <Trash2 className="size-3 text-destructive" />
                                                )}
                                              </Button>
                                            )}
                                          </li>
                                          );
                                        })}
                                      </ul>
                                    )}
                                  </div>
                                </div>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="flex items-start gap-2 rounded-lg border border-border bg-surface-2/60 px-3 py-2.5">
                <Info className="mt-0.5 size-3.5 shrink-0 text-muted" />
                <p className="text-[12px] leading-relaxed text-muted">
                  {t('settings.backends.opencodeProvidersInfo')}
                </p>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
};
