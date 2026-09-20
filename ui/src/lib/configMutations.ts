export type ConfigPath = readonly [string, ...string[]];

export type ConfigSetMutation = {
  kind: 'set';
  path: ConfigPath;
  value: unknown;
};

export type EnabledPlatformsMutation = {
  kind: 'enabled-platforms';
  add: readonly string[];
  remove: readonly string[];
};

export type ConfigMutation = ConfigSetMutation | EnabledPlatformsMutation;

const RESERVED_PATH_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor']);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const valuesEqual = (left: unknown, right: unknown): boolean => {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => valuesEqual(value, right[index]));
  }
  if (isRecord(left) && isRecord(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return (
      leftKeys.length === rightKeys.length &&
      leftKeys.every((key) => Object.hasOwn(right, key) && valuesEqual(left[key], right[key]))
    );
  }
  return false;
};

function validatePath(path: readonly string[]): asserts path is ConfigPath {
  if (path.length === 0) throw new Error('Config mutation path cannot be empty');
  for (const segment of path) {
    if (!segment || RESERVED_PATH_SEGMENTS.has(segment)) {
      throw new Error(`Invalid config mutation path segment: ${segment || '<empty>'}`);
    }
  }
}

const isEnabledPlatformsPath = (path: readonly string[]) =>
  path.length === 2 && path[0] === 'platforms' && path[1] === 'enabled';

export const setConfigField = (path: ConfigPath, value: unknown): ConfigSetMutation => ({
  kind: 'set',
  path,
  value,
});

export const updateEnabledPlatforms = ({
  add = [],
  remove = [],
}: {
  add?: readonly string[];
  remove?: readonly string[];
}): EnabledPlatformsMutation => ({
  kind: 'enabled-platforms',
  add,
  remove,
});

/**
 * Derive leaf-field mutations from one UI-owned before/after subtree.
 * Omitted keys mean unchanged; callers use null for an intentional clear.
 */
export const configChanges = (
  before: unknown,
  after: unknown,
  prefix: readonly string[] = [],
): ConfigMutation[] => {
  if (after === undefined || valuesEqual(before, after)) return [];

  if (isRecord(after)) {
    const previous = isRecord(before) ? before : {};
    return Object.entries(after).flatMap(([key, value]) =>
      configChanges(previous[key], value, [...prefix, key]),
    );
  }

  validatePath(prefix);
  if (isEnabledPlatformsPath(prefix)) {
    throw new Error('platforms.enabled must use updateEnabledPlatforms');
  }
  return [setConfigField(prefix, after)];
};

const assignPath = (
  payload: Record<string, unknown>,
  path: ConfigPath,
  value: unknown,
  seen: Set<string>,
) => {
  validatePath(path);
  if (isEnabledPlatformsPath(path)) {
    throw new Error('platforms.enabled must use updateEnabledPlatforms');
  }
  if (value === undefined) throw new Error(`Config mutation ${path.join('.')} has undefined value`);
  if (isRecord(value)) {
    throw new Error(`Config mutation ${path.join('.')} must target a leaf value`);
  }

  const identity = JSON.stringify(path);
  if (seen.has(identity)) throw new Error(`Duplicate config mutation path: ${path.join('.')}`);
  seen.add(identity);

  let node = payload;
  for (const segment of path.slice(0, -1)) {
    const current = node[segment];
    if (current === undefined) {
      node[segment] = {};
    } else if (!isRecord(current)) {
      throw new Error(`Conflicting config mutation path: ${path.join('.')}`);
    }
    node = node[segment] as Record<string, unknown>;
  }

  const leaf = path[path.length - 1];
  if (Object.hasOwn(node, leaf)) {
    throw new Error(`Conflicting config mutation path: ${path.join('.')}`);
  }
  node[leaf] = value;
};

/** Serialize explicit UI mutations to the existing merge-patch HTTP contract. */
export const configMutationsToPayload = (mutations: readonly ConfigMutation[]) => {
  const payload: Record<string, unknown> = {};
  const seen = new Set<string>();
  const additions = new Set<string>();
  const removals = new Set<string>();

  for (const mutation of mutations) {
    if (mutation.kind === 'set') {
      assignPath(payload, mutation.path, mutation.value, seen);
      continue;
    }

    for (const platform of mutation.remove) {
      if (!platform) continue;
      additions.delete(platform);
      removals.add(platform);
    }
    for (const platform of mutation.add) {
      if (!platform) continue;
      removals.delete(platform);
      additions.add(platform);
    }
  }

  if (additions.size > 0 || removals.size > 0) {
    payload.__avibe_list_ops = {
      'platforms.enabled': {
        ...(additions.size > 0 ? { add: [...additions] } : {}),
        ...(removals.size > 0 ? { remove: [...removals] } : {}),
      },
    };
  }

  if (Object.keys(payload).length === 0) {
    throw new Error('Config mutation set cannot be empty');
  }
  return payload;
};
