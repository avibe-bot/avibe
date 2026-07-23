// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).
import type { AgentSupply, Source } from './types';

const CURRENCY_SYMBOL: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€' };

/** Monthly spend as symbol + amount (1 decimal), e.g. "¥12.4". */
export function formatSpend(cents: number, currency?: string | null): string {
  const symbol = CURRENCY_SYMBOL[currency ?? 'CNY'] ?? '';
  return `${symbol}${(cents / 100).toFixed(1)}`;
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
