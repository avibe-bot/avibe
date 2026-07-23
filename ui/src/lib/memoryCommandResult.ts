export type MemoryCommandResult = {
  schema_version: 1;
  type: 'memory_command_result';
  command: string;
  result: Record<string, unknown>;
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
