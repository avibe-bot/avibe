export type MemoryCommandResult = {
  schema_version: 1;
  type: 'memory_command_result';
  command: string;
  result: Record<string, unknown>;
};

export type WorkbenchMessageResponse =
  | { kind: 'memory_command_result'; result: MemoryCommandResult }
  | { kind: 'already_answered' }
  | { kind: 'queued' }
  | { kind: 'message'; message: Record<string, unknown> }
  | { kind: 'other' };

type PlainMemoryCommandRequest = {
  hasAttachments: boolean;
  hasReferences: boolean;
  metadata?: Record<string, unknown>;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * Read the closed Workbench `/memory` response before ordinary message-response
 * handling sees an incidental `id` or queue field.
 */
export function memoryCommandResultFromResponse(value: unknown): MemoryCommandResult | null {
  if (!isRecord(value) || !isRecord(value.memory_command_result)) return null;
  const result = value.memory_command_result;
  if (
    result.schema_version !== 1 ||
    result.type !== 'memory_command_result' ||
    typeof result.command !== 'string' ||
    !result.command ||
    !isRecord(result.result)
  ) {
    return null;
  }
  return {
    schema_version: 1,
    type: 'memory_command_result',
    command: result.command,
    result: result.result,
  };
}

/**
 * Classify a successful Workbench create-message response. The typed Memory
 * result deliberately wins over incidental ordinary-message fields.
 */
export function routeWorkbenchMessageResponse(value: unknown): WorkbenchMessageResponse {
  const memoryResult = memoryCommandResultFromResponse(value);
  if (memoryResult) return { kind: 'memory_command_result', result: memoryResult };
  if (!isRecord(value)) return { kind: 'other' };
  if (value.already_answered) return { kind: 'already_answered' };
  if (value.queued) return { kind: 'queued' };
  if (value.id) return { kind: 'message', message: value };
  return { kind: 'other' };
}

/**
 * Identify a local, text-only Memory request so ChatPage does not claim a
 * foreground turn before the server returns its direct-read result.
 */
export function isPlainMemoryCommandRequest(
  text: string,
  { hasAttachments, hasReferences, metadata }: PlainMemoryCommandRequest,
): boolean {
  if (hasAttachments || hasReferences || metadata?.quick_reply_for) return false;
  if (['forwarded', 'is_forwarded', 'forward_origin', 'forwarded_from'].some((key) => metadata?.[key])) {
    return false;
  }
  const normalized = text.normalize('NFC').replace(/\r\n?/gu, '\n').trim();
  return normalized === '/memory' || (
    normalized.startsWith('/memory') &&
    normalized.length > '/memory'.length &&
    /\s/u.test(normalized['/memory'.length])
  );
}
