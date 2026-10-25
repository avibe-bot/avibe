// A hosted tool call's card, as a ``toolcall`` row carries it in ``content.hosted``
// (``core/hosted_activity.py``, version 1; the shared fixtures are
// ``hostedActivity.fixture.json`` and, for search services,
// ``hostedServiceActivity.fixture.json``). The row's ``text`` is the IM line; the
// Web renders the card from these fields instead.

export type HostedToolName = 'web_search' | 'x_search' | 'image_generate' | 'web_fetch';

export type HostedActivitySource = {
  /** Only a URL a browser should open; a source without one is named by its title. */
  url?: string;
  title?: string;
  snippet?: string;
  page_age?: string;
};

/** One candidate a search chain reached, as the producer projected it now: ``label`` is the Source's current
 *  name, and ``reason_key`` (what happened) and ``remedy_key`` (what the Source needs now) are keys under
 *  ``chat.agentActivity.hosted``, decided once for the Web card and the IM line alike. */
export type HostedActivityAttempt = {
  source_id: string;
  label: string;
  kind: 'service' | 'model' | 'local';
  outcome: 'completed' | 'failed' | 'skipped';
  error_code?: string;
  skipped?: 'blocked' | 'disabled' | 'cooling' | 'query_too_long' | 'unavailable' | 'unsupported_type' | 'service_busy';
  retry_at?: string;
  needs_action?: string;
  cost?: { basis: 'reported' | 'list' | 'included'; usd?: number; estimate?: boolean };
  superseded?: boolean;
  reason_key?: string;
  remedy_key?: string;
};

/** A completed search that moved past Sources that failed or were blocked, and the Source that answered. */
export type HostedActivitySwitch = { sources: string[]; reason_key: string; to: string };

export type HostedActivityImage = {
  path: string;
  mime: string;
  width?: number;
  height?: number;
  /** Workbench only: the media-proxy URL the row registered for the file. */
  media_url?: string;
  bytes?: number;
};

export type WebFetchRecord = {
  status: 'completed' | 'failed';
  requested_url: string;
  final_url: string | null;
  canonical_url: string | null;
  title: string | null;
  content_type: string | null;
  source_content_type: string | null;
  content: string;
  size_bytes: number;
  source_size_bytes: number | null;
  retrieved_at: string;
  truncated: boolean;
  truncation_reason: 'budget' | 'provider_limit' | null;
  budget_method: 'utf8_bytes_div4_v1';
  error?: string;
  error_code?: string;
  attempts: HostedActivityAttempt[];
  transport_degraded?: boolean;
  transport_degraded_reason?: string;
  charset_fallback?: boolean;
};

export type HostedActivity = {
  version: 1;
  call_id: string;
  tool: HostedToolName;
  /** Natively in the model's response, by a search executor, or on an image endpoint directly. */
  mode: 'native' | 'proxy' | 'direct';
  status: 'completed' | 'failed' | 'incomplete' | 'pending';
  /** Model-facing English, shown as is only for a provider's own reason. */
  error?: string;
  /** The stable code of a reason Avibe worded itself, shown in the reader's language. */
  error_code?: string;
  /** The query, prompt, page or thread the call named. */
  subject?: string;
  action?: string;
  extra_queries?: string[];
  /** Vendor id of whoever ran it: the response's for native, the executor's for proxy. */
  provider?: string;
  /** Whether a search service or a model executor ran it; absent when the record does not say. */
  executor_kind?: 'service' | 'model' | 'local';
  fetch?: WebFetchRecord;
  source_id?: string;
  source_name?: string;
  model?: string;
  sources: HostedActivitySource[];
  images: HostedActivityImage[];
  size?: string;
  quality?: string;
  revised_prompt?: string;
  duration_ms?: number;
  /** The Source that refused to run this search natively, so it ran by proxy instead; absent otherwise. */
  fallback_from?: { source_id: string; source_name?: string };
  /** Every candidate a search chain reached; absent for a record that has none, as a model-only chain's. */
  attempts?: HostedActivityAttempt[];
  switch?: HostedActivitySwitch;
  /** Absent when no price is known: unknown is never shown as zero. ``calls`` is how many calls it covers,
   *  ``at_least`` marks a lower bound (an attempt reported no price), and ``estimate`` a vendor's own estimate. */
  cost?: { basis: 'reported' | 'list' | 'included'; usd?: number; calls: number; estimate?: boolean; at_least?: boolean };
};

const TOOLS: readonly string[] = ['web_search', 'x_search', 'image_generate', 'web_fetch'];
const STATUSES: readonly string[] = ['completed', 'failed', 'incomplete', 'pending'];
const optionalString = (value: unknown): boolean => value === undefined || typeof value === 'string';
const attemptFrom = (raw: unknown): HostedActivityAttempt | undefined => {
  if (!raw || typeof raw !== 'object') return undefined;
  const attempt = raw as Record<string, unknown>;
  if (typeof attempt.source_id !== 'string' || typeof attempt.label !== 'string') return undefined;
  if (attempt.kind !== 'service' && attempt.kind !== 'model' && attempt.kind !== 'local') return undefined;
  if (attempt.outcome !== 'completed' && attempt.outcome !== 'failed' && attempt.outcome !== 'skipped') return undefined;
  if (!optionalString(attempt.reason_key) || !optionalString(attempt.remedy_key)) return undefined;
  return attempt as unknown as HostedActivityAttempt;
};
const switchFrom = (raw: unknown): HostedActivitySwitch | undefined => {
  if (!raw || typeof raw !== 'object') return undefined;
  const note = raw as Record<string, unknown>;
  if (!Array.isArray(note.sources) || !note.sources.every((name) => typeof name === 'string')) return undefined;
  return typeof note.reason_key === 'string' && typeof note.to === 'string' ? note as unknown as HostedActivitySwitch : undefined;
};

const fetchFrom = (raw: unknown): WebFetchRecord | undefined => {
  if (!raw || typeof raw !== 'object') return undefined;
  const record = raw as Record<string, unknown>;
  if (typeof record.requested_url !== 'string' || typeof record.content !== 'string'
    || typeof record.size_bytes !== 'number' || typeof record.truncated !== 'boolean'
    || typeof record.retrieved_at !== 'string' || record.budget_method !== 'utf8_bytes_div4_v1'
    || (record.status !== 'completed' && record.status !== 'failed')) return undefined;
  if (record.transport_degraded !== undefined && typeof record.transport_degraded !== 'boolean') return undefined;
  if (record.transport_degraded_reason !== undefined && (typeof record.transport_degraded_reason !== 'string' || record.transport_degraded_reason.length > 400)) return undefined;
  if (record.charset_fallback !== undefined && typeof record.charset_fallback !== 'boolean') return undefined;
  if (!['title', 'final_url', 'canonical_url', 'content_type', 'source_content_type'].every((key) => record[key] === null || typeof record[key] === 'string')) return undefined;
  if (record.content.length > 48000 || !Number.isSafeInteger(record.size_bytes) || record.size_bytes < 0 || record.size_bytes > 48000) return undefined;
  return record as unknown as WebFetchRecord;
};

/** The card a row carries, or undefined for anything that is not one this client reads. */
export const hostedActivityFrom = (raw: unknown): HostedActivity | undefined => {
  if (!raw || typeof raw !== 'object') return undefined;
  const card = raw as Record<string, unknown>;
  if (card.version !== 1 || typeof card.call_id !== 'string') return undefined;
  if (typeof card.tool !== 'string' || !TOOLS.includes(card.tool)) return undefined;
  if (card.mode !== 'native' && card.mode !== 'proxy' && card.mode !== 'direct') return undefined;
  if (typeof card.status !== 'string' || !STATUSES.includes(card.status)) return undefined;
  return {
    ...(card as unknown as HostedActivity),
    sources: Array.isArray(card.sources) ? (card.sources as HostedActivitySource[]).filter((s) => typeof s?.url === 'string' || typeof s?.title === 'string') : [],
    images: Array.isArray(card.images) ? (card.images as HostedActivityImage[]).filter((i) => typeof i?.path === 'string') : [],
    attempts: Array.isArray(card.attempts) ? card.attempts.map(attemptFrom).filter((attempt): attempt is HostedActivityAttempt => attempt !== undefined) : undefined,
    switch: switchFrom(card.switch),
    fetch: fetchFrom(card.fetch),
  };
};

/**
 * Whether the call finished: the one gate every surface reads. Only a completed
 * call claims how it ran (its mode, who ran it, how long it took) and what it
 * produced (counts, sources, images); a cut-off or failed call shows its status
 * instead. A known cost shows whatever the status: a charge stands whatever
 * happened after it. The IM line reads the same rule (`HostedActivity.completed`).
 */
export const hostedCompleted = (card: HostedActivity): boolean => card.status === 'completed';

/** The media token of a proxy URL (``/api/media/<token>?w=&h=``), which joins a card's image to the answer's. */
export const mediaToken = (url: string | undefined): string | null => {
  if (!url) return null;
  const match = /\/api\/media\/([^/?#]+)/.exec(url);
  return match ? match[1] : null;
};
