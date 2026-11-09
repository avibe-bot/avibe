// The shipped ChatPage, inside the shipped phone AppShell, on a long
// conversation served from memory: the bootstrap holds the newest 50 rows, and
// each older page arrives after `delay` ms, as the server pages it. A live row
// can be pushed over the event stream. No request leaves the page.
//
// Query: count (rows of history, 400), delay (older-page latency in ms, 300),
// activity=1 (execution-process chips), images=0 (no images), boot (bootstrap
// latency in ms, 0), reader=local (keep the local chat snapshot, so a reload
// paints the cached window first), inbox=1 (the Inbox lists the chat with its
// newest reply, so an uncached open draws the skeleton and that reply first),
// sendDelay (ms the server takes to answer a sent message, 150), send=fail (it
// refuses every send), records=1 (an execution-process group's rows load with the
// steps it took, else it has none), activityDelay (ms a group's rows take to load,
// 0), recordsRepeat (a group's rows are its steps that many times over, so they
// take long to render when they land, 1), msg (opens the chat at that message, as
// a link to it does).
import '../../design/shell/storage';

import { createRoot } from 'react-dom/client';

import '../../src/i18n';
import '../../src/index.css';
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import { setLocalSnapshotPrincipal } from '../../src/lib/localSnapshots';
import { DesignShell } from '../../design/shell/DesignShell';
import { apiResponses, demoProjects } from '../../design/shell/fixtures';
import { designNetwork, installDesignNetwork } from '../../design/shell/network';
import { chatFixtures, chatPhone, idleTurn, runningTurn } from '../../design/frames/chat/fixtures';

import { LAB_SESSION_ID, activityGroups, activityRecords, conversation, messageAt } from './conversation';
import { installScrollRecorder, type ScrollLab } from './recorder';

const params = new URLSearchParams(window.location.search);
const count = Number(params.get('count') ?? 400);
const delay = Number(params.get('delay') ?? 300);
const activity = params.get('activity') === '1';
const images = params.get('images') !== '0';
const boot = Number(params.get('boot') ?? 0);
const reader = params.get('reader');
const inbox = params.get('inbox') === '1';
const sendDelay = Number(params.get('sendDelay') ?? 150);
const sendFails = params.get('send') === 'fail';
const records = params.get('records') === '1';
const activityDelay = Number(params.get('activityDelay') ?? 0);
const recordsRepeat = Math.max(1, Number(params.get('recordsRepeat') ?? 1));

const history = conversation(count).map((message) =>
  images ? message : { ...message, text: message.text.replace(/!\[[^\]]*\]\([^)]*\)/g, '(chart)') },
);
const PAGE = 50;
const tail = history.slice(-PAGE);
const initialGroups = activity ? activityGroups(history, records) : [];

installDesignNetwork();

const base = `/api/sessions/${LAB_SESSION_ID}`;
const latest = history[history.length - 1];
const fixtures = chatFixtures({
  session: { id: LAB_SESSION_ID, title: 'Scroll lab' },
  extra: inbox ? {
    projects: demoProjects,
    inboxSessions: [{
      session_id: LAB_SESSION_ID,
      scope_id: latest.scope_id,
      project_id: 'proj_avibe',
      project_name: 'avibe',
      title: 'Scroll lab',
      last_activity_at: latest.created_at,
      last_message_author: latest.author === 'agent' ? 'agent' : 'user',
      replied: true,
      preview_text: latest.text,
      preview_at: latest.created_at,
      unread_count: 0,
      unread: false,
    }],
  } : undefined,
  messages: tail,
  ui: { show_agent_activity: activity },
  activity: initialGroups,
  // Each chart is a distinct image the page has not measured yet.
  api: Object.fromEntries(
    history.filter((m) => m.text.includes('/api/media/')).map((m) => {
      const token = /\/api\/media\/([\w-]+)/.exec(m.text)![1];
      return [`/api/media/${token}`, `/lab-media/${token}.svg`];
    }),
  ),
});
const responses = apiResponses(fixtures);
const bootstrap = responses[`${base}/bootstrap`] as Record<string, unknown>;
responses[`${base}/bootstrap`] = { ...bootstrap, next_before_id: history.length > PAGE ? tail[0].id : null };
designNetwork.setResponses(responses);

// The turn as the server has it: what `/turn-state` and the bootstrap report always agree with the turn
// events the lab sends (`turn`), as on a real server, so the page's own reconciliation (it reads
// `/turn-state` on an interval while the stream is not known to be connected) confirms a running turn instead
// of ending it.
function setTurnState(running: boolean) {
  const state = running ? runningTurn : idleTurn;
  responses[`${base}/turn-state`] = state;
  responses[`${base}/bootstrap`] = { ...(responses[`${base}/bootstrap`] as Record<string, unknown>), turn_state: state };
}
setTurnState(false);
/** How often the page has asked for `/turn-state` (each reconciliation asks once). */
let turnStateReads = 0;

let loads = 0;
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
const designFetch = window.fetch;
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.href);
  if (url.pathname === `${base}/bootstrap` && boot > 0) await new Promise((resolve) => setTimeout(resolve, boot));
  if (url.pathname === `${base}/turn-state`) turnStateReads += 1;
  if (url.pathname === `${base}/messages` && (init?.method ?? 'GET').toUpperCase() === 'POST') {
    // A sent message: the server's row for it, or a refusal.
    await new Promise((resolve) => setTimeout(resolve, sendDelay));
    if (sendFails) return new Response(JSON.stringify({ error: 'The agent is not available.' }), { status: 503, headers: { 'content-type': 'application/json' } });
    const sent = JSON.parse(String(init?.body ?? '{}')) as { text?: string; metadata?: Record<string, unknown> };
    const row = { ...messageAt(history.length), author: 'user', type: 'user', source: 'user', author_id: 'local', id: `msg_sent_${history.length}`, text: sent.text ?? '', metadata: sent.metadata ?? {} } as WorkbenchMessage;
    history.push(row);
    return json(row);
  }
  // One settled group's rows, read when its chip opens.
  if (url.pathname === `${base}/activity` && url.searchParams.has('group_id')) {
    const group = [...initialGroups, ...settledGroups].find(({ id }) => id === url.searchParams.get('group_id'));
    if (activityDelay > 0) await new Promise((resolve) => setTimeout(resolve, activityDelay));
    const rows = records ? Array.from({ length: recordsRepeat }, (_, round) => activityRecords(group!).map((row) => ({ ...row, id: `${row.id}_${round}` }))).flat() : [];
    return json(group ? { ...group, rows } : {});
  }
  // The settled execution-process groups, as the server has them now.
  if (url.pathname === `${base}/activity` && !url.searchParams.has('group_id')) {
    // As the server lists them: summaries, whose rows are read when a chip opens.
    return json({ groups: [...initialGroups, ...settledGroups].map(({ rows: _rows, ...summary }) => summary) });
  }
  if (url.pathname !== `${base}/messages`) return designFetch(input, init);
  const before = url.searchParams.get('before_id');
  const limit = Number(url.searchParams.get('limit') ?? PAGE);
  const end = before && !url.searchParams.has('tail') ? history.findIndex((m) => m.id === before) : history.length;
  const start = Math.max(0, end - limit);
  if (before) {
    loads += 1;
    document.body.dataset.loads = String(loads);
    await new Promise((resolve) => setTimeout(resolve, delay));
  }
  return json({ messages: history.slice(start, end), next_after_id: null, next_before_id: start > 0 ? history[start].id : null });
};

// The event stream: open, silent until the lab pushes a row.
class LabEventSource extends EventTarget {
  static live = new Set<LabEventSource>();
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readyState = 1;
  readonly withCredentials = false;
  onopen = null;
  onmessage = null;
  onerror = null;
  readonly url: string;
  constructor(url: string) {
    super();
    this.url = url;
    LabEventSource.live.add(this);
  }
  close() {
    this.readyState = 2;
    LabEventSource.live.delete(this);
  }
}
window.EventSource = LabEventSource as unknown as typeof EventSource;

declare global {
  interface Window {
    __scrollLab: ScrollLab & {
      /** A row over the event stream: the next one of the conversation, or one by `author`. */
      push(text?: string, author?: 'user' | 'agent'): WorkbenchMessage;
      /** A turn starting or ending over the event stream; `/turn-state` and the bootstrap report it from then on. */
      turn(phase: 'start' | 'end'): void;
      /** How often the page has asked for `/turn-state` so far. */
      turnStateReads(): number;
      /** A row the conversation already has, changed over the event stream (its text replaced). */
      update(id: string, text: string): WorkbenchMessage;
      /** Any row over the event stream: the next one of the conversation with `fields` over it. A live
       *  activity row (a tool call, the agent's narration) is not stored with the conversation. */
      arrive(fields: Partial<WorkbenchMessage>): WorkbenchMessage;
      /** A row the server stores while the stream misses it: only a read brings it. */
      miss(fields?: Partial<WorkbenchMessage>): WorkbenchMessage;
      /** The stream comes back after a gap (its `connected` frame): the page catches up with a read. */
      reconnect(): void;
      /** The turn that wrote `replyId` leaves a settled execution-process group above it. */
      settle(replyId: string): void;
      history: WorkbenchMessage[];
    };
  }
}

if (reader) setLocalSnapshotPrincipal(reader);
const recorder = installScrollRecorder();
const settledGroups: ReturnType<typeof activityGroups> = [];
let activityRows = 0;
let subscription = 1;
const deliver = (row: WorkbenchMessage) => {
  for (const source of LabEventSource.live) {
    source.dispatchEvent(new MessageEvent('message.new', { data: JSON.stringify({ type: 'message.new', data: row }) }));
  }
};
window.__scrollLab = Object.assign(recorder, {
  history,
  push(text?: string, author?: 'user' | 'agent') {
    const next = messageAt(history.length);
    const by = author === 'user'
      ? { author: 'user', type: 'user', source: 'user', author_id: 'local' }
      : author === 'agent' ? { author: 'agent', type: 'result', source: 'agent', author_id: null } : {};
    const row = { ...next, ...by, ...(text ? { text } : {}) } as WorkbenchMessage;
    history.push(row);
    deliver(row);
    return row;
  },
  arrive(fields: Partial<WorkbenchMessage>) {
    const transient = fields.type === 'tool_call' || fields.type === 'assistant';
    const row = { ...messageAt(history.length), ...(transient ? { id: `act_${(activityRows += 1)}` } : {}), ...fields } as WorkbenchMessage;
    if (!transient) history.push(row);
    deliver(row);
    return row;
  },
  miss(fields: Partial<WorkbenchMessage> = {}) {
    const row = { ...messageAt(history.length), ...fields } as WorkbenchMessage;
    history.push(row);
    return row;
  },
  reconnect() {
    subscription += 1;
    for (const source of LabEventSource.live) {
      source.dispatchEvent(new MessageEvent('connected', { data: JSON.stringify({ sub_id: subscription }) }));
    }
  },
  settle(replyId: string) {
    const reply = history.find((row) => row.id === replyId)!;
    settledGroups.push({
      id: `grp_live_${replyId}`,
      anchor_message_id: replyId,
      anchor_position: 'before',
      open: false,
      status: 'done',
      steps: 2,
      duration_ms: 9_000,
      started_at: reply.created_at,
      rows: [],
    } as (typeof settledGroups)[number]);
  },
  turnStateReads: () => turnStateReads,
  turn(phase: 'start' | 'end') {
    // The server's state changes first, then it says so.
    setTurnState(phase === 'start');
    for (const source of LabEventSource.live) {
      source.dispatchEvent(new MessageEvent(`turn.${phase}`, { data: JSON.stringify({ type: `turn.${phase}`, data: { session_id: LAB_SESSION_ID } }) }));
    }
  },
  update(id: string, text: string) {
    const index = history.findIndex((row) => row.id === id);
    const row = { ...history[index], text } as WorkbenchMessage;
    history[index] = row;
    for (const source of LabEventSource.live) {
      source.dispatchEvent(new MessageEvent('message.updated', { data: JSON.stringify({ type: 'message.updated', data: row }) }));
    }
    return row;
  },
});

// record=1: record from the first frame, to see how the chat opens.
if (params.get('record') === '1') recorder.start();

const Phone = chatPhone;
createRoot(document.getElementById('root')!).render(
  <div className="h-dvh">
    <DesignShell fixtures={fixtures} route={`/chat/${LAB_SESSION_ID}${params.get('msg') ? `?msg=${params.get('msg')}` : ''}`}>
      <Phone />
    </DesignShell>
  </div>,
);
