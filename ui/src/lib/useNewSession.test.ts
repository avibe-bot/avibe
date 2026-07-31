import { describe, expect, it } from 'vitest';

import type { VibeAgentBrief, WorkbenchProject } from '../context/ApiContext';
import { isProjectDefaultAgentAvailable, projectDefaultAgentRoute } from './useNewSession';

const project = (agentName: string): WorkbenchProject =>
  ({
    default_agent: { agent_name: agentName },
  }) as WorkbenchProject;

const agent = (over: Partial<VibeAgentBrief>): VibeAgentBrief =>
  ({
    id: 'agent-1',
    name: 'pm',
    display_name: 'pm',
    description: null,
    backend: 'claude',
    model: null,
    reasoning_effort: null,
    enabled: true,
    archived: false,
    archived_at: null,
    source: 'user',
    updated_at: '2026-08-01T00:00:00Z',
    ...over,
  }) as VibeAgentBrief;

describe('isProjectDefaultAgentAvailable', () => {
  it('accepts an enabled live project default', () => {
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({})])).toBe(true);
  });

  it('accepts a legacy normalized-equivalent project default', () => {
    expect(
      isProjectDefaultAgentAvailable(
        project('PROJECT-MANAGER'),
        [agent({ name: 'Project Manager' })],
      ),
    ).toBe(true);
  });

  it('rejects an archived internal project default that is absent from the live catalog', () => {
    expect(isProjectDefaultAgentAvailable(project('_pm-8dd7'), [agent({ name: 'codex' })])).toBe(false);
  });

  it('rejects disabled or archived catalog entries', () => {
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({ enabled: false })])).toBe(false);
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({ archived: true })])).toBe(false);
  });
});

describe('projectDefaultAgentRoute', () => {
  it('pins the project Agent by stable ID for session creation', () => {
    const target = project('pm');
    target.default_agent = {
      agent_backend: 'claude',
      agent_id: 'agent-original',
      agent_name: 'pm',
      agent_variant: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: 'high',
    };

    expect(projectDefaultAgentRoute(target, [agent({ id: 'agent-replacement' })])).toEqual({
      agent_name: 'pm',
      agent_id: 'agent-original',
      agent_variant: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: 'high',
    });
  });
});
