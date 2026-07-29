// Pure formatting helpers for the Model Hub UI (no i18n — callers wrap the
// returned values in translated templates).
import { buildIdentifier } from './menus/identifiers';
import type { AgentSupply, Source, SuppliedModel } from './types';

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
 *
 * Two axes decide the lookup — WHICH SOURCE may answer, and WHICH READING of the id
 * to match on — and nesting them the wrong way round is a bug of its own. The source
 * axis outranks the form axis, because the server has already resolved which source
 * serves: an ordered scan that tries a stronger *form* match in a source the server
 * did NOT name walks past the serving source and renders someone else's model. So the
 * source is settled first, and the reading is not searched at all — it is dictated by
 * WHICH FIELD supplied the id, since the two fields are not the same shape:
 *
 *   `current.model_id`      the RESOLVED upstream id, mapping already applied — bare
 *                           even on an open menu ("NOT a valid input to the chain
 *                           query … the caller-facing identifier is prefixed").
 *                           Read exactly, and only against `current.source_id`.
 *   `selected_model_id`     the caller-facing MENU identifier — the built-in id for a
 *                           fixed menu, `vendor/model` for an open one. Read per
 *                           `menu_kind`, against every source: `current` is null
 *                           exactly when nothing is serving, so no source is
 *                           authoritative and none can be preferred honestly.
 */
export function friendlyModelName(agent: AgentSupply, sources: Source[]): string {
  const current = agent.current ?? null;
  if (current) {
    // One source, one reading. A miss renders the upstream id — which is the honest
    // answer when the serving source's inventory cannot name what it is running,
    // and strictly better than a name borrowed from a source that is not serving.
    const serving = sources.find((s) => s.id === current.source_id);
    const model = serving?.models.find((m) => m.id === current.model_id);
    return model?.display_name || current.model_id;
  }

  const selected = agent.selected_model_id ?? null;
  if (!selected) return '';

  const standardVendors = new Set(agent.standard_vendors ?? []);
  // `display_name || id`, not a `display_name` predicate: a supplied model with no
  // name still owns its id and must not fall through to a later source.
  const holds = (source: Source, model: SuppliedModel): boolean =>
    agent.menu_kind === 'open'
      ? buildIdentifier(source.vendor, model.id, standardVendors) === selected
      : model.id === selected;
  for (const source of sources) {
    const model = source.models.find((m) => holds(source, m));
    if (model) return model.display_name || model.id;
  }

  return selected;
}
