import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { parseShotShard, selectShotTasks } from './design-shot-selection.mjs';

// Unequal declared themes catch partitioning frames and then accidentally
// duplicating/dropping their themes. Prefixes deliberately overlap.
const frames = [
  { id: 'shell/a', themes: ['light', 'dark'] },
  { id: 'shell/b', themes: ['dark'] },
  { id: 'other/c', themes: ['light', 'dark'] },
  { id: 'other/d', themes: ['light'] },
  { id: 'other/e', themes: ['light', 'dark'] },
];
const names = (tasks) => tasks.map(([frame, theme]) => `${frame.id}:${theme}`);
const select = (prefixes = [], theme = null, shard = null) => names(selectShotTasks(frames, prefixes, theme, parseShotShard(shard)));
const allPairs = [
  'shell/a:light', 'shell/a:dark', 'shell/b:dark',
  'other/c:light', 'other/c:dark', 'other/d:light', 'other/e:light', 'other/e:dark',
];

describe('design capture selection and CI partition', () => {
  it('keeps the unsharded default and assigns every declared pair exactly once across uneven shards', () => {
    expect(select()).toEqual(allPairs);
    const shards = ['1/3', '2/3', '3/3'].map((shard) => select([], null, shard));
    expect(shards.map((tasks) => tasks.length)).toEqual([3, 3, 2]);
    expect(shards.flat().sort()).toEqual([...allPairs].sort());
    expect(new Set(shards.flat()).size).toBe(8);
    expect(select([], null, '1/1')).toEqual(allPairs);
  });

  it('partitions after the union of prefixes and preserves explicit theme overrides', () => {
    expect(select(['shell/', 'shell/a'])).toEqual(['shell/a:light', 'shell/a:dark', 'shell/b:dark']);
    expect(select(['shell/', 'shell/a'], null, '1/2')).toEqual(['shell/a:light', 'shell/b:dark']);
    expect(select(['shell/', 'shell/a'], null, '2/2')).toEqual(['shell/a:dark']);
    // Explicit --theme was always an override, not a declared-theme filter.
    expect(select(['shell/'], 'light')).toEqual(['shell/a:light', 'shell/b:light']);
    expect(select(['shell/'], 'light', '1/2')).toEqual(['shell/a:light']);
    expect(select(['shell/'], 'light', '2/2')).toEqual(['shell/b:light']);
  });

  it.each(['', undefined, '0/3', '1/0', '-1/3', '4/3', '1.5/3', '1/3.5', '1', '1/2/3', 'NaN/3', '1/Infinity', '1/9007199254740992'])(
    'rejects invalid shard %s instead of returning zero work', (shard) => {
      expect(() => parseShotShard(shard)).toThrow('--shard must be INDEX/TOTAL');
    },
  );

  it('fails every oversized shard and empty selections', () => {
    for (const shard of ['1/3', '2/3', '3/3']) {
      expect(() => select(['shell/a'], null, shard)).toThrow('shard count 3 exceeds selected shot count 2');
    }
    expect(() => select(['missing/'], null, '1/3')).toThrow('no frame matches missing/');
    expect(() => selectShotTasks([{ id: 'empty', themes: [] }], [], null, null)).toThrow('no frame/theme pairs selected');
  });
});

// Exercise the real CLI parser before it starts Vite/Chromium. The selector's
// unit cases cannot catch a missing --shard value being treated as a prefix,
// or a design:export caller forwarding --manifest into concurrent writers.
it.each([
  { args: ['--shard'], message: '--shard must be INDEX/TOTAL' },
  { args: ['--shard', '0/3'], message: '--shard must be INDEX/TOTAL' },
  { args: ['--manifest', 'unused.json', '--shard', '1/3'], message: '--shard cannot be combined with --manifest' },
])('fails invalid CLI arguments before browser startup: $args', ({ args, message }) => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./design-shot.mjs', import.meta.url)), ...args], {
    encoding: 'utf8', timeout: 10000,
  });
  expect(result.error).toBeUndefined();
  expect(result.status).toBe(1);
  expect(result.stderr).toContain(message);
});
