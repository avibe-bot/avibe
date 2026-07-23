// OpenCode identifier scheme (spec §4.4, locked 07-23; opencode-overlay.md).
// Identifiers are `provider/model-id`. The provider segment is the standard
// vendor id; unrecognizable vendors fall back to a single `custom/` provider.
// Identifiers are STABLE across Hub/Direct switches and across source
// add/remove/failover — they never encode a concrete source. Users never
// hand-assemble the string: the menu checkboxes and the custom-model form
// generate and preview it.
import type { Accent } from '../vendorMeta';
import { sourceAccent } from '../vendorMeta';
import type { Source } from '../types';

// Standard vendor ids that map 1:1 to an OpenCode provider prefix (identical to
// native OpenCode usage — no `avibe-` namespace).
const STANDARD_VENDORS = new Set([
  'anthropic',
  'openai',
  'zhipuai',
  'kimi',
  'xai',
  'google',
  'deepseek',
  'qwen',
  'mistral',
  'groq',
]);

// Model-id family → vendor, for sources whose own vendor isn't a standard id
// (e.g. a custom relay endpoint supplying `glm-*`). First match wins.
const MODEL_FAMILY: Array<[RegExp, string]> = [
  [/^claude/i, 'anthropic'],
  [/^(gpt|o[0-9]|chatgpt|text-embedding)/i, 'openai'],
  [/^glm/i, 'zhipuai'],
  [/^(kimi|moonshot)/i, 'kimi'],
  [/^grok/i, 'xai'],
  [/^gemini/i, 'google'],
  [/^deepseek/i, 'deepseek'],
  [/^qwen/i, 'qwen'],
];

/**
 * Provider segment for a (source vendor, model id): a standard source vendor
 * wins; otherwise infer from the model-id family; otherwise `custom`. This is
 * why `relay.example` (vendor `custom`) supplying `glm-5.2-air` still previews
 * as `zhipuai/glm-5.2-air` (frame 08).
 */
export function inferProvider(sourceVendor: string, modelId: string): string {
  if (STANDARD_VENDORS.has(sourceVendor)) return sourceVendor;
  for (const [re, vendor] of MODEL_FAMILY) if (re.test(modelId)) return vendor;
  return 'custom';
}

/** Full prefixed identifier for a (source vendor, model id). */
export function buildIdentifier(sourceVendor: string, modelId: string): string {
  return `${inferProvider(sourceVendor, modelId)}/${modelId}`;
}

// ── Grouped menu model, derived from the ordered sources list ──────────────

export type MenuModelRow = {
  /** Full prefixed identifier, e.g. `zhipuai/glm-5.2`. */
  identifier: string;
  /** Group prefix (the provider segment). */
  provider: string;
  /** Bare model id (no prefix). */
  modelId: string;
  displayName: string | null;
  /** True when any supplying entry is a manual (custom) model. */
  isCustom: boolean;
  /** Supplying sources, in the order they appear in the input list (priority). */
  sources: Source[];
  /** Deduped supplying-source accents, for the row's supply dots. */
  accents: Accent[];
};

export type MenuGroup = {
  provider: string;
  rows: MenuModelRow[];
};

/**
 * Build the grouped, deduped model rows for the OpenCode open menu (frame 05r).
 * `sources` MUST already be in priority order so the supply dots and candidate
 * order track the 来源 band. The same identifier supplied by several sources
 * collapses into one row carrying every supplying source.
 */
export function buildMenuGroups(sources: Source[]): MenuGroup[] {
  const byIdentifier = new Map<string, MenuModelRow>();
  for (const source of sources) {
    for (const model of source.models) {
      const identifier = buildIdentifier(source.vendor, model.id);
      let row = byIdentifier.get(identifier);
      if (!row) {
        row = {
          identifier,
          provider: identifier.slice(0, identifier.indexOf('/')),
          modelId: model.id,
          displayName: model.display_name ?? null,
          isCustom: model.provenance === 'manual',
          sources: [],
          accents: [],
        };
        byIdentifier.set(identifier, row);
      }
      if (model.display_name && !row.displayName) row.displayName = model.display_name;
      if (model.provenance === 'manual') row.isCustom = true;
      row.sources.push(source);
      const accent = sourceAccent(source);
      if (!row.accents.includes(accent)) row.accents.push(accent);
    }
  }
  // Group by provider, preserving first-seen order (which follows priority).
  const groups: MenuGroup[] = [];
  const byProvider = new Map<string, MenuGroup>();
  for (const row of byIdentifier.values()) {
    let group = byProvider.get(row.provider);
    if (!group) {
      group = { provider: row.provider, rows: [] };
      byProvider.set(row.provider, group);
      groups.push(group);
    }
    group.rows.push(row);
  }
  return groups;
}

// ── Fixed-menu (mapping) helpers ───────────────────────────────────────────

export type TargetModel = {
  /** Bare model id. */
  id: string;
  displayName: string | null;
  /** Sources able to supply it, in priority order. */
  sources: Source[];
  accents: Accent[];
};

/**
 * Distinct target models a fixed-menu override can point at — the union of every
 * source's supplied model ids, in priority order (frame 04 dropdown).
 */
export function buildTargetModels(sources: Source[]): TargetModel[] {
  const byId = new Map<string, TargetModel>();
  for (const source of sources) {
    for (const model of source.models) {
      let target = byId.get(model.id);
      if (!target) {
        target = { id: model.id, displayName: model.display_name ?? null, sources: [], accents: [] };
        byId.set(model.id, target);
      }
      if (model.display_name && !target.displayName) target.displayName = model.display_name;
      target.sources.push(source);
      const accent = sourceAccent(source);
      if (!target.accents.includes(accent)) target.accents.push(accent);
    }
  }
  return [...byId.values()];
}
