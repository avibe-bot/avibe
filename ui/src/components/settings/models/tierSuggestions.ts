import type { SourceProtocol } from './types';

/**
 * What the tier editor offers as ghost chips, derived from the protocol this
 * source was proved to speak. The OpenAI protocols name a fixed effort
 * vocabulary, so those four are worth offering; the Anthropic Messages protocol
 * expresses reasoning as a thinking budget rather than a named tier, so there is
 * nothing protocol-derived to suggest and that source stays pure free text.
 *
 * These are suggestions, never a vocabulary: `reasoning_efforts` is an
 * arbitrary-string capability declaration passed to the upstream verbatim, and a
 * relay may accept tiers no protocol ever named. Nothing here is persisted,
 * pre-filled at discovery, or sent to the API — a suggestion becomes data only
 * when the user clicks it, through the same add path typing it would take.
 * `lib/effortOptions.ts` is the sibling table for the OTHER axis — which efforts
 * an agent BACKEND offers — and is deliberately not reused: it answers a
 * different question and carries values (`minimal`, `max`) this one does not.
 */
export const TIER_SUGGESTIONS: Readonly<Record<SourceProtocol, readonly string[]>> = {
  anthropic: [],
  openai_responses: ['low', 'medium', 'high', 'xhigh'],
  openai_chat: ['low', 'medium', 'high', 'xhigh'],
};
