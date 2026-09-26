import type { GuardPlan } from './GuardImpact';
import { apiFailure, type GuardConfirmation } from './modelsApi';

/** The confirmation a forced write echoes: the server's own plan, verbatim. */
export const confirmGuardPlan = (plan: GuardPlan): GuardConfirmation => ({
  force: true,
  would_remove_hops: plan.hops,
  would_interrupt: plan.gaps,
});

/** The plan a guard refusal names, or null when the failure is anything else. */
export const guardedFailure = (error: unknown): GuardPlan | null => {
  const failure = apiFailure(error);
  if (!failure || (failure.wouldRemoveHops.length === 0 && failure.wouldInterrupt.length === 0)) return null;
  return { hops: failure.wouldRemoveHops, gaps: failure.wouldInterrupt };
};

const FORCED_RESENDS = 2;

/**
 * Send a guarded write, echoing `plan` as its confirmation when there is one.
 *
 * Once the user has `agreed`, a guard that refuses the write names the plan as
 * it stands now, and that answer already covers it: the write goes again with
 * the new plan rather than asking the same question twice. Bounded, so a plan
 * that never settles still ends as the refusal it is. `write` receives the plan
 * each attempt echoes, so a caller can tell which one its outcome belongs to.
 */
export const sendAgreed = async <T,>(
  agreed: boolean,
  plan: GuardPlan | null,
  write: (plan: GuardPlan | null) => Promise<T>,
): Promise<T> => {
  for (let resends = 0; ; resends += 1) {
    try {
      return await write(plan);
    } catch (error) {
      const moved = agreed && resends < FORCED_RESENDS ? guardedFailure(error) : null;
      if (!moved) throw error;
      plan = moved;
    }
  }
};
