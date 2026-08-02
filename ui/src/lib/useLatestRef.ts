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
 * `useLatestRef.test.tsx` pins it.
 *
 * One thing the linter cannot do for you: it does not see through this hook, so
 * reading the returned `.current` *during render* at a call site is not flagged
 * the way reading a plain `useRef` is. That read is still wrong — the component
 * will not re-render when the value changes. Read it from effects, listeners,
 * timers and async callbacks only. `scripts/eslintConventions.test.mjs` pins
 * that the rule itself is still on for the ordinary case, so the exemption above
 * stays one line rather than becoming a config-wide relaxation.
 */
export function useLatestRef<T>(value: T): RefObject<T> {
  const ref = useRef(value);
  // eslint-disable-next-line react-hooks/refs -- The point of the helper; see above.
  ref.current = value;
  return ref;
}
