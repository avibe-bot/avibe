import {
  getLocalSnapshotPrincipal,
  registerLocalSnapshotClearHook,
  subscribeLocalSnapshotPrincipal,
} from './localSnapshots';
import type { SessionDraftAttachment } from './sessionDraftAttachmentsCache';

// A message the reader sent from a chat's composer, kept on the device until the
// instance answers the send. The composer empties at Send, so without this a page
// torn down before the answer (an iPhone App switched away, a reload, the App
// leaving the instance) loses a message the instance may never have received.
//
//  * The send writes it, in the same task that empties the composer, and the
//    send's settlement deletes it, whatever the answer: an accepted or queued
//    message is the instance's, and a refused one goes back to the composer by the
//    failed-send path, which needs no record.
//  * Only a page that never saw the answer leaves one. The next page to open that
//    chat compares it with what the instance holds: a row carrying its client id
//    (``metadata.client_message_id``) in the transcript or the queue means the
//    instance received it, so the record goes; otherwise its text and attachments
//    go back to the composer as a draft. It is never sent again by itself: the
//    instance does not deduplicate by client id, and a message twice is worse than
//    a draft the reader sends again.
//  * It belongs to the reader who sent it, as the local snapshots do
//    (docs/plans/web-ui-local-cache.md): none is kept while the reader is not
//    named, a record of another reader is deleted and never restored, and the
//    records go when the reader's local data does (sign-out, revocation; an
//    authorization change keeps only the current reader's).
//  * A send still out is its sending page's, in this tab or another: no page
//    restores it. The sending page holds a Web Lock named for it until the answer
//    settles it, and the browser releases that lock when the page is torn down,
//    so a record whose lock nobody holds belongs to a page that is gone.

export const UNACKNOWLEDGED_SEND_PREFIX = 'avibe.unacknowledged-send.v1.';

export type UnacknowledgedSend = {
  version: 1;
  sessionId: string;
  clientId: string;
  principal: string;
  text: string;
  attachments: SessionDraftAttachment[];
  sentAt: number;
};

type SendStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;

const inFlight = new Map<string, () => void>();
const LOCK_PREFIX = 'avibe.unacknowledged-send:';

type Locks = Pick<LockManager, 'request' | 'query'>;
function locks(): Locks | undefined {
  try {
    return typeof navigator !== 'undefined' ? navigator.locks : undefined;
  } catch {
    return undefined;
  }
}

function storage(): SendStorage | undefined {
  try {
    return typeof window !== 'undefined' ? window.localStorage : undefined;
  } catch {
    return undefined;
  }
}

const sessionPrefix = (sessionId: string) => `${UNACKNOWLEDGED_SEND_PREFIX}${encodeURIComponent(sessionId)}:`;
const keyOf = (sessionId: string, clientId: string) => `${sessionPrefix(sessionId)}${encodeURIComponent(clientId)}`;

function keys(target: SendStorage, prefix: string): string[] {
  const found: string[] = [];
  for (let index = 0; index < target.length; index += 1) {
    const key = target.key(index);
    if (key?.startsWith(prefix)) found.push(key);
  }
  return found;
}

function parse(raw: string | null): UnacknowledgedSend | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<UnacknowledgedSend>;
    if (
      value.version !== 1
      || typeof value.sessionId !== 'string'
      || typeof value.clientId !== 'string'
      || !value.clientId
      || typeof value.principal !== 'string'
      || typeof value.text !== 'string'
      || !Array.isArray(value.attachments)
      || typeof value.sentAt !== 'number'
    ) return null;
    return value as UnacknowledgedSend;
  } catch {
    return null;
  }
}

/** Keep a send on the device until its answer settles it (``settleSend``). */
export function rememberSend(send: Omit<UnacknowledgedSend, 'version' | 'principal' | 'sentAt'>): void {
  const principal = getLocalSnapshotPrincipal();
  if (!principal) return;
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  inFlight.set(send.clientId, release);
  void locks()?.request(`${LOCK_PREFIX}${send.clientId}`, () => held).catch(() => {});
  try {
    storage()?.setItem(keyOf(send.sessionId, send.clientId), JSON.stringify({
      version: 1,
      ...send,
      principal,
      sentAt: Date.now(),
    } satisfies UnacknowledgedSend));
  } catch {
    // Storage full or denied: the send goes on as before, unprotected.
  }
}

/** The send's answer arrived (or this page withdrew it): nothing is left to recover. */
export function settleSend(sessionId: string, clientId: string): void {
  forgetUnacknowledgedSend(sessionId, clientId);
  inFlight.get(clientId)?.();
  inFlight.delete(clientId);
}

export function forgetUnacknowledgedSend(sessionId: string, clientId: string): void {
  try {
    storage()?.removeItem(keyOf(sessionId, clientId));
  } catch {
    // Best effort: a record that stays is matched against the chat again.
  }
}

/** Every record of this chat, oldest first, except sends whose page is still
 *  waiting for the answer: this one's, or another tab's (its lock is held). */
export async function unacknowledgedSends(sessionId: string): Promise<UnacknowledgedSend[]> {
  const records = storedSends(sessionId);
  if (records.length === 0) return records;
  let live = new Set<string>();
  try {
    const state = await locks()?.query();
    // A lock just asked for and not yet granted is as live as a held one.
    live = new Set([...(state?.held ?? []), ...(state?.pending ?? [])].map((lock) => lock.name ?? ''));
  } catch {
    // Without Web Locks only this document's own sends are known to be out.
  }
  return records.filter((record) => !inFlight.has(record.clientId) && !live.has(`${LOCK_PREFIX}${record.clientId}`));
}

const CLAIM_PREFIX = 'avibe.unacknowledged-send-claim:';

/** Decide on this chat's records under a lock every tab takes for the chat, so a
 *  record is given back by one page only: ``decide`` sees each record still
 *  stored, read again under the lock, and a record it returns true for is deleted. */
export async function claimUnacknowledgedSends(
  sessionId: string,
  decide: (send: UnacknowledgedSend) => boolean,
): Promise<void> {
  const run = async () => {
    for (const send of storedSends(sessionId)) {
      if (decide(send)) forgetUnacknowledgedSend(sessionId, send.clientId);
    }
  };
  const manager = locks();
  if (!manager) return run();
  await manager.request(`${CLAIM_PREFIX}${sessionId}`, run);
}

function storedSends(sessionId: string): UnacknowledgedSend[] {
  const target = storage();
  if (!target) return [];
  try {
    const found: UnacknowledgedSend[] = [];
    for (const key of keys(target, sessionPrefix(sessionId))) {
      const record = parse(target.getItem(key));
      if (!record || record.sessionId !== sessionId) {
        target.removeItem(key);
        continue;
      }
      found.push(record);
    }
    return found.sort((left, right) => left.sentAt - right.sentAt);
  } catch {
    return [];
  }
}

function forgetWhere(prefix: string, keep: (record: UnacknowledgedSend) => boolean): void {
  const target = storage();
  if (!target) return;
  try {
    for (const key of keys(target, prefix)) {
      const record = parse(target.getItem(key));
      if (!record || !keep(record)) target.removeItem(key);
    }
  } catch {
    // Best effort; a record of another reader is still never restored.
  }
}

export function forgetSessionUnacknowledgedSends(sessionId: string): void {
  forgetWhere(sessionPrefix(sessionId), () => false);
}

// A reader named, or this reader's local data cleared: what anyone else sent
// goes. A sign-out unnames the reader before it clears, so then all of it goes.
const keepOnly = (principal: string | null) => {
  forgetWhere(UNACKNOWLEDGED_SEND_PREFIX, (record) => principal !== null && record.principal === principal);
};
subscribeLocalSnapshotPrincipal((next) => {
  if (next) keepOnly(next);
});
registerLocalSnapshotClearHook(async () => keepOnly(getLocalSnapshotPrincipal()));
