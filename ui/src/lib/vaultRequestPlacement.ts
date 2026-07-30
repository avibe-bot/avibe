import type { VaultRequest, WorkbenchMessage } from '@/context/ApiContext';
import { chatRowKind } from '@/lib/chatRowKind';

export type VaultRequestType = 'access' | 'sign' | 'provision' | 'other';

export function vaultRequestType(request: VaultRequest): VaultRequestType {
  const cardType = (request.card as { request_type?: unknown } | null)?.request_type;
  const type = typeof cardType === 'string' ? cardType : request.request_type;
  return type === 'access' || type === 'sign' || type === 'provision' ? type : 'other';
}

export function isVaultApprovalRequest(request: VaultRequest): boolean {
  const type = vaultRequestType(request);
  return type === 'access' || type === 'sign';
}

export type VaultProvisionPlacement = {
  byMessageId: Map<string, VaultRequest[]>;
  unanchored: VaultRequest[];
};

function isAgentReply(message: WorkbenchMessage): boolean {
  return chatRowKind(message).kind === 'agent';
}

function appendRequest(map: Map<string, VaultRequest[]>, messageId: string, request: VaultRequest): void {
  const current = map.get(messageId);
  if (current) current.push(request);
  else map.set(messageId, [request]);
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function sameRequestTurn(request: VaultRequest, message: WorkbenchMessage): boolean {
  const requester = record(request.requester);
  const metadata = record(message.metadata);
  for (const key of ['turn_id', 'run_id'] as const) {
    const requestId = typeof requester[key] === 'string' ? requester[key] : '';
    if (requestId && requestId === metadata[key]) return true;
  }
  return false;
}

/** Attach provision requests to the Agent reply that announced them.
 *
 * Newer producers can set `message_id` explicitly. Historical CLI-created rows
 * do not, so use the session's serialized turn order: the first Agent-authored
 * message created after the request is the reply that owns it. Requests without
 * either anchor remain visible at the transcript tail until a reply arrives.
 */
export function placeVaultProvisionRequests(
  messages: WorkbenchMessage[],
  requests: VaultRequest[],
): VaultProvisionPlacement {
  const byMessageId = new Map<string, VaultRequest[]>();
  const unanchored: VaultRequest[] = [];
  const messagesById = new Map(messages.map((message) => [message.id, message]));
  const agentMessages = messages.filter(isAgentReply);
  const firstLoadedTime = messages.length > 0 ? Date.parse(messages[0].created_at) : Number.NaN;

  for (const request of requests) {
    if (vaultRequestType(request) !== 'provision') continue;

    const explicit = request.message_id ? messagesById.get(request.message_id) : undefined;
    if (explicit && isAgentReply(explicit)) {
      appendRequest(byMessageId, explicit.id, request);
      continue;
    }

    const requestTime = Date.parse(request.created_at);
    const sameTurn = agentMessages.find((message) => sameRequestTurn(request, message));
    // Do not guess when the request predates the retained message window: its
    // real owner may have been trimmed, and attaching it to the first visible
    // later reply would move the card to an unrelated turn.
    const windowCoversRequest = Number.isNaN(firstLoadedTime) || firstLoadedTime <= requestTime;
    const inferred = sameTurn ?? (Number.isNaN(requestTime) || !windowCoversRequest
      ? undefined
      : agentMessages.find((message) => {
          const messageTime = Date.parse(message.created_at);
          return !Number.isNaN(messageTime) && messageTime >= requestTime;
        }));
    if (inferred) appendRequest(byMessageId, inferred.id, request);
    else unanchored.push(request);
  }

  return { byMessageId, unanchored };
}
