// Folds runs of Harness input in a chat transcript into one group, so a stretch
// of Watch results, scheduled runs and Agent callbacks the Agent answered
// silently reads as one row instead of a column of blue chips.
// docs/plans/workbench-refinements.md §3 owns the rules; this module is their
// one implementation, pure so they are tested without the page.
//
// Grouping looks at consecutive transcript rows only. A Turn that did not reply
// writes no row: its Activity chip anchors AFTER its trigger, which is a member
// of the run, so it travels with that member and never separates two members.
// Everything that does draw a row (the user, the Agent in any form, an
// annotation) sits between members in this list and so ends the run.
import type { WorkbenchMessage } from '../context/ApiContext';
import { chatRowKind } from './chatRowKind';

type Message = Pick<WorkbenchMessage, 'id' | 'type' | 'author' | 'source' | 'content' | 'metadata'>;

export type TranscriptItem<M extends Message = WorkbenchMessage> =
  | { kind: 'message'; message: M; index: number }
  | { kind: 'harnessGroup'; members: { message: M; index: number }[] };

/**
 * ``index`` is each row's position in ``messages``, kept because callers key
 * per-row decisions (Retry eligibility) on it.
 *
 * The Session's first message never joins a group: in a delegated Session it is
 * the task brief, the one Harness row that says what the Session is for. It is
 * only known to be first while no older page is left (``hasOlder`` false).
 *
 * ``hideHarness`` drops every Harness row instead, the first included: a Bot's
 * conversation with "Hide details" on (docs/plans/bots.md D5) shows the Bot's
 * replies to that input, never the input itself.
 */
export function groupHarnessRuns<M extends Message>(
  messages: readonly M[],
  { hasOlder, hideHarness = false }: { hasOlder: boolean; hideHarness?: boolean },
): TranscriptItem<M>[] {
  const items: TranscriptItem<M>[] = [];
  let run: { message: M; index: number }[] = [];
  const flush = () => {
    if (run.length >= 2) items.push({ kind: 'harnessGroup', members: run });
    else for (const member of run) items.push({ kind: 'message', ...member });
    run = [];
  };
  messages.forEach((message, index) => {
    const harness = chatRowKind(message).kind === 'harness';
    if (harness && hideHarness) return;
    const opening = index === 0 && !hasOlder;
    if (!opening && harness) {
      run.push({ message, index });
      return;
    }
    flush();
    items.push({ kind: 'message', message, index });
  });
  flush();
  return items;
}

/**
 * React keys for groups that survive growth at either end. A group keyed by its
 * first member would remount when an older page adds members above it, and one
 * keyed by its last member would remount whenever a new message joins live; a
 * remount drops the element the transcript's scroll anchor holds. So a group
 * keeps the key any of its members carried last time, and only a group with no
 * known member takes a new one. Pure: the caller stores the returned map.
 */
export function stableHarnessGroupKeys<M extends Message>(
  items: readonly TranscriptItem<M>[],
  previous: ReadonlyMap<string, string>,
): Map<string, string> {
  const next = new Map<string, string>();
  const used = new Set<string>();
  for (const item of items) {
    if (item.kind !== 'harnessGroup') continue;
    const inherited = item.members
      .map(({ message }) => previous.get(message.id))
      .find((key): key is string => key !== undefined && !used.has(key));
    let key = inherited ?? `harness-group:${item.members[0].message.id}`;
    while (used.has(key)) key = `${key}+`;
    used.add(key);
    for (const { message } of item.members) next.set(message.id, key);
  }
  return next;
}
