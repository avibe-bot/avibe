// Bounded session slots for windowed terminals.
//
// Each windowed terminal needs its own backend session id (so two terminal windows
// — or a window and the /apps/terminal route — don't evict each other's attached
// client). Keying that off the ever-increasing window id would mint an unbounded
// number of session ids: the backend keeps detached tmux sessions in its capacity
// accounting (cap ~8) until an idle timeout, so opening/closing terminal windows
// would exhaust the service even with no terminal open.
//
// Instead we hand each live windowed terminal the lowest free slot index and return
// it on close, so the number of distinct windowed session ids never exceeds the
// number of terminals open at once. Module-level (per page load); the route terminal
// doesn't take a slot — it keeps its persistent, localStorage-backed id.
const inUse = new Set<number>();

export function acquireTerminalSlot(): number {
  let i = 0;
  while (inUse.has(i)) i += 1;
  inUse.add(i);
  return i;
}

export function releaseTerminalSlot(slot: number): void {
  inUse.delete(slot);
}

// Test-only: reset the shared pool between cases.
export function _resetTerminalSlots(): void {
  inUse.clear();
}
