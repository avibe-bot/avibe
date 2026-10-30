// What the Tools tab and the Source chips say about hosted tools
// (`vibey-hosted-tools.md` §8). Pure projections of the Tools-tab read
// (`hosted-tools-status.schema.json`): positive records are display only,
// refusals are routing state until the user clears them, and nothing here
// decides a route.
import type { HostedRefusal, HostedSuccess, HostedToolName, HostedToolRunStatus, HostedToolsStatus } from './types';

const time = (at: string): number => Date.parse(at);

/** One Source's refusals of a tool on one aspect: the models it refused, and when it last did. */
export type RefusalGroup = { sourceId: string; aspect: HostedRefusal['aspect']; models: string[]; at: string };

export type RefusalSummary = {
  /** Sources that refused the tool itself. */
  call: RefusalGroup[];
  /** Sources that still run the tool but refused earlier results sent back to it. */
  replay: RefusalGroup[];
  /** Distinct Sources across both lines; with two or more, the clears move into Advanced. */
  sources: number;
  /** The Source the tool has run on since every call refusal, when that is another Source. */
  switchedTo: string | null;
};

/**
 * The refusal lines of one tool. A Source that refused the call is left out of
 * the replay line: replay is moot where the tool is not declared natively.
 */
export const refusalSummary = (status: HostedToolRunStatus): RefusalSummary => {
  const groups = new Map<string, RefusalGroup>();
  for (const refusal of status.refused) {
    const key = `${refusal.aspect}:${refusal.source_id}`;
    const group = groups.get(key);
    if (!group) {
      groups.set(key, { sourceId: refusal.source_id, aspect: refusal.aspect, models: [refusal.model_id], at: refusal.at });
      continue;
    }
    if (!group.models.includes(refusal.model_id)) group.models.push(refusal.model_id);
    if (time(refusal.at) > time(group.at)) group.at = refusal.at;
  }
  const call = [...groups.values()].filter((group) => group.aspect === 'call');
  const callSources = new Set(call.map((group) => group.sourceId));
  const replay = [...groups.values()].filter((group) => group.aspect === 'replay' && !callSources.has(group.sourceId));
  const latest = status.latest_ok;
  const switchedTo = latest !== null
    && call.length > 0
    && !callSources.has(latest.source_id)
    && call.every((group) => time(group.at) < time(latest.at))
    ? latest.source_id
    : null;
  return { call, replay, sources: call.length + replay.length, switchedTo };
};

/** One (Source, model) on a tool, as the Advanced status list and the Source chips show it. */
export type PairEvidence =
  | { kind: 'ok'; sourceId: string; modelId: string; success: HostedSuccess }
  | { kind: 'refused'; sourceId: string; modelId: string; refusal: HostedRefusal; lastOk: HostedSuccess | null };

const pairKey = (sourceId: string, modelId: string) => JSON.stringify([sourceId, modelId]);

/**
 * Every (Source, model) the tool has a record for, one entry each. A refusal
 * wins over a success on the same pair, because it is routing state, and a call
 * refusal over a replay refusal.
 */
export const pairEvidence = (status: HostedToolRunStatus): PairEvidence[] => {
  const verified = new Map(status.verified.map((success) => [pairKey(success.source_id, success.model_id), success]));
  const refused = new Map<string, HostedRefusal>();
  for (const refusal of status.refused) {
    const key = pairKey(refusal.source_id, refusal.model_id);
    const held = refused.get(key);
    if (!held || (held.aspect === 'replay' && refusal.aspect === 'call')) refused.set(key, refusal);
  }
  const pairs: PairEvidence[] = [];
  for (const [key, success] of verified) {
    if (!refused.has(key)) pairs.push({ kind: 'ok', sourceId: success.source_id, modelId: success.model_id, success });
  }
  for (const [key, refusal] of refused) {
    pairs.push({ kind: 'refused', sourceId: refusal.source_id, modelId: refusal.model_id, refusal, lastOk: verified.get(key) ?? null });
  }
  return pairs;
};

export type ModelCapability = { tool: HostedToolName; evidence: PairEvidence };

/**
 * A Source model's chips, in catalog order: one per feature with a record. The
 * call is one feature and its replay another. A call refusal wins over a success,
 * because it is routing state, and also hides the replay chip, which is moot while
 * the tool is not declared natively. A replay refusal sits beside the success: the
 * model still runs the tool. No record draws nothing.
 */
export const modelCapabilities = (status: HostedToolsStatus, sourceId: string, modelId: string): ModelCapability[] =>
  status.tools.flatMap((tool): ModelCapability[] => {
    const mine = <T extends { source_id: string; model_id: string }>(item: T) => item.source_id === sourceId && item.model_id === modelId;
    const success = tool.status.verified.find(mine) ?? null;
    const call = tool.status.refused.find((refusal) => mine(refusal) && refusal.aspect === 'call');
    if (call) return [{ tool: tool.name, evidence: { kind: 'refused', sourceId, modelId, refusal: call, lastOk: success } }];
    const replay = tool.status.refused.find((refusal) => mine(refusal) && refusal.aspect === 'replay');
    return [
      ...(success ? [{ tool: tool.name, evidence: { kind: 'ok', sourceId, modelId, success } } satisfies ModelCapability] : []),
      ...(replay ? [{ tool: tool.name, evidence: { kind: 'refused', sourceId, modelId, refusal: replay, lastOk: null } } satisfies ModelCapability] : []),
    ];
  });

/**
 * The executor order to save after moving one shown executor from position `from`
 * to `to` (a drag, or one step of an arrow). The saved order may name Sources the
 * read does not show now (refused, or gone for a while); the server keeps those on
 * purpose and skips them, so a move rearranges only the shown ones within the
 * saved list and leaves every hidden id where it was. A shown executor the saved
 * list lacks follows the list, in its new place.
 */
export const executorOrderAfterReorder = (
  saved: readonly string[] | null,
  shown: readonly string[],
  from: number,
  to: number,
): string[] => {
  const moved = [...shown];
  const [item] = moved.splice(from, 1);
  moved.splice(to, 0, item);
  if (saved === null) return moved;
  const visible = new Set(shown);
  const next = moved[Symbol.iterator]();
  const merged = saved.map((id) => (visible.has(id) ? next.next().value as string : id));
  return [...merged, ...next];
};

/** One arrow step: the move from `index` to its neighbor in `direction`. */
export const executorOrderAfterMove = (
  saved: readonly string[] | null,
  shown: readonly string[],
  index: number,
  direction: -1 | 1,
): string[] => executorOrderAfterReorder(saved, shown, index, index + direction);

/**
 * The copy key under `settings.models.tools.image.description` for an image model
 * id. A dot separates i18n key segments, so the id's dots become underscores.
 */
export const imageModelCopyKey = (modelId: string): string => modelId.replaceAll('.', '_');

