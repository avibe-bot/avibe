// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).
import { buildIdentifier } from './menus/identifiers';
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

/**
 * A list of names in the reader's own punctuation — 「A、B、C」 for a Chinese
 * reader, "A, B, C" for an English one.
 *
 * Joining on a literal `、` looked locale-neutral and is not: it is Chinese
 * punctuation, and it shipped into the English UI. `narrow` + `conjunction` is the
 * pair that separates in BOTH locales and adds an "and" to neither — `unit` joins
 * Chinese with nothing at all, and the wider styles grow a 和 / "and" that an
 * 11px attribution line has no room for.
 */
export function formatNameList(names: readonly string[], locale: string): string {
  return new Intl.ListFormat(locale, { style: 'narrow', type: 'conjunction' }).format(names);
}

/** Whole minutes until a cooldown retry_at (never negative). */
export function cooldownEtaMinutes(retryAt?: string | null): number {
  if (!retryAt) return 0;
  const ms = new Date(retryAt).getTime() - Date.now();
  return Math.max(0, Math.round(ms / 60_000));
}

/**
 * Friendly name for the model a backend is set to run.
 *
 * Falls back from 「what is serving」 to 「what was selected」 on purpose: while an
 * Agent is waiting or interrupted `current` is null by contract, and a model box
 * that empties out at exactly the moment something went wrong hides the one fact
 * the user needs — WHICH model has no supply. The display name is then looked up
 * across all sources, since the source that used to name it may be the one that
 * just failed.
 *
 * A prefixed identifier is RESOLVED, never stripped. The old code cut through the
 * last slash first and unconditionally, on the reading that `SuppliedModel.id` is a
 * 「bare model id (no provider prefix)」 — but the contract puts no `pattern` on that
 * field, and 「no provider prefix」 is not 「no slash」. A relay endpoint supplies
 * `accounts/fireworks/models/llama-v3` under exactly that name, so the cut both lost
 * the metadata the id carries and matched some OTHER source's `llama-v3`, rendering
 * a model the user never selected.
 *
 * The fix is not a smarter cut — a bare tail is ambiguous in both directions, and
 * nothing about `llama-v3` says whether a prefix was dropped. It is to rebuild the
 * identifier the way the backend does. Per the frozen opencode overlay an identifier
 * is `inferProvider(source.vendor)/model.id`, so a supplied model can be matched on
 * provider AND id through the one function that owns that scheme, and a tail
 * collision becomes impossible rather than merely unlikely: `llama-v3` from a
 * `custom` source rebuilds to `custom/llama-v3`, which is not what was selected.
 *
 * An identifier neither lookup resolves renders as selected: verbose beats wrong.
 */
export function friendlyModelName(agent: AgentSupply, sources: Source[]): string {
  const modelId = agent.current?.model_id ?? agent.selected_model_id ?? null;
  if (!modelId) return '';

  const supplying = sources.find((s) => s.id === agent.current?.source_id);
  const ordered = supplying ? [supplying, ...sources.filter((s) => s !== supplying)] : sources;

  // Existence, not `display_name`: a supplied model with no name still owns its id
  // and must not fall through to the identifier branch.
  for (const source of ordered) {
    const exact = source.models.find((m) => m.id === modelId);
    if (exact) return exact.display_name || modelId;
  }

  if (modelId.includes('/')) {
    const standardVendors = new Set(agent.standard_vendors ?? []);
    for (const source of ordered) {
      const model = source.models.find(
        (m) => buildIdentifier(source.vendor, m.id, standardVendors) === modelId,
      );
      if (model) return model.display_name || model.id;
    }
  }

  return modelId;
}
