import { describe, expect, it } from 'vitest';

import { searchGraph } from './graphSearch';
import type { AgentGraphNode, AgentGraphTriggerNode } from './agentGraph';

// The matcher only reads session_id/title (nodes) and name/definition_id
// (triggers); minimal casts keep the fixtures readable.
const node = (over: Partial<AgentGraphNode>): AgentGraphNode =>
  ({ session_id: 'ses_x', title: null, agent_name: null, ...over }) as AgentGraphNode;
const trigger = (over: Partial<AgentGraphTriggerNode>): AgentGraphTriggerNode =>
  ({ definition_id: 'def_x', definition_type: 'scheduled', name: null, schedule_label: null, enabled: true, ...over }) as AgentGraphTriggerNode;

const ids = (rs: ReturnType<typeof searchGraph>) => rs.map((r) => r.id);

describe('searchGraph', () => {
  it('returns nothing for an empty/whitespace query', () => {
    expect(searchGraph('', [node({ session_id: 'ses_abc' })], [])).toEqual([]);
    expect(searchGraph('   ', [node({ session_id: 'ses_abc' })], [])).toEqual([]);
  });

  it('matches a session_id by prefix (case-insensitive)', () => {
    const rs = searchGraph('SES_AB', [node({ session_id: 'ses_abc123' })], []);
    expect(ids(rs)).toEqual(['ses_abc123']);
    expect(rs[0].rank).toBe(0);
  });

  it('matches a title by substring, case-insensitive', () => {
    const rs = searchGraph('draft', [node({ session_id: 'ses_1', title: 'Daily Draft PM' })], []);
    expect(ids(rs)).toEqual(['ses_1']);
    expect(rs[0].rank).toBe(1);
  });

  it('matches a session_id by mid-string substring (lower rank than prefix)', () => {
    const rs = searchGraph('abc', [node({ session_id: 'ses_xxabc' })], []);
    expect(ids(rs)).toEqual(['ses_xxabc']);
    expect(rs[0].rank).toBe(2);
  });

  it('excludes non-matches', () => {
    expect(searchGraph('zzz', [node({ session_id: 'ses_1', title: 'hello' })], [])).toEqual([]);
  });

  it('matches trigger chips by definition name', () => {
    const rs = searchGraph('daily', [], [trigger({ definition_id: 'def_1', name: 'Daily draft' })]);
    expect(rs).toHaveLength(1);
    expect(rs[0].kind).toBe('trigger');
    expect(rs[0].id).toBe('def_1');
  });

  it('ranks an id prefix ahead of a substring hit', () => {
    const rs = searchGraph(
      'ses_a',
      [
        node({ session_id: 'ses_zzz', title: 'ses_a appears in title' }), // title substring (1)
        node({ session_id: 'ses_abc' }), // id prefix (0)
      ],
      [],
    );
    expect(ids(rs)).toEqual(['ses_abc', 'ses_zzz']);
  });

  it('ranks title/name-substring above id-substring, nodes before triggers on a tie', () => {
    const nodes = [
      node({ session_id: 'ses_1', title: 'Backend lane' }), // title substring (1)
      node({ session_id: 'ses_2lane' }), // id substring (2)
    ];
    const triggers = [trigger({ definition_id: 'def_z', name: 'lane watch' })]; // name substring (1)
    const rs = searchGraph('lane', nodes, triggers);
    expect(ids(rs)).toEqual(['ses_1', 'def_z', 'ses_2lane']);
  });
});
