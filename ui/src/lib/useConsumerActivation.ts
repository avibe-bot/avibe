import { useCallback, useMemo, useRef, useState } from 'react';

export interface ConsumerActivation {
  /** True while at least one consumer has declared it actually reads this data.
   *  This is React state, so it lands one render AFTER the activation — which is
   *  what makes it usable as an effect dependency. */
  active: boolean;
  /** The same answer read straight from the counter, for code running outside
   *  render: effects, event listeners, SSE callbacks.
   *
   *  Consumers activate from their own effects, and React runs child effects
   *  before the provider's, so this is already accurate in the provider's mount
   *  effect while ``active`` is still false. Which one to reach for follows from
   *  what the code has to decide:
   *
   *  * skip a request → ``active`` is enough; the second render triggers it.
   *  * CHOOSE between two requests → ``isActive()``, because the choice is made
   *    in the first commit and ``active`` is still false there.
   *  * react to the demand EDGE (demand appearing, and especially demand going
   *    away) → BOTH: ``active`` in the dependency list, since a stable function
   *    identity never re-runs an effect, and ``isActive()`` in the body to keep
   *    that first commit correct. An effect that only closes over ``isActive``
   *    silently answers "was there demand when I mounted", which reads as
   *    correct for as long as nobody navigates. */
  isActive: () => boolean;
  /** Effect body: call from a consumer's ``useEffect`` and return the result as
   *  its cleanup. Balanced activate/release pairs in one commit batch to a net
   *  no-op, so a consumer whose deps churn doesn't re-trigger a bootstrap. */
  activate: () => () => void;
}

/** Refcounted "someone is actually reading this" signal for a provider mounted
 *  ABOVE the router.
 *
 *  Such a provider is mounted once per document and never remounts on
 *  navigation — which is what makes its cache worth having, and also what makes
 *  a mount-time bootstrap fire on every route, including the routes that never
 *  read it. Counting active consumers turns that mount-driven fetch into a
 *  demand-driven one without moving the provider into the router or splitting
 *  its cache per route.
 *
 *  Same contract as ``ShowPagesInventoryStore.activate()``, expressed for a
 *  provider that keeps its state in React rather than in an external store. */
export function useConsumerActivation(): ConsumerActivation {
  const [consumers, setConsumers] = useState(0);
  // The same count, committed synchronously by activate() so an effect or a
  // listener can read it before the state update renders. Both are updated
  // together and never diverge by more than that one render.
  const consumersRef = useRef(0);
  const activate = useCallback(() => {
    consumersRef.current += 1;
    setConsumers((count) => count + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      consumersRef.current = Math.max(0, consumersRef.current - 1);
      setConsumers((count) => Math.max(0, count - 1));
    };
  }, []);
  const isActive = useCallback(() => consumersRef.current > 0, []);
  return useMemo(
    () => ({ active: consumers > 0, isActive, activate }),
    [activate, consumers, isActive],
  );
}
