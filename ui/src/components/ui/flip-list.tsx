import * as React from 'react';

import { cn } from '@/lib/utils';
import {
  BURST_WINDOW_MS,
  chooseListMotion,
  countChanges,
  diffOrder,
  planListMotion,
  type Box,
  type ListMotionMode,
} from '@/lib/listMotion';
import {
  canAnimate,
  motionTiming,
  playFlip,
  readMotionMs,
  readMotionNumber,
  readMotionToken,
  springTiming,
  MotionPreferenceContext,
  prefersReducedMotion,
  type MotionTiming,
} from '@/lib/motion';

// A list whose rows move instead of jumping (docs/plans/web-ui-motion.md M7; the
// plan is `lib/listMotion.ts`). Each row carries `data-flip-id`, and `ids` lists
// them in the order rendered. Right before React changes the DOM this reads
// where each row is on screen (a running move included, so a second change
// retargets from where the row is). In the next animation frame it reads where
// layout put them, which is the layout that frame does anyway, rather than
// forcing one inside the update, and plays the difference with the Web
// Animations API before that frame paints: transform and opacity only, nothing
// re-rendered, rows more than a screen away left alone. Changes committed before
// that frame are one change (nothing was painted between them). A row that left
// is drawn where it was by a copy taken before the commit, in a layer of its own
// that is not part of the list.

const FLIP_ATTRIBUTE = 'data-flip-id';

type Running = Animation[];

type Snapshot = {
  prevIds: readonly string[];
  before: Map<string, Box>;
  /** Copies of the rows that will leave their slot, taken while they still show what they showed. */
  copies: Map<string, HTMLElement>;
  /** Rows caught partway through coming in: where their opacity had got to. */
  opacity: Map<string, number>;
};

type Props = {
  ids: readonly string[];
  /** A change of group is a different list (another tab, another expansion): no motion. */
  group?: string;
  disabled?: boolean;
  /** The forced motion preference (`MotionPreferenceContext`); null follows the system. */
  forced: boolean | null;
  className?: string;
  id?: string;
  children: React.ReactNode;
};

const boxOf = (element: Element): Box => {
  const rect = element.getBoundingClientRect();
  return { top: rect.top, left: rect.left, width: rect.width, height: rect.height };
};

const sameOrder = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((id, index) => id === b[index]);

/** A copy that is only a picture of the row: no ids, no handles, out of the tab order and the accessibility tree. */
function stillCopy(element: HTMLElement): HTMLElement {
  const copy = element.cloneNode(true) as HTMLElement;
  for (const name of copy.getAttributeNames()) if (name.startsWith('data-') || name === 'id') copy.removeAttribute(name);
  copy.querySelectorAll('[id]').forEach((node) => node.removeAttribute('id'));
  copy.setAttribute('data-flip-ghost', '');
  copy.setAttribute('aria-hidden', 'true');
  copy.setAttribute('inert', '');
  return copy;
}

/** The tokens a list change reads. Read once per document: they never change at run time, and
 *  reading `:root`'s computed style during an update would force a style recalculation of the page. */
let cachedTokens: ReturnType<typeof readTokens> | null = null;
const listTokens = () => (cachedTokens ??= readTokens());
function readTokens() {
  const beat = readMotionMs('--motion-beat');
  return {
    instant: motionTiming('instant', 'enter'),
    fastExit: motionTiming('fast', 'exit'),
    fastEnter: motionTiming('fast', 'enter'),
    base: motionTiming('base', 'enter'),
    smooth: springTiming('smooth'),
    handover: { duration: readMotionMs('--motion-handover-duration'), easing: readMotionToken('--motion-handover-ease') || 'ease' },
    beat,
    offset: readMotionNumber('--motion-offset', 8),
    enterScale: readMotionNumber('--motion-scale-enter-sm', 0.98),
  };
}

class FlipListCore extends React.Component<Props> {
  private root = React.createRef<HTMLDivElement>();
  private layer = React.createRef<HTMLDivElement>();
  private running = new WeakMap<Element, Running>();
  private changes: number[] = [];
  /** The change waiting for its frame, from the state last painted. */
  private pending: Snapshot | null = null;
  private frame = 0;
  /** Every row animation running, so a change of list can end them. */
  private playing = new Set<Animation>();

  private rows(): Map<string, HTMLElement> {
    const out = new Map<string, HTMLElement>();
    const root = this.root.current;
    if (!root) return out;
    root.querySelectorAll<HTMLElement>(`[${FLIP_ATTRIBUTE}]`).forEach((row) => {
      out.set(row.getAttribute(FLIP_ATTRIBUTE)!, row);
    });
    return out;
  }

  private viewport() {
    return { top: 0, bottom: window.innerHeight || document.documentElement.clientHeight };
  }

  getSnapshotBeforeUpdate(prevProps: Props): Snapshot | null {
    const { ids, group, disabled } = this.props;
    if (disabled || prevProps.disabled || group !== prevProps.group) {
      // Another list: what the old one was playing ends here, copies and all.
      this.drop();
      this.halt();
      return null;
    }
    // Already waiting for a frame: nothing has been painted since, so the screen still shows
    // what that snapshot read. Only copy what this commit takes away that it did not.
    if (this.pending) {
      this.copyLeaving(this.pending, ids, this.rows());
      return null;
    }
    if (prevProps.ids.length === 0 || !canAnimate(this.root.current)) return null;
    const rows = this.rows();
    const before = new Map<string, Box>();
    const opacity = new Map<string, number>();
    for (const [id, row] of rows) {
      before.set(id, boxOf(row));
      if (this.running.has(row)) {
        const value = parseFloat(getComputedStyle(row).opacity);
        if (value < 1) opacity.set(id, value);
      }
    }
    const snapshot = { prevIds: prevProps.ids, before, copies: new Map<string, HTMLElement>(), opacity };
    this.copyLeaving(snapshot, ids, rows);
    return snapshot;
  }

  /** Copies of the rows that leave a slot on or near the screen, taken while they still show what they showed. */
  private copyLeaving(snapshot: Snapshot, ids: readonly string[], rows: Map<string, HTMLElement>) {
    if (sameOrder(snapshot.prevIds, ids)) return;
    const diff = diffOrder(snapshot.prevIds, ids);
    const view = this.viewport();
    const screen = view.bottom - view.top;
    for (const id of snapshot.prevIds) {
      if ((!diff.removed.has(id) && !diff.moved.has(id)) || snapshot.copies.has(id)) continue;
      const row = rows.get(id);
      const box = snapshot.before.get(id);
      if (!row || !box || box.top + box.height < view.top - screen || box.top > view.bottom + screen) continue;
      snapshot.copies.set(id, stillCopy(row));
    }
  }

  componentDidUpdate(_prevProps: Props, _prevState: unknown, snapshot: Snapshot | null) {
    if (!snapshot) return;
    this.pending = snapshot;
    this.frame = requestAnimationFrame(() => this.flush());
  }

  private drop() {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.pending = null;
  }

  private flush() {
    const snapshot = this.pending;
    this.drop();
    if (!snapshot || !this.root.current) return;
    const rows = this.rows();
    // A row caught mid-move is measured where layout puts it, then moved on from where it was seen.
    const stopped = new Set<string>();
    for (const [id, row] of rows) if (this.stop(row)) stopped.add(id);
    const after = new Map<string, Box>();
    for (const [id, row] of rows) after.set(id, boxOf(row));

    const { ids, forced } = this.props;
    const reduced = forced ?? prefersReducedMotion();
    const now = performance.now();
    this.changes = this.changes.filter((at) => now - at < BURST_WINDOW_MS);
    const changes = countChanges(snapshot.prevIds, ids);
    const mode = chooseListMotion({ reduced, changes, recentChanges: this.changes.length });
    const plan = planListMotion({ prevIds: snapshot.prevIds, nextIds: ids, before: snapshot.before, after, viewport: this.viewport(), mode });
    // A row this change does not move finishes its fade from where it was seen. (A row that
    // still has somewhere to go is a glide of this change; in the fade forms nothing travels.)
    for (const id of [...plan.glides, ...plan.enters].map((step) => step.id)) stopped.delete(id);
    this.resume(stopped, rows, snapshot);
    if (plan.leaves.length + plan.glides.length + plan.enters.length === 0) return;
    if (changes > 0) this.changes.push(now);
    this.play(plan, mode, rows, snapshot);
  }

  /** Rows stopped for measuring that this change does not move: a fade under way carries on. */
  private resume(ids: Set<string>, rows: Map<string, HTMLElement>, snapshot: Snapshot) {
    for (const id of ids) {
      const row = rows.get(id);
      const opacity = snapshot.opacity.get(id);
      if (row && opacity !== undefined) this.track(row, row.animate([{ opacity }, { opacity: 1 }], listTokens().fastEnter));
    }
  }

  componentWillUnmount() {
    this.drop();
    this.halt();
  }

  /** Ends every row animation and removes every copy. */
  private halt() {
    for (const animation of [...this.playing]) animation.cancel();
    this.playing.clear();
    this.layer.current?.replaceChildren();
  }

  /** Cancels what the row is playing; whether it was playing anything. */
  private stop(row: Element) {
    const running = this.running.get(row);
    if (!running) return false;
    this.running.delete(row);
    for (const animation of running) animation.cancel();
    return true;
  }

  private track(row: Element, animation: Animation | null) {
    if (!animation) return;
    this.playing.add(animation);
    const done = () => this.playing.delete(animation);
    animation.finished.then(done, done);
    const list = this.running.get(row) ?? [];
    list.push(animation);
    this.running.set(row, list);
    const forget = () => {
      const current = this.running.get(row);
      if (!current) return;
      const left = current.filter((item) => item !== animation);
      if (left.length) this.running.set(row, left);
      else this.running.delete(row);
    };
    animation.finished.then(forget, () => {});
  }

  private play(
    plan: ReturnType<typeof planListMotion>,
    mode: ListMotionMode,
    rows: Map<string, HTMLElement>,
    snapshot: Snapshot,
  ) {
    const tokens = listTokens();
    const moving = mode === 'move';
    const fade: MotionTiming = mode === 'crossfade' ? tokens.handover : tokens.fastEnter;
    const fadeOut: MotionTiming = mode === 'crossfade' ? tokens.handover : tokens.fastExit;

    // Leaving: a copy where the row was, fading (and, when moving, shrinking a little).
    let closeUp = 0;
    const root = this.root.current;
    const layer = this.layer.current;
    if (root && layer) {
      const origin = root.getBoundingClientRect();
      for (const leave of plan.leaves) {
        const copy = snapshot.copies.get(leave.id);
        // It leaves as it was seen: a row still coming in (its copy carries none of its motion)
        // fades from where its fade had got to, and one not yet showing leaves nothing behind.
        const seen = snapshot.opacity.get(leave.id) ?? 1;
        if (!copy || seen < 0.01) continue;
        const timing = !moving ? fadeOut : leave.kind === 'removed' ? tokens.fastExit : tokens.instant;
        if (moving) closeUp = Math.max(closeUp, timing.duration);
        Object.assign(copy.style, {
          position: 'absolute',
          margin: '0',
          left: `${leave.box.left - origin.left - root.clientLeft + root.scrollLeft}px`,
          top: `${leave.box.top - origin.top - root.clientTop + root.scrollTop}px`,
          width: `${leave.box.width}px`,
          height: `${leave.box.height}px`,
        });
        layer.appendChild(copy);
        const to = moving ? { opacity: 0, transform: `scale(${tokens.enterScale})` } : { opacity: 0 };
        const from = moving ? { opacity: seen, transform: 'scale(1)' } : { opacity: seen };
        const animation = copy.animate([from, to], { ...timing, fill: 'forwards' });
        const drop = () => copy.remove();
        animation.finished.then(drop, drop);
      }
    }

    // Staying: from where each row was to where it is, once the leaving rows have gone. A row
    // inside another that glides (a session in a phone project card) moves with it already, so it
    // glides only by the difference, on the same timing.
    const gliding = new Map(plan.glides.map((glide) => [glide.id, glide]));
    for (const glide of plan.glides) {
      const row = rows.get(glide.id);
      if (!row) continue;
      const holder = row.parentElement?.closest<HTMLElement>(`[${FLIP_ATTRIBUTE}]`);
      const carried = holder && root?.contains(holder) ? gliding.get(holder.getAttribute(FLIP_ATTRIBUTE)!) : undefined;
      const dx = glide.dx - (carried?.dx ?? 0);
      const dy = glide.dy - (carried?.dy ?? 0);
      if (Math.abs(dx) >= 0.5 || Math.abs(dy) >= 0.5) {
        this.track(row, playFlip(row, { x: dx, y: dy }, { ...tokens.smooth, delay: closeUp }));
      }
      const seen = snapshot.opacity.get(glide.id);
      if (seen !== undefined) this.track(row, row.animate([{ opacity: seen }, { opacity: 1 }], tokens.fastEnter));
    }

    // Coming in: a beat after the rows below have made most of the room.
    const enterAfter = closeUp + tokens.fastEnter.duration + tokens.beat / 2;
    for (const enter of plan.enters) {
      const row = rows.get(enter.id);
      if (!row) continue;
      const from = snapshot.opacity.get(enter.id) ?? 0;
      if (moving) {
        // It grows from its top edge. The origin is set for the move, not animated: a keyframed
        // transform-origin would take the whole effect off the compositor.
        const lift = `translateY(${-tokens.offset}px) scale(${tokens.enterScale})`;
        row.style.transformOrigin = 'top center';
        const grow = row.animate([{ transform: lift }, { transform: 'none' }], { ...tokens.smooth, delay: enterAfter, fill: 'backwards' });
        const settle = () => {
          if (!this.running.get(row)?.some((animation) => animation !== grow && animation.playState === 'running')) row.style.transformOrigin = '';
        };
        grow.finished.then(settle, settle);
        this.track(row, grow);
        this.track(row, row.animate([{ opacity: from }, { opacity: 1 }], { ...tokens.base, delay: enterAfter, fill: 'backwards' }));
      } else {
        this.track(row, row.animate([{ opacity: from }, { opacity: 1 }], { ...fade, fill: 'backwards' }));
      }
    }
  }

  render() {
    const { className, id, children } = this.props;
    return (
      <div ref={this.root} id={id} className={cn('relative', className)}>
        {children}
        <div ref={this.layer} aria-hidden data-flip-ghosts="" className="pointer-events-none absolute left-0 top-0 z-10 h-0 w-full" />
      </div>
    );
  }
}

/**
 * A list whose rows glide, leave and come in (M7) as its `ids` change. Mark each
 * row with `data-flip-id`; the root takes `className` (it is the list's own box,
 * with one extra layer that takes no room). Under reduced motion, and during a
 * burst, changes fade instead of moving.
 */
export function FlipList(props: Omit<Props, 'forced'>) {
  const forced = React.useContext(MotionPreferenceContext);
  return <FlipListCore {...props} forced={forced} />;
}
