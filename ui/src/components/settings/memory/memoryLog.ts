import type { TFunction } from 'i18next';

import type { MemoryLogEntry } from '../../../context/ApiContext';
import { JSON_TREE_MAX_BYTES, JSON_TREE_MAX_NODES } from '../../../lib/filePreview';

export const MEMORY_LOG_ENTRY_LIMIT = 200;

const ENUM_LABEL_KEYS = {
  reason: {
    missing: 'missing',
    busy: 'busy',
    malformed: 'malformed',
    expired: 'expired',
    runs_missing: 'runsMissing',
    runs_busy: 'runsBusy',
    runs_malformed: 'runsMalformed',
  },
  status: {
    created: 'created',
    mixed: 'mixed',
    pending: 'pending',
    processing: 'processing',
    delivered: 'delivered',
    dead: 'dead',
    running: 'running',
    ok: 'ok',
    success: 'success',
    failed: 'failed',
    dead_letter: 'deadLetter',
    crashed: 'crashed',
    error: 'error',
    present: 'present',
    missing: 'missing',
    not_seen: 'notSeen',
    indexed: 'indexed',
  },
  callKind: {
    llm: 'llm',
    multimodal_llm: 'multimodalLlm',
    embedding: 'embedding',
    rerank: 'rerank',
  },
  callStage: {
    processing_preflight: 'processingPreflight',
    boundary: 'boundary',
    strategy: 'strategy',
    cascade: 'cascade',
    episode_extract: 'episodeExtract',
    parse: 'parse',
  },
} as const;

type EnumLabelGroup = keyof typeof ENUM_LABEL_KEYS;

// Backend enums are closed for known versions. A future value stays inert text
// instead of being mistaken for an i18n key or interpreted as markup.
export function memoryLogEnumLabel(t: TFunction, group: EnumLabelGroup, value: string): string {
  const labels = ENUM_LABEL_KEYS[group] as Record<string, string>;
  const key = labels[value];
  return key ? t(`memory.log.${group}.${key}`) : value;
}

export type JsonPreview =
  | { mode: 'tree'; value: object; text: string }
  | { mode: 'text'; text: string };

const byteLength = (value: string): number => new TextEncoder().encode(value).byteLength;

function countJsonNodes(root: unknown, limit: number): number {
  let count = 0;
  const stack: unknown[] = [root];
  while (stack.length > 0) {
    const value = stack.pop();
    count += 1;
    if (count > limit) return count;
    if (value !== null && typeof value === 'object') {
      const children = Array.isArray(value) ? value : Object.values(value as Record<string, unknown>);
      for (const child of children) stack.push(child);
    }
  }
  return count;
}

export function prepareJsonPreview(value: unknown): JsonPreview {
  let parsed = value;
  let text: string;
  if (typeof value === 'string') {
    text = value;
    try {
      parsed = JSON.parse(value);
    } catch {
      return { mode: 'text', text };
    }
  } else {
    try {
      text = JSON.stringify(value, null, 2) ?? String(value);
    } catch {
      return { mode: 'text', text: String(value) };
    }
  }
  if (
    parsed === null ||
    typeof parsed !== 'object' ||
    byteLength(text) > JSON_TREE_MAX_BYTES ||
    countJsonNodes(parsed, JSON_TREE_MAX_NODES) > JSON_TREE_MAX_NODES
  ) {
    return { mode: 'text', text };
  }
  return { mode: 'tree', value: parsed as object, text };
}

export function mergeMemoryLogEntries(
  current: MemoryLogEntry[],
  incoming: MemoryLogEntry[],
  replace: boolean,
): MemoryLogEntry[] {
  if (replace) return incoming.slice(0, MEMORY_LOG_ENTRY_LIMIT);
  const seen = new Set(current.map((entry) => entry.memcell_id));
  return [...current, ...incoming.filter((entry) => !seen.has(entry.memcell_id))]
    .slice(0, MEMORY_LOG_ENTRY_LIMIT);
}
