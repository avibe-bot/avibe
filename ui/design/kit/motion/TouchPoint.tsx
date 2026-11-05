// Where a finger is, in a motion demo that shows a gesture (a drag, a hold). It is a marker for the
// viewer, as a screen recording draws touches, not part of any surface. Its loop moves it with
// `[data-touch-point]`; it rests hidden until a beat shows it.
export function TouchPoint({ top, left }: { top: number; left: number }) {
  return (
    <span
      data-touch-point=""
      aria-hidden="true"
      className="pointer-events-none fixed z-[100] size-11 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-foreground/50 bg-foreground/20 opacity-0"
      style={{ top, left }}
    />
  );
}
