import { useEffect, useSyncExternalStore } from 'react';

import { useApi } from '../context/ApiContext';
import { fetchBackendFastModels, fetchBackendModelLabels } from './backendModels';

// Friendly model names ("Opus 5.5") for closed pickers, which otherwise only
// know the raw id the route stores. Every catalog answer a picker already reads
// is remembered here and in localStorage, so a trigger normally names its model
// from the first paint without a request. Only a model this browser has never
// seen a label for costs one read — per backend per page load, of what is
// already on disk (see fetchBackendModelLabels) — and a model with no label
// simply keeps its id.

const STORAGE_KEY = 'avibe.model-labels.v1';

type LabelsByBackend = Record<string, Record<string, string>>;

const isLabelMap = (value: unknown): value is Record<string, string> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
  && Object.values(value as Record<string, unknown>).every((label) => typeof label === 'string');

function readStored(): LabelsByBackend {
  try {
    const raw = typeof window !== 'undefined' ? window.localStorage.getItem(STORAGE_KEY) : null;
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out = Object.create(null) as LabelsByBackend;
    for (const [backend, labels] of Object.entries(parsed as Record<string, unknown>)) {
      if (isLabelMap(labels)) out[backend] = Object.assign(Object.create(null), labels);
    }
    return out;
  } catch {
    return {};
  }
}

let labelsByBackend: LabelsByBackend | null = null;
const listeners = new Set<() => void>();
// Backends whose labels were read (or are being read) this page load.
const readThisLoad = new Set<string>();

const current = (): LabelsByBackend => (labelsByBackend ??= readStored());

/** Record a backend's complete label answer, replacing what was known for it. */
export function rememberModelLabels(backend: string, labels: Record<string, string>): void {
  if (!backend) return;
  readThisLoad.add(backend);
  const next = Object.assign(Object.create(null), current()) as LabelsByBackend;
  next[backend] = Object.assign(Object.create(null), labels);
  labelsByBackend = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage full or unavailable: the in-memory answer still serves this load.
  }
  for (const listener of listeners) listener();
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

const labelOf = (backend: string, model: string): string | null => {
  const labels = current()[backend];
  return labels && Object.prototype.hasOwnProperty.call(labels, model) ? labels[model] : null;
};

/** The remembered label for `model`, reading the backend's labels once if this
 *  browser has none for it. Null keeps the caller on the raw id. */
export function useRememberedModelLabel(backend: string, model: string | null | undefined): string | null {
  const api = useApi();
  const label = useSyncExternalStore(subscribe, () => (backend && model ? labelOf(backend, model) : null));
  useEffect(() => {
    if (!backend || !model || label !== null || readThisLoad.has(backend)) return;
    readThisLoad.add(backend);
    fetchBackendModelLabels(api, backend).then(
      (labels) => rememberModelLabels(backend, labels),
      // No answer this load; the open menu still reads the full list.
      () => undefined,
    );
  }, [api, backend, model, label]);
  return label;
}

// Which models run at their vendor's fast tier, for a closed picker's fast-mode
// mark: it shows only while the Session asks for the fast tier AND its model
// would get it. Remembered from every catalog answer a picker reads, so the mark
// paints from the first frame. Unlike a label, the answer follows routing, which
// Settings or another client may have changed since it was stored: a picker that
// needs it reads Model Hub's persisted catalog once per backend per page load.

const FAST_STORAGE_KEY = 'avibe.fast-models.v1';

type FastByBackend = Record<string, string[]>;

function readStoredFast(): FastByBackend {
  try {
    const raw = typeof window !== 'undefined' ? window.localStorage.getItem(FAST_STORAGE_KEY) : null;
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out = Object.create(null) as FastByBackend;
    for (const [backend, models] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(models) && models.every((model) => typeof model === 'string')) out[backend] = models;
    }
    return out;
  } catch {
    return {};
  }
}

let fastByBackend: FastByBackend | null = null;
const fastListeners = new Set<() => void>();
const fastReadThisLoad = new Set<string>();

const currentFast = (): FastByBackend => (fastByBackend ??= readStoredFast());

/** Record a backend's complete answer of fast-tier models, replacing what was known for it. */
export function rememberFastModels(backend: string, models: readonly string[]): void {
  if (!backend) return;
  fastReadThisLoad.add(backend);
  const next = Object.assign(Object.create(null), currentFast()) as FastByBackend;
  next[backend] = [...models];
  fastByBackend = next;
  try {
    window.localStorage.setItem(FAST_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage full or unavailable: the in-memory answer still serves this load.
  }
  for (const listener of fastListeners) listener();
}

const subscribeFast = (listener: () => void) => {
  fastListeners.add(listener);
  return () => fastListeners.delete(listener);
};

/** Whether `model` runs at its vendor's fast tier as far as this browser knows;
 *  when `wanted`, reads the backend's answer again once per page load. */
export function useRememberedFastModel(backend: string, model: string | null | undefined, wanted: boolean): boolean {
  const api = useApi();
  const known = useSyncExternalStore(subscribeFast, () => (backend ? currentFast()[backend] ?? null : null));
  useEffect(() => {
    if (!wanted || !backend || fastReadThisLoad.has(backend)) return;
    fastReadThisLoad.add(backend);
    fetchBackendFastModels(api, backend).then(
      (models) => rememberFastModels(backend, models),
      () => undefined,
    );
  }, [api, backend, wanted]);
  return Boolean(model && known?.includes(model));
}
