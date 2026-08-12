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
const HARNESS_NATIVE_PREFIXES = ['agent_run:', 'scheduled:', 'watch:', 'webhook:', 'hook:'];

// Live Harness rows can arrive before read-side enrichment. Agent callbacks need
// source-session resolution; legacy queued triggers may need kind/definition
// recovery from their stable native id. A targeted reconcile fills either set.
export function needsHarnessProvenanceReconcile(
  message: Pick<
    WorkbenchMessage,
    'source' | 'native_message_id' | 'source_session_id' | 'author_name'
  >,
): boolean {
  if (message.source !== 'harness' || typeof message.native_message_id !== 'string') return false;
  if (message.author_name == null) {
    return HARNESS_NATIVE_PREFIXES.some((prefix) => message.native_message_id?.startsWith(prefix));
  }
  return (
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
> & { metadata?: Record<string, unknown> };

const VAULT_CALLBACK_STATUS_KEYS: Record<string, string> = {
  'provision:fulfilled': 'chat.source.vaultProvided',
  'access:approved': 'chat.source.vaultAccessApproved',
  'sign:approved': 'chat.source.vaultSigned',
  'access:denied': 'chat.source.vaultDenied',
  'sign:denied': 'chat.source.vaultDenied',
  'provision:denied': 'chat.source.vaultDenied',
  'access:failed': 'chat.source.vaultFailed',
  'sign:failed': 'chat.source.vaultFailed',
  'provision:failed': 'chat.source.vaultFailed',
  'access:expired': 'chat.source.vaultExpired',
  'sign:expired': 'chat.source.vaultExpired',
  'provision:expired': 'chat.source.vaultExpired',
};

function vaultCallbackMetadata(message: TriggerFields): { requestType: string; status: string } | null {
  if (message.source !== 'harness') return null;
  const metadata = message.metadata;
  const sourceActor = typeof metadata?.source_actor === 'string' ? metadata.source_actor : '';
  if (!sourceActor.startsWith('vault:')) return null;
  const requestType = typeof metadata?.vault_request_type === 'string' ? metadata.vault_request_type : '';
  const status = typeof metadata?.vault_request_status === 'string' ? metadata.vault_request_status : '';
  return { requestType, status };
}

export function isVaultCallback(message: TriggerFields): boolean {
  if (message.source !== 'harness') return false;
  const metadata = message.metadata;
  const sourceActor = typeof metadata?.source_actor === 'string' ? metadata.source_actor : '';
  return sourceActor.startsWith('vault:');
}

export function vaultCallbackStatusKey(message: TriggerFields): string | null {
  const metadata = vaultCallbackMetadata(message);
  if (!metadata) return null;
  return VAULT_CALLBACK_STATUS_KEYS[`${metadata.requestType}:${metadata.status}`] ?? null;
}

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
  // ``show_intent`` (a page button action) stays a non-navigating harness row.
  // ``show_annotation`` no longer reaches here at all: an annotation is typed
  // ``annotation`` and renders as its own card, never as a harness trigger row.
  if (kind === 'show_intent') return null;
  if (kind === 'watch' || TASK_KINDS.has(kind ?? '')) {
    const itemKind = kind === 'watch' ? 'watch' : 'task';
    return {
      kind: 'harness',
      to: harnessNavPath({ id: `${itemKind}:${message.author_id ?? ''}`, item_kind: itemKind }, message.session_id),
    };
  }
  return null;
}

// i18n key for a harness chip's leading label. Branches on source presence:
//  - a resolved agent callback (source_session_id set) uses the "From" PREFIX
//    (chat.source.from) that leads into the source-session link → "From · <title>↗".
//  - an unresolved one (source deleted, or a pre-enrichment live row) falls back to
//    the SELF-CONTAINED "From agent" (chat.source.harness) — still source semantics,
//    never the mechanism-flavored "Automated" that read as system-triggered.
//  - Task / Watch / Webhook keep their own self-contained labels.
// Pure so the branch is unit-tested alongside chatTriggerLink.
export function harnessChipLabelKey(message: TriggerFields): string {
  if (message.source === 'harness' && message.source_session_id) return 'chat.source.from';
  if (isVaultCallback(message)) return 'chat.source.vault';
  const kind = message.author_name;
  if (kind === 'show_intent') return 'chat.source.showIntent';
  if (kind === 'watch') return 'chat.source.watch';
  if (kind === 'webhook') return 'chat.source.webhook';
  if (kind === 'hook') return 'chat.source.hook';
  if (kind === 'activity_recovery') return 'chat.source.activityRecovery';
  if (TASK_KINDS.has(kind ?? '')) return 'chat.source.scheduled';
  return 'chat.source.harness';
}
