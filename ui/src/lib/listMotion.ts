// How a list moves when its rows change (docs/plans/web-ui-motion.md M7, §5, §6):
// which rows leave, which glide, which come in, and in what form. Pure, so the
// choreography is testable without layout; `components/ui/flip-list.tsx` reads
// the boxes and plays the plan.

/** A row's box on screen (viewport px). */
export type Box = { top: number; left: number; width: number; height: number };

export type OrderDiff = {
  inserted: Set<string>;
  removed: Set<string>;
  /** Rows that changed place relative to the others (a conversation a reply moved
   *  to the top), as opposed to rows that only shifted to make room. */
  moved: Set<string>;
};

/**
 * What changed between two orders. A row is `moved` when it is not in the
 * longest run of rows that kept their relative order: when a reply takes the
 * third card to the top, the third card moved and the two above it only shifted.
 */
export function diffOrder(prev: readonly string[], next: readonly string[]): OrderDiff {
  const prevIndex = new Map(prev.map((id, index) => [id, index]));
  const nextSet = new Set(next);
  const inserted = new Set(next.filter((id) => !prevIndex.has(id)));
  const removed = new Set(prev.filter((id) => !nextSet.has(id)));
  const kept = next.filter((id) => prevIndex.has(id));
  const steady = longestIncreasing(kept.map((id) => prevIndex.get(id)!));
  const moved = new Set(kept.filter((_, index) => !steady.has(index)));
  return { inserted, removed, moved };
}

/** Indices (into `values`) of one longest strictly increasing subsequence. */
function longestIncreasing(values: readonly number[]): Set<number> {
  const tails: number[] = [];
  const previous = new Array<number>(values.length).fill(-1);
  for (let i = 0; i < values.length; i += 1) {
    let lo = 0;
    let hi = tails.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (values[tails[mid]] < values[i]) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0) previous[i] = tails[lo - 1];
    tails[lo] = i;
  }
  const out = new Set<number>();
  for (let i = tails.at(-1) ?? -1; i >= 0; i = previous[i]) out.add(i);
  return out;
}

/**
 * The form a change takes.
 * - `move`: rows leave, glide and come in (M7).
 * - `fade`: nothing travels; what changed fades out and in on `fast`. Reduced
 *   motion (§7), and a burst of live updates (§5: more than 3 in 1 s).
 * - `crossfade`: the same on the hand-over tokens, for a change too big to read
 *   as row moves (§6: more than 5 rows).
 */
export type ListMotionMode = 'move' | 'fade' | 'crossfade';

/** Rows changed (inserted, removed or moved) beyond which a change cross-fades (§6). */
export const CROSSFADE_OVER = 5;
/** Changes within `BURST_WINDOW_MS` beyond which list moves fall back to the fade (§5). */
export const BURST_OVER = 3;
export const BURST_WINDOW_MS = 1000;
/** At most this many elements animate in one change (§5). */
export const MAX_ANIMATED = 8;

export function chooseListMotion({ reduced, changes, recentChanges }: {
  reduced: boolean;
  /** Rows inserted, removed or moved by this change */
  changes: number;
  /** Changes in the burst window before this one. */
  recentChanges: number;
}): ListMotionMode {
  if (reduced || recentChanges + 1 > BURST_OVER) return 'fade';
  if (changes > CROSSFADE_OVER) return 'crossfade';
  return 'move';
}

export type ListMotionPlan = {
  /** Rows to draw where they were, leaving: `removed` rows, and `moved` rows' old slot. */
  leaves: { id: string; box: Box; kind: 'removed' | 'moved' }[];
  /** Rows that stay and slide from their old place to their new one. */
  glides: { id: string; dx: number; dy: number }[];
  /** Rows coming in at their slot: new rows, and moved rows at their new place. */
  enters: { id: string }[];
};

/** The visible band (viewport px) and how far past it rows still move: one screen (§5). */
export type Viewport = { top: number; bottom: number };

const overlaps = (a: Box, b: Box) =>
  a.top < b.top + b.height - 1 && b.top < a.top + a.height - 1 && a.left < b.left + b.width - 1 && b.left < a.left + a.width - 1;

const near = (box: Box | undefined, view: Viewport) => {
  if (!box) return Infinity;
  const screen = view.bottom - view.top;
  if (box.top + box.height < view.top - screen || box.top > view.bottom + screen) return Infinity;
  if (box.top + box.height <= view.top) return view.top - (box.top + box.height);
  if (box.top >= view.bottom) return box.top - view.bottom;
  return 0;
};

/**
 * The plan for one change: from the order and boxes before it to those after it.
 * Rows more than a screen off the visible band snap, at most `MAX_ANIMATED`
 * elements animate (nearest the band first), and in the fade forms nothing
 * glides: a row that only shifted takes its place at once, so a row that left
 * fades where it was only if nothing has taken that place (two texts over each
 * other would only flash).
 */
export function planListMotion({ prevIds, nextIds, before, after, viewport, mode, limit = MAX_ANIMATED }: {
  prevIds: readonly string[];
  nextIds: readonly string[];
  before: ReadonlyMap<string, Box>;
  after: ReadonlyMap<string, Box>;
  viewport: Viewport;
  mode: ListMotionMode;
  limit?: number;
}): ListMotionPlan {
  const diff = diffOrder(prevIds, nextIds);
  type Candidate = { distance: number; order: number; apply: (plan: ListMotionPlan) => void };
  const candidates: Candidate[] = [];
  let order = 0;
  const add = (distance: number, apply: Candidate['apply']) => {
    if (distance !== Infinity) candidates.push({ distance, order: order++, apply });
  };

  const taken = (box: Box) => mode !== 'move' && [...after.values()].some((other) => overlaps(box, other));
  for (const id of prevIds) {
    const box = before.get(id);
    if (!box || taken(box)) continue;
    if (diff.removed.has(id)) add(near(box, viewport), (plan) => plan.leaves.push({ id, box, kind: 'removed' }));
    else if (diff.moved.has(id)) add(near(box, viewport), (plan) => plan.leaves.push({ id, box, kind: 'moved' }));
  }
  for (const id of nextIds) {
    const box = after.get(id);
    if (!box) continue;
    if (diff.inserted.has(id) || diff.moved.has(id)) {
      add(near(box, viewport), (plan) => plan.enters.push({ id }));
      continue;
    }
    if (mode !== 'move') continue;
    const from = before.get(id);
    if (!from) continue;
    const dx = from.left - box.left;
    const dy = from.top - box.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
    add(Math.min(near(from, viewport), near(box, viewport)), (plan) => plan.glides.push({ id, dx, dy }));
  }

  const plan: ListMotionPlan = { leaves: [], glides: [], enters: [] };
  candidates
    .sort((a, b) => a.distance - b.distance || a.order - b.order)
    .slice(0, limit)
    .sort((a, b) => a.order - b.order)
    .forEach((candidate) => candidate.apply(plan));
  return plan;
}

/** Rows inserted, removed or moved. A page loaded at the end is many inserted rows, so
 *  it takes over on the hand-over fade like any change too big to read as moves. */
export function countChanges(prevIds: readonly string[], nextIds: readonly string[]): number {
  const diff = diffOrder(prevIds, nextIds);
  return diff.inserted.size + diff.removed.size + diff.moved.size;
}
