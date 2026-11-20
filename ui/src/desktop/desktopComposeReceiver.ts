import type { ComposeTarget } from '../context/ComposerBridgeContext';
import { MAIN_BOT_SESSION_ID } from '../lib/bots';
import { quoteText } from '../lib/quoteText';

// Ask Vibey (macOS Services): the desktop shell puts a selection into the main
// Bot's composer on this instance. Native drives this receiver through fenced
// eval (desktop/src-tauri/src/compose.rs, contract version 1); nothing here
// sends a message. DesktopCompose installs it. ui/design: composer/ask-vibey/inserted-zh

export const RECEIVER = '__AVIBE_DESKTOP_COMPOSE__';
const TEXT_LIMIT = 1048576;
const FILE_LIMIT = 100 * 1024 * 1024;
const FILE_COUNT = 10;
const OPEN_TIMEOUT = 15000;
const READY_TIMEOUT = 5000;

type Refusal = 'too_large' | 'empty' | 'unreadable';
type FileFacts = { name: string; type: string; size: number } | { name: string; refused: Refusal };
export type Incoming = { id: string; kind: 'text' | 'url' | 'files' | 'unreadable'; text?: string; files: FileFacts[] };
export type Status = 'receiving' | 'inserting' | 'done' | 'unavailable';

export type Receiver = {
  version: 1;
  /** This document's identity. Native reads it once and fences every later
   *  call to it, so a reloaded or replaced document answers 'stale'. */
  document: string;
  begin: (share: unknown) => string;
  chunk: (id: string, index: number, data: string) => string;
  commit: (id: string) => string;
  status: (id: string) => string;
};

export type ReceiverDeps = {
  /** The open chat's compose target, read fresh on every call. */
  target: () => ComposeTarget | null;
  /** Show the main Bot's conversation if it is not the current route. */
  openVibey: () => void;
  /** Report a refusal with the composer's existing attachment copy. */
  toast: (key: RefusalCopy) => void;
  wait?: (ms: number) => Promise<void>;
  document?: string;
};

export type RefusalCopy = 'chat.compose.attachmentTooLarge' | 'chat.compose.attachmentEmpty' | 'chat.compose.attachmentFailed';
const REFUSAL_COPY: Record<Refusal, RefusalCopy> = {
  too_large: 'chat.compose.attachmentTooLarge',
  empty: 'chat.compose.attachmentEmpty',
  unreadable: 'chat.compose.attachmentFailed',
};

const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);

function parse(value: unknown): Incoming | null {
  if (!isObject(value) || typeof value.id !== 'string' || !value.id || !Array.isArray(value.files)) return null;
  const kind = value.kind;
  if (kind !== 'text' && kind !== 'url' && kind !== 'files' && kind !== 'unreadable') return null;
  if (kind === 'text' || kind === 'url') {
    if (typeof value.text !== 'string' || !value.text.trim() || value.text.length > TEXT_LIMIT || value.files.length) return null;
  } else if (value.text !== undefined) return null;
  if (kind !== 'files' && value.files.length) return null;
  if (kind === 'files' && (value.files.length === 0 || value.files.length > FILE_COUNT)) return null;
  for (const file of value.files) {
    if (!isObject(file) || typeof file.name !== 'string' || !file.name) return null;
    if ('refused' in file) {
      if (!(file.refused === 'too_large' || file.refused === 'empty' || file.refused === 'unreadable')) return null;
    } else if (typeof file.type !== 'string' || !Number.isSafeInteger(file.size) || (file.size as number) <= 0 || (file.size as number) > FILE_LIMIT) return null;
  }
  return value as Incoming;
}

function decode(data: string): Uint8Array | null {
  try {
    const raw = atob(data);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index++) bytes[index] = raw.charCodeAt(index);
    return bytes;
  } catch {
    return null;
  }
}

const sleep = (ms: number) => new Promise<void>((resolve) => { window.setTimeout(resolve, ms); });

/**
 * The page side of the contract, without React: it holds each share until
 * native commits it, then inserts shares one at a time in arrival order.
 */
export function createReceiver(deps: ReceiverDeps): Receiver {
  const wait = deps.wait ?? sleep;
  const shares = new Map<string, { share: Incoming; parts: Uint8Array[][]; received: number[]; status: Status }>();
  let queue: Promise<void> = Promise.resolve();

  const until = async <T,>(read: () => T | null | undefined | false, timeout: number): Promise<T | null> => {
    for (let waited = 0; ; waited += 100) {
      const value = read();
      if (value) return value;
      if (waited >= timeout) return null;
      await wait(100);
    }
  };
  const vibey = () => {
    const target = deps.target();
    return target?.sessionId === MAIN_BOT_SESSION_ID ? target : null;
  };

  async function insert(share: Incoming, parts: Uint8Array[][]): Promise<Status> {
    deps.openVibey();
    const opened = await until(vibey, OPEN_TIMEOUT);
    if (!opened) return 'unavailable';
    if (!opened.composerReady) opened.showChat();
    // Ready waits for the saved draft to hydrate, which a slow instance can take a while to answer.
    const ready = await until(() => { const target = vibey(); return target?.composerReady ? target : null; }, OPEN_TIMEOUT);
    if (!ready) return 'unavailable';
    if (share.kind === 'text' || share.kind === 'url') {
      const line = share.kind === 'text' ? quoteText(share.text!) : share.text!.trim();
      if (!await until(() => vibey()?.appendLine(line), READY_TIMEOUT)) return 'unavailable';
    }
    const files: File[] = [];
    const refusals = new Set<Refusal>();
    share.files.forEach((file, index) => {
      if ('refused' in file) refusals.add(file.refused);
      else files.push(new File(parts[index] as BlobPart[], file.name, { type: file.type }));
    });
    if (share.kind === 'unreadable') refusals.add('unreadable');
    // A share is text or files, never both, so a refusal here adds nothing.
    if (files.length && !await until(() => vibey()?.addFiles(files), READY_TIMEOUT)) return 'unavailable';
    for (const refusal of refusals) deps.toast(REFUSAL_COPY[refusal]);
    return 'done';
  }

  return {
    version: 1,
    document: deps.document ?? crypto.randomUUID(),
    begin: (raw) => {
      const share = parse(raw);
      if (!share) return 'invalid';
      // A repeated begin after commit (native lost the status) answers where
      // the share is, so it is never inserted twice.
      const current = shares.get(share.id);
      if (current && current.status !== 'receiving') return current.status;
      shares.set(share.id, { share, parts: share.files.map(() => []), received: share.files.map(() => 0), status: 'receiving' });
      return 'ok';
    },
    chunk: (id, index, data) => {
      const entry = shares.get(id);
      const facts = entry?.share.files[index];
      if (!entry || entry.status !== 'receiving' || !facts || 'refused' in facts) return 'invalid';
      const bytes = decode(data);
      if (!bytes || entry.received[index] + bytes.length > facts.size) return 'invalid';
      entry.parts[index].push(bytes);
      entry.received[index] += bytes.length;
      return 'ok';
    },
    commit: (id) => {
      const entry = shares.get(id);
      if (!entry) return 'invalid';
      if (entry.status !== 'receiving') return 'ok';
      const complete = entry.share.files.every((file, index) => 'refused' in file || entry.received[index] === file.size);
      if (!complete) return 'invalid';
      entry.status = 'inserting';
      queue = queue.then(async () => {
        entry.status = await insert(entry.share, entry.parts).catch(() => 'unavailable' as const);
        entry.parts = [];
      });
      return 'ok';
    },
    status: (id) => shares.get(id)?.status ?? 'unknown',
  };
}
