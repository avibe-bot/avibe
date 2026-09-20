import type { ReasoningEffort } from '@/lib/effortOptions';

import type { SourceProtocol } from './types';

/**
 * The effort vocabulary for a source's protocol family.
 *
 * One table, two readers. The tier editor offers these as ghost chips on a
 * model the user may still declare; the backend applies the SAME list when
 * discovery proves a model is reasoning-capable without naming levels (rung 1
 * of the provenance ladder — upstream capability signals are booleans, not
 * level enums). Suggesting one set while the ladder applies another would make
 * a user-typed list and an upstream-declared list disagree for no reason the
 * user could see.
 *
 * Members come from `REASONING_EFFORTS`, so nothing offered here is outside the
 * unified vocabulary. The lists themselves are per-family because the families
 * genuinely differ: the OpenAI protocols name a `minimal` tier and stop at
 * `xhigh`, while the Anthropic side runs `low`..`max` (matching the claude rows
 * in `vibe/data/backend_models.json` — see `tierSuggestions.test.ts`, which
 * reads that catalog rather than restating it).
 *
 * Suggestions are still not a vocabulary the user is held to: `reasoning_efforts`
 * is an arbitrary-string declaration passed to the upstream verbatim, the field
 * stays free text, and a relay may accept tiers no protocol ever named. Nothing
 * here is persisted, pre-filled at discovery, or sent to the API — a suggestion
 * becomes data only when the user clicks it, through the same add path typing it
 * would take.
 */
export const TIER_SUGGESTIONS: Readonly<Record<SourceProtocol, readonly ReasoningEffort[]>> = {
  anthropic: ['low', 'medium', 'high', 'xhigh', 'max'],
  openai_responses: ['minimal', 'low', 'medium', 'high', 'xhigh'],
  openai_chat: ['minimal', 'low', 'medium', 'high', 'xhigh'],
};
