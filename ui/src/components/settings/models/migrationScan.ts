// Whether a migration scan may run at all. Gated on the Model Hub capability so a
// disabled instance issues no scan request; separated from its callers so the gate
// is testable without rendering a surface.
//
// This module also owns the one predicate the API-key import entry is defined
// by. Setup's notice is about keys the gateway can actually take over, so the
// count, the rows offered, the batch submitted and the remainder after an import
// all read `isImportableKey` — a number that came from a different rule than the
// rows behind it is the bug this exists to prevent.
//
// It is NOT the settings migration's rule. The broader surface legitimately
// applies `keep_native` (that path creates a native_cli-channel source, see
// `migration.py`) and shows `reauth` rows for context; this entry copies neither.
import { modelsApi } from './modelsApi';
import type { MigrationItem, MigrationKind } from './types';

export const scanMigrationWhenEnabled = async (
  enabled: boolean,
  scan: typeof modelsApi.scanMigration = modelsApi.scanMigration,
) => (enabled ? scan() : null);

/** The kinds whose credential is a key the gateway can hold. `oauth_native` is
 *  a native subscription token and is excluded by kind, not by action — an
 *  OpenCode key arrives as `opencode_provider`, not `api_key`, so filtering on
 *  `api_key` alone would silently drop every OpenCode import. */
export const IMPORTABLE_KEY_KINDS: readonly MigrationKind[] = ['api_key', 'opencode_provider'];

/** One candidate the API-key import entry may count, offer, submit and re-count. */
export const isImportableKey = (item: Pick<MigrationItem, 'proposed_action' | 'kind'>): boolean =>
  item.proposed_action === 'import' && IMPORTABLE_KEY_KINDS.includes(item.kind);

/** The candidates of a scan, in scan order. */
export const importableKeys = (items: readonly MigrationItem[]): MigrationItem[] =>
  items.filter(isImportableKey);
