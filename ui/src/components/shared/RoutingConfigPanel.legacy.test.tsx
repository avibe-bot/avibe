// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { VibeAgentBrief } from '../../context/ApiContext';

const pickerValues: unknown[] = [];
vi.mock('../workbench/AgentRoutePicker', () => ({
  AgentRoutePicker: ({ value }: { value: unknown }) => {
    pickerValues.push(value);
    return null;
  },
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_manage_access_members: false } }),
}));

import { RoutingConfigPanel } from './RoutingConfigPanel';

const agent = (name: string, backend: string, source: string): VibeAgentBrief => ({
  id: `id-${name}`, name, display_name: name, description: null, backend, model: `${name}-model`,
  reasoning_effort: null, enabled: true, archived: false, archived_at: null, source, updated_at: '',
});

describe('RoutingConfigPanel', () => {
  afterEach(() => { cleanup(); pickerValues.length = 0; });

  // Routing that holds a backend id means that backend's built-in Agent, which takes the
  // next free name (vibey-2) while a user's Agent holds the id.
  it('shows a backend-id route as that backend’s built-in Agent', () => {
    render(<RoutingConfigPanel
      value={{ custom_cwd: '', routing: { agent_name: 'vibey' }, show_message_types: [] }}
      onChange={() => {}} onBrowseDirectory={() => {}} globalConfig={{}}
      vibeAgents={[agent('vibey-2', 'vibey', 'builtin'), agent('claude', 'claude', 'builtin')]}
    />);
    expect(pickerValues.at(-1)).toMatchObject({ agent_backend: 'vibey', agent_name: 'vibey-2', model: 'vibey-2-model' });
  });

  it('prefers an Agent that really has the name', () => {
    render(<RoutingConfigPanel
      value={{ custom_cwd: '', routing: { agent_name: 'vibey' }, show_message_types: [] }}
      onChange={() => {}} onBrowseDirectory={() => {}} globalConfig={{}}
      vibeAgents={[agent('vibey-2', 'vibey', 'builtin'), agent('vibey', 'claude', 'custom')]}
    />);
    expect(pickerValues.at(-1)).toMatchObject({ agent_backend: 'claude', agent_name: 'vibey' });
  });
});
