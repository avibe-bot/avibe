// Pure toast-coalescing predicate + action type, kept out of ToastContext.tsx so
// that provider file only exports its component + hook (react-refresh HMR rule).

// Optional one-tap action (e.g. an Undo) rendered inside a toast.
export type ToastAction = { label: string; onClick: () => void };

// A toast carrying an action targets ONE specific item (e.g. undo THIS hide), so
// it must never fold into an earlier toast's repeat counter — that would strand
// the action on the wrong item. Only plain duplicates within the window coalesce.
export function shouldCoalesceToast(
  hasAction: boolean,
  existing: { expiresAt: number } | undefined,
  now: number,
): boolean {
  return !hasAction && existing !== undefined && existing.expiresAt > now;
}
