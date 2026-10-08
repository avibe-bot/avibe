import type {
  RunningAgentsResult,
  SessionRuntimeState,
  WorkbenchMessage,
  WorkbenchSession,
} from '@/context/ApiContext';
import { isTranscriptMessage } from '@/lib/chatMessageTypes';

import type { PetPose } from './PetAvatar';

/**
 * What the desktop pet shows for its bound session. One pure derivation from
 * the same reads the Workbench already makes; see
 * docs/plans/2026-10-01-desktop-pet.md ("Pet state").
 */
export type PetState = 'needs_input' | 'blocked' | 'ready' | 'running' | 'idle';

export type PetStateInputs = {
  session: Pick<WorkbenchSession, 'agent_status'> | null;
  /** The loaded tail, oldest first. */
  messages: WorkbenchMessage[];
  turn: Pick<SessionRuntimeState, 'foreground' | 'in_flight'> | null;
  pendingVaultRequests: number;
  unreadCount: number;
};

/** The options of a message's quick-reply group, or `[]` when it has none. */
export const quickReplyOptions = (message: WorkbenchMessage): string[] => {
  const content = message.content as { quick_replies?: unknown } | null | undefined;
  const options = content?.quick_replies;
  return Array.isArray(options)
    ? options.filter((option): option is string => typeof option === 'string' && option.length > 0)
    : [];
};

export const quickReplyChosen = (message: WorkbenchMessage): string | null => {
  const chosen = (message.content as { quick_reply_chosen?: unknown } | null | undefined)?.quick_reply_chosen;
  return typeof chosen === 'string' && chosen ? chosen : null;
};

const isAgentResult = (message: WorkbenchMessage): boolean =>
  message.author === 'agent' && message.type === 'result';

/** The latest agent result in the tail, or null. */
export const latestAgentResult = (messages: WorkbenchMessage[]): WorkbenchMessage | null => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (isAgentResult(messages[index])) return messages[index];
  }
  return null;
};

/**
 * The quick-reply group the agent is waiting on now: the latest agent result's
 * group, while unanswered. An older unanswered group is not an open question —
 * once a newer result exists the agent has moved on — so it never counts
 * (it stays clickable in the Workbench).
 */
export const openQuickReplies = (messages: WorkbenchMessage[]): { message: WorkbenchMessage; options: string[] } | null => {
  const latest = latestAgentResult(messages);
  if (!latest || quickReplyChosen(latest)) return null;
  const options = quickReplyOptions(latest);
  return options.length > 0 ? { message: latest, options } : null;
};

const isRunning = (turn: PetStateInputs['turn']): boolean =>
  turn?.foreground === 'running' || turn?.in_flight === true;

/** Priority: Needs input > Blocked > Ready > Running > Idle. */
export const derivePetState = (inputs: PetStateInputs): PetState => {
  if (inputs.pendingVaultRequests > 0 || openQuickReplies(inputs.messages)) return 'needs_input';
  if (inputs.session?.agent_status === 'failed') return 'blocked';
  if (!isRunning(inputs.turn) && inputs.unreadCount > 0) return 'ready';
  if (isRunning(inputs.turn)) return 'running';
  return 'idle';
};

/**
 * Whether an agent is mid-turn in any conversation the user can see: an
 * active instance of a foreground session, on any surface. Background runs,
 * Runtime-owned sessions and processes without a session do not count, and
 * an unreachable snapshot says nothing is known to be working.
 */
export const conversationAgentWorking = (result: RunningAgentsResult): boolean =>
  result.ok === true
  && result.agents.some((agent) => agent.state === 'active' && agent.visibility === 'foreground');

/**
 * What the avatar draws. The bound session's state wins; when it has nothing
 * to say, an agent working in another conversation keeps the pet busy (and
 * awake). Only the pose follows other sessions: the panel, badge and
 * `data-state` describe the bound session alone.
 */
export const derivePetPose = (
  state: PetState,
  { othersWorking, asleep }: { othersWorking: boolean; asleep: boolean },
): PetPose => {
  if (state !== 'idle') return state;
  if (othersWorking) return 'running';
  return asleep ? 'sleeping' : 'idle';
};

/**
 * The rows the panel shows as the latest exchange: the last user message, then
 * every unread agent result (oldest first), or the last agent result when none
 * is unread. `hasOlder` says the server has rows before the loaded tail.
 * `unreadCount` is the inbox's session count: when it is ahead of the tail,
 * unread rows may sit past what is loaded even if every loaded result is read.
 */
export const latestExchange = (
  messages: WorkbenchMessage[],
  hasOlder: boolean,
  unreadCount = 0,
): {
  user: WorkbenchMessage | null;
  results: WorkbenchMessage[];
  /** False when unread results may reach past the loaded tail, so the pet
   *  must not mark read and sends the user to the Workbench instead. */
  unreadComplete: boolean;
} => {
  const transcript = messages.filter((message) => isTranscriptMessage(message));
  let user: WorkbenchMessage | null = null;
  for (let index = transcript.length - 1; index >= 0; index -= 1) {
    if (transcript[index].author === 'user') {
      user = transcript[index];
      break;
    }
  }
  const results = transcript.filter(isAgentResult);
  const unread = results.filter((message) => message.read_at === null);
  if (unread.length === 0) {
    const last = results[results.length - 1];
    // Inbox says something is unread, but the tail has no unread result: those
    // replies sit past the loaded window. A fully-loaded session treats a
    // leftover count as stale.
    return { user, results: last ? [last] : [], unreadComplete: unreadCount <= 0 || !hasOlder };
  }
  // Unread rows reach past the tail when the oldest loaded result is itself
  // unread, or when the inbox count is ahead of the unread results we have.
  const unreadComplete = !hasOlder || (results[0].read_at !== null && unread.length >= unreadCount);
  return { user, results: unread, unreadComplete };
};
