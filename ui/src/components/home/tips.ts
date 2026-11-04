// The tip of the day (ui/design: home/home/b-partial-*, c-ready-*): one capability a day, the ones
// not used yet first. Pure, so the order the home shows can be checked without a clock.

/** The approved set, in its order. */
export type TipId = 'bots' | 'showPages' | 'tasks' | 'vault' | 'skills' | 'chatApps' | 'desktopPet' | 'voice';

export const TIP_IDS: readonly TipId[] = ['bots', 'showPages', 'tasks', 'vault', 'skills', 'chatApps', 'desktopPet', 'voice'];

/** Days since the epoch on the reader's own calendar, so the tip changes at their midnight. */
export const dayNumber = (date: Date): number =>
  Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000);

/**
 * The order ‹ › walks, and where today starts in it. The tips whose capability is not known to be
 * used come first, in the set's order; the day picks among them, so each comes round in turn.
 * Once every one is used, the day picks among them all.
 */
export function tipsForDay(available: readonly TipId[], used: ReadonlySet<TipId>, day: number): { order: TipId[]; start: number } {
  const unused = available.filter((id) => !used.has(id));
  const order = [...unused, ...available.filter((id) => used.has(id))];
  const pool = unused.length > 0 ? unused.length : order.length;
  return { order, start: pool > 0 ? ((day % pool) + pool) % pool : 0 };
}
