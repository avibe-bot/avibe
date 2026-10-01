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

/**
 * Whether agreeing to `shown` also agrees to a refusal that names `next`.
 *
 * Agreeing to a removal covers whichever hops go with it, so hops may move
 * freely. Stopping an Agent is a consequence of its own, so every gap `next`
 * names — and every Agent within it — must be one the user was shown. `null`
 * is an agreement given before the server spoke, which showed no gap at all.
 */
export const agreementCovers = (shown: GuardPlan | null, next: GuardPlan): boolean => next.gaps.every((gap) => {
  const seen = shown?.gaps.find((item) => item.backend === gap.backend && item.model_id === gap.model_id);
  return seen !== undefined && gap.agents.every((agent) => seen.agents.includes(agent));
});

/**
 * The plan a failed guarded write still has to ask the user about, or null.
 *
 * Without an agreement every guard refusal is a question. With one, only a
 * refusal the agreement does not cover is: a covered plan that kept moving past
 * the resend bound has been answered already, and ends as the failure it is.
 */
export const unansweredRefusal = (error: unknown, agreed: boolean, shown: GuardPlan | null): GuardPlan | null => {
  const refusal = guardedFailure(error);
  return refusal && (!agreed || !agreementCovers(shown, refusal)) ? refusal : null;
};

const FORCED_RESENDS = 2;

/**
 * Send a guarded write, echoing `plan` as its confirmation when there is one.
 *
 * Once the user has `agreed`, a guard that refuses the write names the plan as
 * it stands now, and that answer already covers it as long as it stops no Agent
 * the user was not shown: the write goes again with the new plan rather than
 * asking the same question twice. A refusal the agreement does not cover is
 * thrown for its caller to ask, and `unansweredRefusal` tells the two apart.
 * Bounded, so a plan that never settles still ends as the refusal it is.
 * `write` receives the plan each attempt echoes, so a caller can tell which one
 * its outcome belongs to.
 */
export const sendAgreed = async <T,>(
  agreed: boolean,
  plan: GuardPlan | null,
  write: (plan: GuardPlan | null) => Promise<T>,
): Promise<T> => {
  const shown = plan;
  for (let resends = 0; ; resends += 1) {
    try {
      return await write(plan);
    } catch (error) {
      const moved = agreed && resends < FORCED_RESENDS ? guardedFailure(error) : null;
      if (!moved || !agreementCovers(shown, moved)) throw error;
      plan = moved;
    }
  }
};
