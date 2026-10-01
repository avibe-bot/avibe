import { describe, expect, it, vi } from 'vitest';

import type { GuardPlan } from './GuardImpact';
import { sendAgreed, unansweredRefusal } from './guardedWrite';
import { ApiCallError } from './modelsApi';

const hop = (position: number) => ({
  backend: 'claude' as const, menu_model: 'beta', source_id: 'src_a', model_id: 'beta-air', position,
});
const gap = (model_id: string, agents: string[]) => ({ backend: 'claude' as const, model_id, agents });
const refusal = (plan: GuardPlan) => new ApiCallError(
  'backend_model_in_route', 'modelHub.errors.backend_model_in_route', true, plan.gaps, [], plan.hops, 409,
);

describe('sendAgreed', () => {
  const shown: GuardPlan = { hops: [hop(1)], gaps: [gap('beta', ['pm', '写作助手'])] };

  it.each([
    ['moved hops under the same Agents', { hops: [hop(2)], gaps: shown.gaps }, true],
    ['fewer Agents than were shown', { hops: [hop(1)], gaps: [gap('beta', ['pm'])] }, true],
    ['a gap for a model that was not shown', { hops: [hop(1)], gaps: [...shown.gaps, gap('gamma', [])] }, false],
    ['an Agent that was not shown', { hops: [hop(1)], gaps: [gap('beta', ['pm', '写作助手', 'ops'])] }, false],
  ])('MH-UNLISTED-002: forces an agreed plan only while it stops no Agent the user was not shown: %s', async (_, moved, forced) => {
    const write = vi.fn()
      .mockRejectedValueOnce(refusal(moved))
      .mockResolvedValue('saved');

    const outcome = sendAgreed(true, shown, write);

    if (forced) {
      await expect(outcome).resolves.toBe('saved');
      expect(write.mock.calls.map(([plan]) => plan)).toEqual([shown, moved]);
    } else {
      const error = await outcome.catch((caught: unknown) => caught);
      expect(write).toHaveBeenCalledTimes(1);
      expect(unansweredRefusal(error, true, shown)).toEqual(moved);
    }
  });

  it('treats an agreement given before the server spoke as showing no Agent', async () => {
    const hopsOnly: GuardPlan = { hops: [hop(1)], gaps: [] };
    const withAgent: GuardPlan = { hops: [hop(1)], gaps: [gap('beta', ['pm'])] };
    const write = vi.fn()
      .mockRejectedValueOnce(refusal(hopsOnly))
      .mockRejectedValueOnce(refusal(withAgent));

    const error = await sendAgreed(true, null, write).catch((caught: unknown) => caught);

    expect(write.mock.calls.map(([plan]) => plan)).toEqual([null, hopsOnly]);
    expect(unansweredRefusal(error, true, null)).toEqual(withAgent);
  });

  it('ends a covered plan that keeps moving as a failure, not a question', async () => {
    const write = vi.fn(async (plan: GuardPlan | null) => {
      throw refusal({ hops: [hop((plan?.hops[0]?.position ?? 0) + 1)], gaps: shown.gaps });
    });

    const error = await sendAgreed(true, shown, write).catch((caught: unknown) => caught);

    expect(write).toHaveBeenCalledTimes(3);
    expect(unansweredRefusal(error, true, shown)).toBeNull();
    expect(unansweredRefusal(error, false, null)).not.toBeNull();
  });
});
