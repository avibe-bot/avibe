import { modelsApi } from '../settings/models/modelsApi';
import type { Source, TurnProvenance } from '../settings/models/types';

const sourceNames = (sources: Source[]): Record<string, string> =>
  Object.fromEntries(sources.map((source) => [source.id, source.display_name]));

// A transcript can hold many failure notices; they share one Sources read and
// each Turn's record is read once, however often its row remounts. A record
// is immutable once written, and a failed read is dropped so a later mount
// may try again. Only the most recently read records are kept, so a long
// session paging through chats holds a bounded set.
const NAMES_TTL_MS = 30_000;
export const RECORDS_LIMIT = 200;
let namesRead: { at: number; value: Promise<Record<string, string>> } | null = null;
const records = new Map<string, Promise<TurnProvenance>>();

export function readNames(): Promise<Record<string, string>> {
  if (!namesRead || Date.now() - namesRead.at > NAMES_TTL_MS) {
    namesRead = { at: Date.now(), value: modelsApi.listSources().then(sourceNames, () => ({})) };
  }
  return namesRead.value;
}

export function readRecord(turnId: string): Promise<TurnProvenance> {
  let read = records.get(turnId);
  if (read) {
    records.delete(turnId);
  } else {
    read = modelsApi.getTurnProvenance(turnId);
    read.catch(() => { if (records.get(turnId) === read) records.delete(turnId); });
  }
  records.set(turnId, read);
  if (records.size > RECORDS_LIMIT) records.delete(records.keys().next().value as string);
  return read;
}

export function resetFailureDetailsCache(): void {
  namesRead = null;
  records.clear();
}
