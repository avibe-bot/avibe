import { useRef, type RefObject } from 'react';

/**
 * A ref that always holds the latest `value`, for readers that run *outside*
 * render: effects, subscriptions, timers, event listeners, async callbacks.
 *
 * Twelve call sites had spelled this out by hand as
 *
 * ```ts
 * const handlerRef = useRef(onPaste);
 * handlerRef.current = onPaste;
 * ```
 *
 * so that a long-lived listener can call the newest callback without the
 * effect re-subscribing on every prop change. The write happens during render,
 * which `react-hooks/refs` flags — correctly in general, because a component
 * that *renders* from a ref will not re-render when it changes.
 *
 * This helper keeps that write during render on purpose, and is the only place
 * in the UI where the rule is exempted:
 *
 * - The write is idempotent and invisible to the render output, so a double
 *   invocation under StrictMode or a discarded concurrent render is harmless.
 * - Deferring it to an effect would be a behaviour change, not a cleanup: the
 *   ref would still hold the previous value while children's effects run, and
 *   during the render itself.
 *
 * The contract is therefore "current *now*, in this render", and
 * `useLatestRef.test.tsx` pins it. Reading the result during render defeats the
 * purpose and stays an error at the call site — `scripts/eslintConventions.test.mjs`
 * pins that too.
 */
export function useLatestRef<T>(value: T): RefObject<T> {
  const ref = useRef(value);
  // eslint-disable-next-line react-hooks/refs -- The point of the helper; see above.
  ref.current = value;
  return ref;
}
