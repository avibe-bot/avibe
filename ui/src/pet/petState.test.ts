import { describe, expect, it } from 'vitest';

import type { RunningAgent, RunningAgentsResult, WorkbenchMessage } from '@/context/ApiContext';

import {
  conversationAgentWorking,
  derivePetPose,
  derivePetState,
  latestExchange,
  openQuickReplies,
  type PetState,
  type PetStateInputs,
} from './petState';

let nextId = 0;
const row = (overrides: Partial<WorkbenchMessage>): WorkbenchMessage => ({
  id: `m${(nextId += 1)}`,
  scope_id: 'scope',
  session_id: 'S',
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: 'text',
  content: {},
  metadata: {},
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
  delivered_at: null,
  read_at: '2026-10-04T00:00:01Z',
  ...overrides,
});

const user = (text = 'hi') => row({ author: 'user', type: 'user', source: 'user', text });
const result = (overrides: Partial<WorkbenchMessage> = {}) => row(overrides);
const asking = (chosen?: string) => result({
  content: { quick_replies: ['Yes', 'No'], ...(chosen ? { quick_reply_chosen: chosen } : {}) },
});

const quiet: PetStateInputs = {
  session: { agent_status: 'idle' },
  messages: [user(), result()],
  turn: { foreground: 'idle', in_flight: false },
  pendingVaultRequests: 0,
  unreadCount: 0,
};

describe('derivePetState', () => {
  it.each<[string, Partial<PetStateInputs>, ReturnType<typeof derivePetState>]>([
    ['nothing is happening', {}, 'idle'],
    ['the agent is working', { turn: { foreground: 'running', in_flight: true } }, 'running'],
    ['a turn is in flight before the foreground flips', { turn: { foreground: 'idle', in_flight: true } }, 'running'],
    ['a reply is unread and the agent is idle', { unreadCount: 2 }, 'ready'],
    ['the last run failed', { session: { agent_status: 'failed' } }, 'blocked'],
    ['a vault request is pending', { pendingVaultRequests: 1 }, 'needs_input'],
    ['the latest result asks a quick-reply question', { messages: [user(), asking()] }, 'needs_input'],
  ])('is %s → %s', (_case, override, expected) => {
    expect(derivePetState({ ...quiet, ...override })).toBe(expected);
  });

  it('orders Needs input > Blocked > Ready > Running > Idle', () => {
    const everything: PetStateInputs = {
      session: { agent_status: 'failed' },
      messages: [user(), asking()],
      turn: { foreground: 'running', in_flight: true },
      pendingVaultRequests: 1,
      unreadCount: 3,
    };
    expect(derivePetState(everything)).toBe('needs_input');
    expect(derivePetState({ ...everything, messages: [user(), result()], pendingVaultRequests: 0 })).toBe('blocked');
    expect(derivePetState({
      ...everything, messages: [], pendingVaultRequests: 0, session: { agent_status: 'idle' }, turn: null,
    })).toBe('ready');
    // A running turn is not Ready even with unread replies: the reply is not final yet.
    expect(derivePetState({
      ...everything, messages: [], pendingVaultRequests: 0, session: { agent_status: 'idle' },
    })).toBe('running');
  });

  it('clears Needs input once the question is answered', () => {
    expect(derivePetState({ ...quiet, messages: [user(), asking('Yes')] })).toBe('idle');
  });

  it('treats a free-text reply after the group as its answer, while that turn runs', () => {
    const messages = [user(), asking(), user('free text')];
    expect(openQuickReplies(messages)).toBeNull();
    expect(derivePetState({ ...quiet, messages, turn: { foreground: 'running', in_flight: true } })).toBe('running');
    // Rows that are not the user's input do not answer it: a harness message,
    // or the user's own display-only Show Page annotation.
    expect(openQuickReplies([user(), asking(), row({ author: 'harness', type: 'harness' })])).not.toBeNull();
    expect(openQuickReplies([user(), asking(), row({ author: 'user', type: 'annotation', source: 'user' })])).not.toBeNull();
  });

  it('ignores an older unanswered group once a newer result exists', () => {
    const messages = [user(), asking(), user('free text'), result()];
    expect(openQuickReplies(messages)).toBeNull();
    expect(derivePetState({ ...quiet, messages })).toBe('idle');
  });
});

describe('latestExchange', () => {
  it('shows the last user message and the last result when nothing is unread', () => {
    const last = result({ text: 'latest' });
    const exchange = latestExchange([user('old'), result(), user('ask'), last], true);
    expect(exchange.user?.text).toBe('ask');
    expect(exchange.results).toEqual([last]);
    expect(exchange.unreadComplete).toBe(true);
  });

  it('shows every unread result, oldest first', () => {
    const first = result({ read_at: null, text: 'one' });
    const second = result({ read_at: null, text: 'two' });
    const exchange = latestExchange([result(), user(), first, second], true);
    expect(exchange.results).toEqual([first, second]);
    expect(exchange.unreadComplete).toBe(true);
  });

  it('is incomplete when unread results may reach past the loaded tail', () => {
    const tail = [result({ read_at: null }), user(), result({ read_at: null })];
    expect(latestExchange(tail, true).unreadComplete).toBe(false);
    // The tail already starts at the session's first row.
    expect(latestExchange(tail, false).unreadComplete).toBe(true);
  });

  it('is incomplete when the inbox count is ahead of the loaded unread results', () => {
    const visible = result({ read_at: null, text: 'visible' });
    expect(latestExchange([visible], true, 3).unreadComplete).toBe(false);
    expect(latestExchange([visible], true, 3).results).toEqual([visible]);
    // Inbox says unread, the loaded tail is all read: those replies sit past it.
    expect(latestExchange([result({ text: 'read' })], true, 2).unreadComplete).toBe(false);
    // A leftover count on a fully loaded session is stale.
    expect(latestExchange([result({ text: 'read' })], false, 2).unreadComplete).toBe(true);
  });

  it('leaves out process rows', () => {
    const exchange = latestExchange([user(), row({ type: 'tool_call' }), result({ text: 'done' })], false);
    expect(exchange.results.map((message) => message.text)).toEqual(['done']);
  });
});

const agent = (overrides: Partial<RunningAgent>): RunningAgent => ({
  backend: 'claude',
  state: 'active',
  base_session_id: 'S',
  composite_key: 'S:/w',
  workdir: '/w',
  pid: 1,
  pid_shared: false,
  native_session_id: null,
  model: null,
  elapsed_seconds: 1,
  session_id: 'S',
  title: 'S',
  platform: 'avibe',
  scope_type: 'project',
  scope_display_name: 'p',
  visibility: 'foreground',
  trigger_source: 'human',
  agent_name: 'claude',
  openable_in_chat: true,
  ...overrides,
});
const snapshot = (agents: RunningAgent[]): RunningAgentsResult => ({
  ok: true,
  agents,
  counts: { total: agents.length, active: 0, idle: 0, orphan: 0, by_backend: {} },
});

describe('conversationAgentWorking', () => {
  it('is true only for an active agent in a foreground conversation', () => {
    expect(conversationAgentWorking(snapshot([agent({})]))).toBe(true);
    expect(conversationAgentWorking(snapshot([agent({ state: 'idle' }), agent({ state: 'orphan' })]))).toBe(false);
    expect(conversationAgentWorking(snapshot([agent({ visibility: 'background' })]))).toBe(false);
    expect(conversationAgentWorking(snapshot([agent({ visibility: 'system' })]))).toBe(false);
    expect(conversationAgentWorking(snapshot([agent({ session_id: null, visibility: null })]))).toBe(false);
    expect(conversationAgentWorking(snapshot([agent({ state: 'idle' }), agent({ session_id: 'T' })]))).toBe(true);
  });

  it('is false when the Runtime cannot answer', () => {
    expect(conversationAgentWorking({ ok: false, unreachable: true, agents: [agent({})], counts: {} })).toBe(false);
  });
});

describe('derivePetPose', () => {
  const states: PetState[] = ['needs_input', 'blocked', 'ready', 'running', 'idle'];

  it('keeps every bound-session state other than idle, whatever else is working', () => {
    for (const state of states.filter((value) => value !== 'idle')) {
      for (const othersWorking of [false, true]) {
        for (const asleep of [false, true]) {
          expect(derivePetPose(state, { othersWorking, asleep })).toBe(state);
        }
      }
    }
  });

  it('shows another conversation\'s work over idle and sleep', () => {
    expect(derivePetPose('idle', { othersWorking: true, asleep: true })).toBe('running');
    expect(derivePetPose('idle', { othersWorking: true, asleep: false })).toBe('running');
    expect(derivePetPose('idle', { othersWorking: false, asleep: true })).toBe('sleeping');
    expect(derivePetPose('idle', { othersWorking: false, asleep: false })).toBe('idle');
  });
});
