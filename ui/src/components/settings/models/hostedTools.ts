// What the Tools tab and the Source chips say about hosted-tool evidence
// (`vibey-hosted-tools.md` §6.1–§6.2). Pure projections of the Tools-tab read
// (`hosted-tools-status.schema.json`): the server has already folded staleness
// into `status`, so nothing here re-derives routing.
import type {
  HostedEvidence,
  HostedToolCandidate,
  HostedToolExecutor,
  HostedToolName,
  HostedToolsStatus,
  HostedToolStatus,
} from './types';

/**
 * One call-evidence record as a chip.
 *
 * - `native`: verified on the current engine; the tool runs natively.
 * - `reverify`: verified, but older than 30 days. It still runs natively.
 * - `engineUpgraded`: taken on another engine or probe shape. It routes as
 *   unknown, i.e. through the proxy, until the user checks again.
 * - `unsupported`: the Source refused the tool, by probe or by a real error.
 *
 * `null` is no record: unchecked, which is the normal state and draws no chip
 * on a Source (§6.2).
 */
export type HostedEvidenceChip =
  | { kind: 'native'; at: string | null }
  | { kind: 'reverify'; at: string | null }
  | { kind: 'engineUpgraded'; at: string | null }
  | { kind: 'unsupported'; at: string | null };

export const evidenceChip = (evidence: HostedEvidence): HostedEvidenceChip | null => {
  // Staleness first: a wire-stale record reads `unknown` but still has a past
  // the user needs to see, whatever it once said.
  if (evidence.stale === 'wire') return { kind: 'engineUpgraded', at: evidence.observed_at };
  if (evidence.status === 'verified') {
    return { kind: evidence.stale === 'age' ? 'reverify' : 'native', at: evidence.observed_at };
  }
  if (evidence.status === 'unsupported') return { kind: 'unsupported', at: evidence.observed_at };
  return null;
};

export const sameExecutor = (left: HostedToolExecutor | null, right: HostedToolExecutor | null): boolean =>
  left !== null && right !== null && left.source_id === right.source_id && left.model_id === right.model_id;

/** The candidates a user may pick as executor: those verified for the call now. */
export const verifiedCandidates = (tool: HostedToolStatus): HostedToolCandidate[] =>
  tool.candidates.filter((candidate) => candidate.call.status === 'verified');

/**
 * The candidate shown as the proxy executor. Only the user's own choice, and
 * only while it is verified: resolution then binds exactly it. Without a
 * choice the turn takes the first verified pair in Vibey's Source order, which
 * this read does not state, so no row claims it.
 */
export const chosenExecutor = (tool: HostedToolStatus): HostedToolCandidate | null =>
  verifiedCandidates(tool).find((candidate) => sameExecutor(candidate, tool.executor)) ?? null;

export type ModelCapability = { tool: HostedToolName; enabled: boolean; evidence: HostedEvidence; chip: HostedEvidenceChip };

/** A Source model's evidence for each tool, in catalog order, for its detail chips. */
export const modelCapabilities = (status: HostedToolsStatus, sourceId: string, modelId: string): ModelCapability[] =>
  status.tools.flatMap((tool) => {
    const candidate = tool.candidates.find((entry) => entry.source_id === sourceId && entry.model_id === modelId);
    const chip = candidate ? evidenceChip(candidate.call) : null;
    return candidate && chip ? [{ tool: tool.name, enabled: tool.enabled, evidence: candidate.call, chip }] : [];
  });
