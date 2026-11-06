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
// refuses every send).
import '../../design/shell/storage';

import { createRoot } from 'react-dom/client';

import '../../src/i18n';
import '../../src/index.css';
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import { setLocalSnapshotPrincipal } from '../../src/lib/localSnapshots';
import { DesignShell } from '../../design/shell/DesignShell';
import { apiResponses, demoProjects } from '../../design/shell/fixtures';
import { designNetwork, installDesignNetwork } from '../../design/shell/network';
import { chatFixtures, chatPhone } from '../../design/frames/chat/fixtures';

import { LAB_SESSION_ID, activityGroups, conversation, messageAt } from './conversation';
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

const history = conversation(count).map((message) =>
  images ? message : { ...message, text: message.text.replace(/!\[[^\]]*\]\([^)]*\)/g, '(chart)') },
);
const PAGE = 50;
const tail = history.slice(-PAGE);

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
  activity: activity ? activityGroups(history) : [],
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

let loads = 0;
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
const designFetch = window.fetch;
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.href);
  if (url.pathname === `${base}/bootstrap` && boot > 0) await new Promise((resolve) => setTimeout(resolve, boot));
  if (url.pathname === `${base}/messages` && (init?.method ?? 'GET').toUpperCase() === 'POST') {
    // A sent message: the server's row for it, or a refusal.
    await new Promise((resolve) => setTimeout(resolve, sendDelay));
    if (sendFails) return new Response(JSON.stringify({ error: 'The agent is not available.' }), { status: 503, headers: { 'content-type': 'application/json' } });
    const sent = JSON.parse(String(init?.body ?? '{}')) as { text?: string; metadata?: Record<string, unknown> };
    const row = { ...messageAt(history.length), author: 'user', type: 'user', source: 'user', author_id: 'local', id: `msg_sent_${history.length}`, text: sent.text ?? '', metadata: sent.metadata ?? {} } as WorkbenchMessage;
    history.push(row);
    return json(row);
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
      /** A turn starting or ending over the event stream. */
      turn(phase: 'start' | 'end'): void;
      /** A row the conversation already has, changed over the event stream (its text replaced). */
      update(id: string, text: string): WorkbenchMessage;
      history: WorkbenchMessage[];
    };
  }
}

if (reader) setLocalSnapshotPrincipal(reader);
const recorder = installScrollRecorder();
window.__scrollLab = Object.assign(recorder, {
  history,
  push(text?: string, author?: 'user' | 'agent') {
    const next = messageAt(history.length);
    const by = author === 'user'
      ? { author: 'user', type: 'user', source: 'user', author_id: 'local' }
      : author === 'agent' ? { author: 'agent', type: 'result', source: 'agent', author_id: null } : {};
    const row = { ...next, ...by, ...(text ? { text } : {}) } as WorkbenchMessage;
    history.push(row);
    for (const source of LabEventSource.live) {
      source.dispatchEvent(new MessageEvent('message.new', { data: JSON.stringify({ type: 'message.new', data: row }) }));
    }
    return row;
  },
  turn(phase: 'start' | 'end') {
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
    <DesignShell fixtures={fixtures} route={`/chat/${LAB_SESSION_ID}`}>
      <Phone />
    </DesignShell>
  </div>,
);
