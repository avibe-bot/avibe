// How a migration scan divides into consent groups.
//
// Extracted from `MigrationDialog` unchanged so setup's provider screen can ask
// the same question the dialog asks — which backends may be taken over together,
// and which rows a selection actually submits — without owning a second answer.
// A group is the unit of consent, not a row: shared persisted assignments link
// backends into one custody boundary, and a single non-importable row inside a
// linked group blocks the whole group.
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
  /** Importable rows across the whole linked group. */
  linkedImportRows: MigrationItem[];
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
 */
export function groupMigrationCandidates(
  items: MigrationItem[],
  eligible: (item: MigrationItem) => boolean,
): MigrationGroup[] {
  const scope = scopedBackends(items, eligible);
  const candidates = items.filter((item) => scope.has(item.backend));
  return BACKEND_ORDER.map((backend) => {
    const rows = candidates.filter((item) => item.backend === backend);
    const importRows = rows.filter(isImportable);
    const required = requiredBackends(items, [backend]);
    const linkedRows = candidates.filter((item) => required.has(item.backend));
    const linkedImportRows = linkedRows.filter(isImportable);
    const blockedRows = linkedRows.filter((item) => !isImportable(item));
    return {
      backend,
      rows,
      importRows,
      required,
      linkedImportRows,
      blockedRows,
      blocked: blockedRows.length > 0,
    };
  }).filter((group) => group.rows.length > 0);
}

/** Whether a group can be consented to at all. */
export const groupSelectable = (group: MigrationGroup): boolean =>
  !group.blocked && group.importRows.length > 0;

/** The rows a selection submits: every importable row of every selected backend. */
export const appliableItems = (
  items: MigrationItem[],
  selected: ReadonlySet<AgentBackend>,
): MigrationItem[] =>
  items.filter((item) => selected.has(item.backend) && isImportable(item));
