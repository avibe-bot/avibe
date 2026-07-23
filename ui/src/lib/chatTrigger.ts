// Provenance click-through for a harness trigger message in Chat (contract
// A9a/A9b). A pure mapper so the branching is unit-tested without the component.
//
// - A9a "自动触发" (agent callback): link to the SOURCE session's chat, labelled
//   by its title prefix (fallback: source agent name + short session id).
// - A9b "定时任务" / "Watch 监听": link to the matching Harness tab filtered to
//   this session, reusing the backgroundActivity deep-link helper.
// Other harness rows (webhook, or an agent callback whose source didn't resolve)
// stay non-navigating.
import type { WorkbenchMessage } from '../context/ApiContext';
import { harnessNavPath } from './backgroundActivity';

export type ChatTriggerLink =
  | { kind: 'source'; to: string; label: string }
  | { kind: 'harness'; to: string };

const AGENT_RUN_NATIVE_PREFIX = 'agent_run:';

// True for a live agent-callback ("自动触发") harness message that has NOT yet
// been enriched with its source session (A9a). The live ``message.new`` payload
// is published raw (before ``list_session_messages`` resolves the source), so
// ChatPage triggers a targeted reconcile on these to pull the enriched REST row
// (``mergeById`` then fills ``source_session_*`` in place) — otherwise the source
// chip would only appear after a manual reload/refocus.
export function isUnresolvedAgentCallback(
  message: Pick<WorkbenchMessage, 'source' | 'native_message_id' | 'source_session_id'>,
): boolean {
  return (
    message.source === 'harness' &&
    typeof message.native_message_id === 'string' &&
    message.native_message_id.startsWith(AGENT_RUN_NATIVE_PREFIX) &&
    message.source_session_id == null
  );
}

// author_name values that map to the Harness "tasks" tab (watch → "watches").
// Includes the legacy/queued-restore `task` trigger kind alongside scheduled/task_run.
const TASK_KINDS = new Set(['scheduled', 'task_run', 'task']);
const TITLE_PREFIX_MAX = 12;

function titlePrefix(title: string): string {
  const trimmed = title.trim();
  return trimmed.length > TITLE_PREFIX_MAX ? `${trimmed.slice(0, TITLE_PREFIX_MAX)}…` : trimmed;
}

type TriggerFields = Pick<
  WorkbenchMessage,
  | 'source'
  | 'author_name'
  | 'author_id'
  | 'session_id'
  | 'source_session_id'
  | 'source_session_title'
  | 'source_session_agent_name'
>;

// ``agentFallback`` is the localized word for "agent" (chat.source.agentFallback);
// passed in so this stays a pure, translation-free mapper.
export function chatTriggerLink(message: TriggerFields, agentFallback: string): ChatTriggerLink | null {
  if (message.source !== 'harness') return null;

  const sourceId = message.source_session_id;
  if (sourceId) {
    const title = message.source_session_title?.trim();
    const label = title
      ? titlePrefix(title)
      : `${message.source_session_agent_name?.trim() || agentFallback} · ${sourceId.slice(-6)}`;
    return { kind: 'source', to: `/chat/${encodeURIComponent(sourceId)}`, label };
  }

  const kind = message.author_name;
  if (kind === 'watch' || TASK_KINDS.has(kind ?? '')) {
    const itemKind = kind === 'watch' ? 'watch' : 'task';
    return {
      kind: 'harness',
      to: harnessNavPath({ id: `${itemKind}:${message.author_id ?? ''}`, item_kind: itemKind }, message.session_id),
    };
  }
  return null;
}
