// Why this page has not moved onto the App's direct path, told to the
// instance in one request header (`X-Avibe-Page`), so that the instance's
// own line a minute for each session says it too
// (docs/plans/remote-direct-ios.md, the hand-over). A fixed word, and nothing
// else: no address, identifier or time.
//
// Nothing is sent for it alone. The word rides one request only, the
// page's own read of `/api/session` (`apiFetch`): the read that begins
// every connect of the event stream and follows every gap in it. The
// instance always answers that read itself, and with the word it is made
// same-origin, so the header can never be carried to another origin. So
// the instance hears the word at every return, reconnect and hand-over,
// as the state the page stands in then; not every minute. It is sent when
// the word changes, and otherwise at most once in PAGE_REPORT_REPEAT_MS.
// Only a page that can hand over has a word at all (the phone App and the
// desktop app with `direct` version 2); anywhere else nothing is sent.
//
// How long a return held the page's requests (lib/native, the hold on
// return) rides the same header once, as `<word>;hold=<ms>`, on the first
// such read after the hold, whether or not the word is due.

/** The page's present reason, in the instance's words. */
export type PageWhy =
  // The App has not answered the page's first question yet.
  | 'not_ready'
  // The App does not prefer the direct path, or is not on a direct address.
  | 'no_path'
  // The stream and the latest request both arrive directly.
  | 'on_path'
  // Waiting for a quiet page, and what keeps it busy.
  | 'quiet_request'
  | 'quiet_socket'
  | 'quiet_frame'
  // The page has not been told how it arrives: the last hand-over's stream,
  // or a new page's first one, has not said yet.
  | 'verdict'
  // The last hand-over closed nothing and kept nothing, and the page still
  // arrives another way: its connections are not ones the App can close.
  | 'not_ours'
  // The bounds on how many hand-overs are reached.
  | 'cap'
  // Too soon after the last hand-over.
  | 'spacing'
  // The page arrives another way, a hand-over is wanted, nothing above holds
  // it, and none is under way. It should never last.
  | 'idle'
  // The page has asked the App for a hand-over and waits for its answer.
  | 'asked';

export const PAGE_REPORT_HEADER = 'X-Avibe-Page';
const PAGE_REPORT_REPEAT_MS = 10_000;

let read: (() => PageWhy | null) | null = null;
let sent: PageWhy | null = null;
let sentAt = -Infinity;
let hold: number | null = null;
/** The longest hold the instance takes; a longer one is sent as this. */
const PAGE_REPORT_HOLD_MAX_MS = 1_000;

/**
 * `reader` gives the page's present reason, or null when it has none to
 * give. One reader at a time; the returned function removes it.
 */
export function setPageWhyReader(reader: () => PageWhy | null): () => void {
  read = reader;
  return () => {
    if (read !== reader) return;
    read = null;
    sent = null;
    sentAt = -Infinity;
    hold = null;
  };
}

/**
 * A return held this page's requests for `ms`. Said once, with the next
 * report; of two holds before one, the later.
 */
export function notePageHold(ms: number): void {
  if (!read) return;
  hold = Math.min(Math.max(Math.round(ms), 0), PAGE_REPORT_HOLD_MAX_MS);
}

/**
 * The header's value for a request that leaves now, or null when nothing is
 * due. Taking it counts as sending it.
 */
export function takePageReport(): string | null {
  const word = read ? read() : null;
  if (word === null) return null;
  if (hold === null && word === sent && Date.now() - sentAt < PAGE_REPORT_REPEAT_MS) return null;
  const value = hold === null ? word : `${word};hold=${hold}`;
  hold = null;
  sent = word;
  sentAt = Date.now();
  return value;
}
