// How a migration scan divides into consent groups.
//
// Extracted from `MigrationDialog` unchanged so setup's provider screen can ask
// the same question the dialog asks — which backends may be taken over together,
// and which rows a selection actually submits — without owning a second answer.
// A group is the unit of consent, not a row: shared persisted assignments link
// backends into one custody boundary. Rows the Hub cannot carry are not shown at
// all — they stay native and never block their group, unless the row says the
// backend's native config cannot be parsed at all.
import type { TranslationKey } from '@/i18n/types';
import type { AgentBackend, MigrationItem, MigrationScan } from './types';

/**
 * A scan plus the backends consented to against it.
 *
 * Declared here rather than imported from the setup flow contract so the
 * dependency runs one way: setup's `SetupProviderSelection` is structurally this
 * shape, and Model Hub never has to know the onboarding module exists.
 */
export type MigrationSelection = {
  scan: MigrationScan | null;
  selectedBackends: AgentBackend[];
};

export const BACKEND_ORDER: AgentBackend[] = ['claude', 'codex', 'opencode'];

export const isImportable = (item: MigrationItem) => item.proposed_action === 'import';

/**
 * The transitive closure of backends that share persisted assignments with any
 * of `backends`.
 *
 * Resolve from every row of an included backend, not just the eligible
 * entry-point rows. Scope filters cannot conceal a shared-file consumer.
 */
export function requiredBackends(
  items: MigrationItem[],
  backends: Iterable<AgentBackend>,
): Set<AgentBackend> {
  const required = new Set(backends);
  let expanded = true;
  while (expanded) {
    expanded = false;
    for (const item of items) {
      if (!required.has(item.backend)) continue;
      for (const backend of item.required_backends ?? []) {
        if (required.has(backend)) continue;
        required.add(backend);
        expanded = true;
      }
    }
  }
  return required;
}

export type MigrationGroup = {
  backend: AgentBackend;
  /** This backend's own rows, in scan order. */
  rows: MigrationItem[];
  importRows: MigrationItem[];
  /** Every backend this one must migrate with, including itself. */
  required: Set<AgentBackend>;
  /** Rows across the whole linked group this scope may take over. */
  linkedImportRows: MigrationItem[];
  /** Linked rows it may not: a blocker, or a credential outside the scope. */
  blockedRows: MigrationItem[];
  blocked: boolean;
};

/** Backends the scope reaches, widened to whole consent groups. */
export const scopedBackends = (
  items: MigrationItem[],
  eligible: (item: MigrationItem) => boolean,
): Set<AgentBackend> =>
  requiredBackends(items, items.filter(eligible).map((item) => item.backend));

/**
 * The dialog's rows, grouped. `items` stays the complete scan so a group's
 * linked rows can include a backend the scope itself never selected.
 *
 * Two scopes, because an entry point asks two different questions. `eligible` says
 * which rows it was opened FROM: their backends, widened to their custody closures,
 * are what appears at all. `takeable` says which of the rows that appear it may
 * actually take OVER — and an importable row in view that fails it blocks its group
 * rather than being quietly left behind, because the server migrates every carried
 * credential of a backend together and refuses a batch that omits one.
 *
 * Settings may take over everything the scan proposes importing, which is the
 * default and makes the second scope invisible there. Setup may not: it is scoped to
 * API keys, and a backend whose credentials also include an importable subscription
 * store is a Settings review rather than a setup card.
 */
export function groupMigrationCandidates(
  items: MigrationItem[],
  eligible: (item: MigrationItem) => boolean,
  takeable: (item: MigrationItem) => boolean = () => true,
): MigrationGroup[] {
  // Only what the Hub can carry is a candidate. Everything else stays native,
  // shadowed by the Hub launch, so offering it would only show what cannot move.
  const carried = items.filter(isImportable);
  const scope = scopedBackends(carried, eligible);
  const candidates = carried.filter((item) => scope.has(item.backend));
  const importableHere = (item: MigrationItem) => isImportable(item) && takeable(item);
  return BACKEND_ORDER.map((backend) => {
    const rows = candidates.filter((item) => item.backend === backend);
    const importRows = rows.filter(importableHere);
    const required = requiredBackends(carried, [backend]);
    const linkedRows = candidates.filter((item) => required.has(item.backend));
    const linkedImportRows = linkedRows.filter(importableHere);
    // A linked backend with nothing to carry can never join the batch, yet the
    // server still requires it: the shared credential it reads would be moved
    // out from under it. Its native rows explain why the group cannot move.
    const stranded = [...required].filter((linked) => !carried.some((item) => item.backend === linked));
    // A backend whose native config cannot be parsed would fail every Hub
    // launch, so its blocker row blocks the group the other native rows don't.
    const blockedRows = [...new Set([
      ...linkedRows.filter((item) => !importableHere(item)),
      ...items.filter((item) => stranded.includes(item.backend) && !isImportable(item)),
      ...items.filter((item) => required.has(item.backend) && item.config_blocker),
    ])];
    return {
      backend,
      rows,
      importRows,
      required,
      linkedImportRows,
      blockedRows,
      blocked: blockedRows.length > 0 || stranded.length > 0,
    };
  }).filter((group) => group.rows.length > 0);
}

/** Whether a group can be consented to at all. */
export const groupSelectable = (group: MigrationGroup): boolean =>
  !group.blocked && group.importRows.length > 0;

/**
 * The rows a scope may actually take over, group by group.
 *
 * The same question `groupSelectable` answers for the dialog, asked without
 * rendering one: a surface that counts candidates by filtering rows would count a
 * key whose group is blocked and then open a review with nothing to press. Offer
 * and batch have to come from one rule, and this is it.
 */
export const takeableImportRows = (
  items: MigrationItem[],
  eligible: (item: MigrationItem) => boolean,
  takeable: (item: MigrationItem) => boolean = () => true,
): MigrationItem[] =>
  groupMigrationCandidates(items, eligible, takeable)
    .filter(groupSelectable)
    .flatMap((group) => group.importRows);

const BLOCKED_NOTE_KEYS = new Set<string>([
  'settings.models.migration.blocked.config',
  'settings.models.migration.blocked.environment',
  'settings.models.migration.blocked.credential',
  'settings.models.migration.blocked.dynamic_shell',
  'settings.models.migration.blocked.ambiguous_shell',
  'settings.models.migration.blocked.unreadable',
  'settings.models.migration.blocked.reference',
  'settings.models.migration.blocked.helper',
  'settings.models.migration.blocked.token',
  'settings.models.migration.blocked.headers',
  'settings.models.migration.blocked.transport',
] satisfies TranslationKey[]);

export const BLOCKED_REASON_FALLBACK_KEY = 'settings.models.migration.blocked.fallback' satisfies TranslationKey;

/**
 * Why a row this entry point could otherwise have taken is nonetheless blocked.
 *
 * The server's own reasons all say the credential cannot be imported at all, which
 * is untrue of a row that is importable and merely out of this scope's reach —
 * unreachable in Settings, whose scope takes over everything the scan proposes.
 *
 * `notes_key` is an open vocabulary, so a key this app does not ship copy for
 * resolves to the generic line rather than to its own machine name. Which copy
 * explains a blocked row lives with the rule that decides a row is blocked, so the
 * dialog that lists the group and any surface that has to explain one row of it
 * cannot answer the same question two ways.
 */
export const blockedReasonKey = (item: MigrationItem): TranslationKey => {
  if (isImportable(item)) return 'onboarding.import.outOfScope';
  return item.notes_key && BLOCKED_NOTE_KEYS.has(item.notes_key)
    ? (item.notes_key as TranslationKey)
    : BLOCKED_REASON_FALLBACK_KEY;
};

/**
 * The rows a selection submits: every importable row of every selected backend.
 *
 * Unscoped on purpose, and safe because a backend reaches a selection only through
 * `groupSelectable` — whose group, by the rule above, has no linked row the scope
 * cannot take. The server enforces the same completeness from its side: a batch that
 * omits a row of a backend it is migrating is refused outright.
 */
export const appliableItems = (
  items: MigrationItem[],
  selected: ReadonlySet<AgentBackend>,
): MigrationItem[] =>
  items.filter((item) => selected.has(item.backend) && isImportable(item));
