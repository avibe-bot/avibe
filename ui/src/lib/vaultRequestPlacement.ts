import type { VaultRequest, WorkbenchMessage } from '@/context/ApiContext';
import { chatRowKind } from '@/lib/chatRowKind';
import { specFor } from '@/lib/messageTypes';
import { messageOrderTimeMs, timestampOrderTimeMs, transcriptOrderTimeMs } from '@/lib/transcriptOrder';

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

function isInputTurn(message: WorkbenchMessage): boolean {
  return specFor(message.type).inputAuthors.includes(message.author);
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

/** The transcript message that started the turn which created a request. */
export function vaultRequestSourceMessageId(request: VaultRequest): string | null {
  const requester = record(request.requester);
  const messageId = requester.message_id;
  return typeof messageId === 'string' && messageId.trim() ? messageId : null;
}

export function vaultRequestSourceTurnId(request: VaultRequest): string | null {
  const requester = record(request.requester);
  const turnId = requester.turn_id;
  return typeof turnId === 'string' && turnId.trim() ? turnId : null;
}

export function vaultRequestSourceRunId(request: VaultRequest): string | null {
  const requester = record(request.requester);
  const runId = requester.run_id;
  return typeof runId === 'string' && runId.trim() ? runId : null;
}

export function vaultRequestSourcePlatform(request: VaultRequest): string | null {
  const requester = record(request.requester);
  const platform = requester.platform;
  return typeof platform === 'string' && platform.trim() ? platform : null;
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

function inferReplyWithinTurn(
  messages: WorkbenchMessage[],
  requestTime: number,
): WorkbenchMessage | undefined {
  for (const message of messages) {
    const messageTime = messageOrderTimeMs(message);
    if (Number.isNaN(messageTime) || messageTime < requestTime) continue;
    // An anchorless request belongs only to the turn that created it. Once a
    // later user/harness input starts another turn, no later Agent row can own it.
    if (isInputTurn(message)) return undefined;
    if (isAgentReply(message)) return message;
  }
  return undefined;
}

function inferReplyFromSourceMessage(
  messages: WorkbenchMessage[],
  sourceMessageId: string,
  requestTime: number,
  sourcePlatform: string | null,
): WorkbenchMessage | undefined {
  const sourceIndex = messages.findIndex(
    (message) => message.id === sourceMessageId || (
      message.native_message_id === sourceMessageId &&
      (!sourcePlatform || message.platform === sourcePlatform)
    ),
  );
  if (sourceIndex < 0) return undefined;
  let replyBeforeRequest: WorkbenchMessage | undefined;

  for (const message of messages.slice(sourceIndex + 1)) {
    if (isInputTurn(message)) {
      // A request may be persisted after its reply but before the next input.
      // In that case the recorded pre-request reply is still the owner. Only
      // discard the source identity when the later input predates the request.
      const messageTime = transcriptOrderTimeMs(message);
      return replyBeforeRequest && !Number.isNaN(requestTime) &&
        !Number.isNaN(messageTime) && messageTime >= requestTime
        ? replyBeforeRequest
        : undefined;
    }
    if (!isAgentReply(message)) continue;
    if (Number.isNaN(requestTime) || messageOrderTimeMs(message) >= requestTime) return message;
    // Some legacy request rows point at an older input even though the request
    // was persisted after that turn's reply. Keep it as a fallback only when
    // the source turn never crosses another input boundary.
    replyBeforeRequest ??= message;
  }
  return replyBeforeRequest;
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
  sourceMessageIds: ReadonlyMap<string, string> = new Map(),
): VaultProvisionPlacement {
  const byMessageId = new Map<string, VaultRequest[]>();
  const unanchored: VaultRequest[] = [];
  const messagesById = new Map(messages.map((message) => [message.id, message]));
  const agentMessages = messages.filter(isAgentReply);
  // Window coverage follows transcript-entry order. A queued row can be authored
  // before the request but only enter the visible transcript after it, so its
  // message-id clock must not make a trimmed request look loaded.
  const firstLoadedTime = messages.length > 0 ? transcriptOrderTimeMs(messages[0]) : Number.NaN;

  for (const request of requests) {
    if (vaultRequestType(request) !== 'provision') continue;

    const explicit = request.message_id ? messagesById.get(request.message_id) : undefined;
    if (explicit && isAgentReply(explicit)) {
      appendRequest(byMessageId, explicit.id, request);
      continue;
    }

    const requestTime = timestampOrderTimeMs(request.created_at);
    const sameTurn = agentMessages.find((message) => sameRequestTurn(request, message));
    // The request row can be written after the Agent reply has already been
    // persisted, so its creation timestamp is not a reliable turn boundary.
    // Newer requesters carry the input message id; use that identity first.
    const explicitSource = vaultRequestSourceMessageId(request);
    const fromSource = explicitSource ?? sourceMessageIds.get(request.id) ?? null;
    const sourcePlatform = vaultRequestSourcePlatform(request);
    const sourceResolved = Boolean(fromSource && messages.some(
      (message) => message.id === fromSource || (
        message.native_message_id === fromSource &&
        (!sourcePlatform || message.platform === sourcePlatform)
      ),
    ));
    const sourceUnresolved = Boolean(explicitSource && !sourceResolved);
    // Do not guess when the request predates the retained message window: its
    // real owner may have been trimmed, and attaching it to the first visible
    // later reply would move the card to an unrelated turn.
    const windowCoversRequest = Number.isNaN(firstLoadedTime) || firstLoadedTime <= requestTime;
    const fromSourceReply = fromSource
      ? inferReplyFromSourceMessage(messages, fromSource, requestTime, sourcePlatform)
      : undefined;
    const inferred = sameTurn ?? fromSourceReply
      ?? (sourceUnresolved || Number.isNaN(requestTime) || !windowCoversRequest
        ? undefined
        : inferReplyWithinTurn(messages, requestTime));
    if (inferred) appendRequest(byMessageId, inferred.id, request);
    else unanchored.push(request);
  }

  return { byMessageId, unanchored };
}
