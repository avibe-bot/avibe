// What the Tools tab and the Source chips say about hosted-tool evidence
// (`vibey-hosted-tools.md` §6.1–§6.2). Pure projections of the Tools-tab read
// (`hosted-tools-status.schema.json`): the server has already folded staleness
// into `status`, so nothing here re-derives routing.
import type {
  HostedCostEstimate,
  HostedEvidence,
  HostedToolCandidate,
  HostedToolExecutor,
  HostedToolName,
  HostedToolProbeResult,
  HostedToolsStatus,
  HostedToolStatus,
} from './types';

/**
 * One call-evidence record as a chip.
 *
 * - `native`: verified; the tool runs natively.
 * - `unsupported`: the Source refused the tool, by probe or by a real error.
 * - `engineUpgraded`: taken on another engine or probe shape. It routes as
 *   unknown, i.e. through the proxy, until the user checks again.
 *
 * `aged` marks a probe record older than 30 days, whichever way it went: it
 * still routes as recorded and only asks for a new check.
 *
 * `null` is no record: unchecked, which is the normal state and draws no chip
 * on a Source (§6.2).
 */
export type HostedEvidenceChip = {
  kind: 'native' | 'unsupported' | 'engineUpgraded';
  aged: boolean;
  at: string | null;
};

export const evidenceChip = (evidence: HostedEvidence): HostedEvidenceChip | null => {
  // Staleness first: a wire-stale record reads `unknown` but still has a past
  // the user needs to see, whatever it once said.
  if (evidence.stale === 'wire') return { kind: 'engineUpgraded', aged: false, at: evidence.observed_at };
  const aged = evidence.stale === 'age';
  if (evidence.status === 'verified') return { kind: 'native', aged, at: evidence.observed_at };
  if (evidence.status === 'unsupported') return { kind: 'unsupported', aged, at: evidence.observed_at };
  return null;
};

export const sameExecutor = (left: HostedToolExecutor | null, right: HostedToolExecutor | null): boolean =>
  left !== null && right !== null && left.source_id === right.source_id && left.model_id === right.model_id;

/** The candidates a user may pick as executor: those verified for the call now. */
export const verifiedCandidates = (tool: HostedToolStatus): HostedToolCandidate[] =>
  tool.candidates.filter((candidate) => candidate.call.status === 'verified');

export type ModelCapability = {
  tool: HostedToolName;
  evidence: HostedEvidence;
  chip: HostedEvidenceChip;
  candidate: HostedToolCandidate;
  /** The tool is on and a turn would bind an executor (`bound`): a model that
   *  cannot run it natively reaches it through that proxy. */
  proxied: boolean;
};

/** A Source model's evidence for each tool, in catalog order, for its detail chips. */
export const modelCapabilities = (status: HostedToolsStatus, sourceId: string, modelId: string): ModelCapability[] =>
  status.tools.flatMap((tool) => {
    const candidate = tool.candidates.find((entry) => entry.source_id === sourceId && entry.model_id === modelId);
    const chip = candidate ? evidenceChip(candidate.call) : null;
    return candidate && chip
      ? [{ tool: tool.name, evidence: candidate.call, chip, candidate, proxied: tool.enabled && tool.bound !== null }]
      : [];
  });

/** Tools a user can check. An image probe waits until Vibey parses image results, so the server refuses it. */
export const probeableTool = (tool: HostedToolName): boolean => tool !== 'image_generation';

export type ProbeTarget = { tool: HostedToolName; candidate: HostedToolCandidate };

export const probeKey = ({ tool, candidate }: ProbeTarget): string => `${tool}:${candidate.source_id}:${candidate.model_id}`;

/**
 * What the header check offers to run: every checkable pair with no current
 * answer, i.e. no record, or one an engine upgrade or age made stale.
 */
export const pendingChecks = (status: HostedToolsStatus): ProbeTarget[] =>
  status.tools.flatMap((tool) => (probeableTool(tool.name)
    ? tool.candidates
      .filter((candidate) => candidate.call.status === 'unknown' || candidate.call.stale !== null)
      .map((candidate) => ({ tool: tool.name, candidate }))
    : []));

/**
 * The cost of running these probes, as the server estimates each one. Unknown is
 * never zero: one unknown estimate makes the total unknown. A set run entirely on
 * subscriptions is included.
 */
/** Whether a probe's answer became no evidence: inconclusive, transient, or a request that failed (null). */
export const probeUnanswered = (result: HostedToolProbeResult | null): boolean =>
  result === null || result.call.outcome === 'inconclusive' || result.call.outcome === 'transient';

export const probeTotal = (targets: readonly ProbeTarget[]): HostedCostEstimate | null => {
  let usd = 0;
  let listed = false;
  for (const { candidate } of targets) {
    const estimate = candidate.probe_estimate;
    if (!estimate) return null;
    if (estimate.basis === 'list') {
      usd += estimate.usd;
      listed = true;
    }
  }
  return listed ? { basis: 'list', usd } : { basis: 'included' };
};
