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

// author_name values that map to the Harness "tasks" tab (watch → "watches").
const TASK_KINDS = new Set(['scheduled', 'task_run']);
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

export function chatTriggerLink(message: TriggerFields): ChatTriggerLink | null {
  if (message.source !== 'harness') return null;

  const sourceId = message.source_session_id;
  if (sourceId) {
    const title = message.source_session_title?.trim();
    const label = title
      ? titlePrefix(title)
      : `${message.source_session_agent_name?.trim() || 'agent'} · ${sourceId.slice(-6)}`;
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
