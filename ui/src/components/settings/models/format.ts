// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).
import type { AgentSupply, Source } from './types';

const CURRENCY_SYMBOL: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€' };

/**
 * Symbol for an ISO 4217 code — the ONLY place a currency symbol is resolved.
 *
 * USD is the fallback because every upstream vendor bills in USD; a CNY default
 * was our own invention. The mapping is kept, so a source that really does report
 * CNY (or EUR) still renders in its own currency. Returns '' for a code we cannot
 * map, so callers can fall back to a currency-free label rather than print a
 * misleading symbol.
 */
export function currencySymbol(currency?: string | null): string {
  return CURRENCY_SYMBOL[currency ?? 'USD'] ?? '';
}

/** Monthly spend as symbol + amount (1 decimal), e.g. "$12.4". */
export function formatSpend(cents: number, currency?: string | null): string {
  return `${currencySymbol(currency)}${(cents / 100).toFixed(1)}`;
}

/** Whole minutes until a cooldown retry_at (never negative). */
export function cooldownEtaMinutes(retryAt?: string | null): number {
  if (!retryAt) return 0;
  const ms = new Date(retryAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}

/** Friendly model name for a backend's current supply: prefer the supplying
 *  source's display_name for the model id, else the bare id. */
export function friendlyModelName(agent: AgentSupply, sources: Source[]): string {
  const modelId = agent.current?.model_id;
  if (!modelId) return '';
  const source = sources.find((s) => s.id === agent.current?.source_id);
  const named = source?.models.find((m) => m.id === modelId)?.display_name;
  return named || modelId;
}
