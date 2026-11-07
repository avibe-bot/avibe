// The disclosure lab: the shipped ChatPage, inside the shipped phone AppShell, on a conversation built
// to hold every disclosure the transcript has (lab/disclosure/data.ts), served from memory as the server
// pages it. A floating panel switches the expand variant (src/lib/transcriptMotion, src/index.css: LAB),
// light and dark, reduced motion, and slow motion, and brings live events: a reply arriving, a turn
// running. Nothing leaves the page; no Avibe backend, credentials, or persisted app state are used.
import { applyAll, applyReducedStyles, applySlow, applyTheme, applyVariant, loadPrefs, savePrefs, setReduced, type LabPrefs } from './prefs';
import '../../design/shell/storage';

import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

import '../../src/i18n';
import '../../src/index.css';
import type { WorkbenchMessage } from '../../src/context/ApiContext';
import type { TurnActivityGroupWire } from '../../src/lib/agentActivity';
import { DesignShell } from '../../design/shell/DesignShell';
import { apiResponses } from '../../design/shell/fixtures';
import { designNetwork, installDesignNetwork } from '../../design/shell/network';
import { chatFixtures, chatPhone, idleTurn, runningTurn } from '../../design/frames/chat/fixtures';

import { LAB_SESSION_ID, labAgent, labConversation, labUser, liveStep, runningRows } from './data';
import { Panel } from './Panel';

const Phone = chatPhone;
const { history, groups, runningAsk } = labConversation();
const PAGE = 50;
const tail = history.slice(-PAGE);

// The running turn at the tail, as the server lists it.
const startedAt = Date.now() - 24_000;
let running: TurnActivityGroupWire | null = {
  id: 'grp_lab_running',
  anchor_message_id: runningAsk.id,
  anchor_position: 'after',
  open: true,
  status: 'running',
  steps: 4,
  duration_ms: null,
  started_at: new Date(startedAt).toISOString(),
  rows: runningRows(Date.now()),
};
const settled: TurnActivityGroupWire[] = [...groups];
let turnState = runningTurn;

installDesignNetwork();
const base = `/api/sessions/${LAB_SESSION_ID}`;
const fixtures = chatFixtures({
  session: { id: LAB_SESSION_ID, title: '展开动效 Lab' },
  messages: tail,
  ui: { show_agent_activity: true },
  turnState,
  activity: [...settled, running],
});
const responses = apiResponses(fixtures);
const bootstrap = responses[`${base}/bootstrap`] as Record<string, unknown>;
responses[`${base}/bootstrap`] = { ...bootstrap, next_before_id: history.length > PAGE ? tail[0].id : null };
designNetwork.setResponses(responses);

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
const designFetch = window.fetch;
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.href);
  const method = (init?.method ?? 'GET').toUpperCase();
  if (url.pathname === `${base}/messages` && method === 'POST') {
    // A message the reader sends: the server's row for it.
    await new Promise((resolve) => setTimeout(resolve, 150));
    const sent = JSON.parse(String(init?.body ?? '{}')) as { text?: string; metadata?: Record<string, unknown> };
    return json(append({ ...labUser(sent.text ?? ''), metadata: sent.metadata ?? {} }));
  }
  if (url.pathname === `${base}/turn-state`) return json(turnState);
  // The execution-process groups as the server has them now, with their rows: each opens at once.
  if (url.pathname === `${base}/activity`) {
    const all = running ? [...settled, running] : settled;
    const id = url.searchParams.get('group_id');
    if (id) return json(all.find((group) => group.id === id) ?? {});
    return json({ groups: all });
  }
  if (url.pathname !== `${base}/messages` || method !== 'GET') return designFetch(input, init);
  const before = url.searchParams.get('before_id');
  const limit = Number(url.searchParams.get('limit') ?? PAGE);
  const end = before && !url.searchParams.has('tail') ? history.findIndex((m) => m.id === before) : history.length;
  const start = Math.max(0, end - limit);
  if (before) await new Promise((resolve) => setTimeout(resolve, 300));
  return json({ messages: history.slice(start, end), next_after_id: null, next_before_id: start > 0 ? history[start].id : null });
};

// The event stream: open, silent until the lab sends something.
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
const send = (type: string, data: unknown) => {
  for (const source of LabEventSource.live) source.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ type, data }) }));
};

let seq = history.length;
/** A row the server stores now. */
function append(fields: Partial<WorkbenchMessage>): WorkbenchMessage {
  seq += 1;
  const at = new Date().toISOString();
  const row = { ...history[history.length - 1], id: `msg_lab_live_${seq}`, created_at: at, updated_at: at, delivered_at: at, read_at: at, metadata: {}, content: {}, ...fields } as WorkbenchMessage;
  history.push(row);
  return row;
}

let liveRows = 0;
let streaming: number | null = null;
const replies = [
  '好了，第二批也开始了，覆盖 5% 的用户。',
  '已经推上去了，CI 在跑。',
  '这一轮的结果：\n\n| 检查 | 结果 |\n| --- | --- |\n| 单元测试 | 1841 通过 |\n| 构建 | 通过 |\n| 包体积 | 3.4MB |',
  '收到，按这个来。',
];
let replyCount = 0;

/** The running turn ends: its reply comes in, and its execution process settles into a chip above it. */
function settle(reply?: string) {
  if (streaming !== null) window.clearInterval(streaming);
  streaming = null;
  const row = append(labAgent(reply ?? replies[replyCount++ % replies.length]));
  if (running) {
    settled.push({ ...running, anchor_message_id: row.id, anchor_position: 'before', open: false, status: 'done', duration_ms: Date.now() - Date.parse(running.started_at ?? row.created_at) });
    running = null;
  }
  turnState = idleTurn;
  send('message.new', row);
  send('turn.end', { session_id: LAB_SESSION_ID });
}

/** A new turn: the reader's ask, then the agent's steps arriving one by one until it is ended. */
function run() {
  const ask = append(labUser('再试一次：展开一个执行过程，然后收起来。'));
  send('message.new', ask);
  turnState = runningTurn;
  running = { id: `grp_lab_live_${seq}`, anchor_message_id: ask.id, anchor_position: 'after', open: true, status: 'running', steps: 0, duration_ms: null, started_at: new Date().toISOString(), rows: [] };
  window.setTimeout(() => send('turn.start', { session_id: LAB_SESSION_ID }), 300);
  let steps = 0;
  streaming = window.setInterval(() => {
    if (steps >= 6) {
      if (streaming !== null) window.clearInterval(streaming);
      streaming = null;
      return;
    }
    liveRows += 1;
    const step = { ...history[history.length - 1], id: `act_lab_${liveRows}`, created_at: new Date().toISOString(), ...liveStep(steps) } as WorkbenchMessage;
    running?.rows?.push({ id: step.id, kind: step.type === 'tool_call' ? 'tool_call' : 'assistant', text: step.text ?? '', created_at: step.created_at });
    if (running) running.steps = running.rows?.length ?? 0;
    send('message.new', step);
    steps += 1;
  }, 1400);
}

function Lab() {
  const [prefs, setPrefs] = useState<LabPrefs>(loadPrefs);
  const [isRunning, setRunning] = useState(running !== null);
  useEffect(() => {
    // The stylesheet is on the page now: its reduced-motion rules and duration tokens follow the switches.
    applyAll(prefs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const change = (next: Partial<LabPrefs>) => {
    const merged = { ...prefs, ...next };
    setPrefs(merged);
    savePrefs(merged);
    if (next.theme) applyTheme(merged.theme);
    if (next.variant) applyVariant(merged.variant);
    if (next.reduced !== undefined) setReduced(merged.reduced);
    if (next.variant || next.slow !== undefined) applySlow(merged.slow);
  };
  return (
    <>
      <div className="h-dvh">
        <DesignShell fixtures={fixtures} route={`/chat/${LAB_SESSION_ID}`} theme={prefs.theme}>
          <Phone />
        </DesignShell>
      </div>
      <Panel
        prefs={prefs}
        onChange={change}
        running={isRunning}
        onReply={() => {
          if (running) settle();
          else send('message.new', append(labAgent(replies[replyCount++ % replies.length])));
          setRunning(false);
        }}
        onRun={() => {
          run();
          setRunning(true);
        }}
        onSettle={() => {
          settle();
          setRunning(false);
        }}
      />
    </>
  );
}

// Before the first paint: the saved theme and variant (the stylesheet's rules follow once it is in).
const initial = loadPrefs();
applyTheme(initial.theme);
applyVariant(initial.variant);
applyReducedStyles();
createRoot(document.getElementById('root')!).render(<Lab />);
