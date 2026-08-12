export type UnknownWriteReconciliation<T> =
  | { kind: 'committed'; value: T }
  | { kind: 'absent' }
  | { kind: 'unread' };

/**
 * A lost mutation response is not a failure verdict. Read the authoritative
 * collection and expose another write only after that read proves absence.
 */
export async function reconcileUnknownWrite<Collection, Value>(
  read: () => Promise<Collection>,
  committedValue: (collection: Collection) => Value | undefined,
): Promise<UnknownWriteReconciliation<Value>> {
  try {
    const collection = await read();
    const value = committedValue(collection);
    return value === undefined ? { kind: 'absent' } : { kind: 'committed', value };
  } catch {
    return { kind: 'unread' };
  }
}
