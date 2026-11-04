// Selection belongs before scheduling: every declared frame/theme pair has one
// owner, regardless of the number of concurrent pages on that shard's runner.
export function parseShotShard(value) {
  if (value === null) return null;
  const match = /^([1-9]\d*)\/([1-9]\d*)$/.exec(value ?? '');
  const index = Number(match?.[1]);
  const total = Number(match?.[2]);
  if (!Number.isSafeInteger(index) || !Number.isSafeInteger(total) || index > total) {
    throw new Error('--shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL (positive safe integers)');
  }
  return { index, total };
}

export function selectShotTasks(all, prefixes, theme, shard) {
  const selected = all.filter((f) => prefixes.length === 0 || prefixes.some((p) => f.id.startsWith(p)));
  if (selected.length === 0) throw new Error(`no frame matches ${prefixes.join(', ')}`);
  const tasks = selected.flatMap((frame) => (theme ? [theme] : frame.themes).map((t) => [frame, t]));
  if (tasks.length === 0) throw new Error('no frame/theme pairs selected');
  if (!shard) return tasks;
  // Refuse the entire oversized partition, not just its empty legs: a typo must
  // never produce green zero-work jobs or a misleading partly-green matrix.
  if (shard.total > tasks.length) {
    throw new Error(`shard count ${shard.total} exceeds selected shot count ${tasks.length}`);
  }
  return tasks.filter((_, index) => index % shard.total === shard.index - 1);
}
