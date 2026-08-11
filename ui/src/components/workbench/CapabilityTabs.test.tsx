import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { CapabilityTabs } from './CapabilityTabs';
import {
  InstanceAuthorizationContext,
  type InstanceAuthorizationValue,
} from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';

function renderWith(context: InstanceAuthorizationValue, path = '/agents') {
  return renderToStaticMarkup(
    <InstanceAuthorizationContext.Provider value={context}>
      <MemoryRouter initialEntries={[path]}>
        <CapabilityTabs />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

const localOwner: InstanceAuthorizationValue = {
  remote: false,
  instanceRole: 'owner',
  capabilities: OWNER_INSTANCE_CAPABILITIES,
};

describe('CapabilityTabs', () => {
  it('shows the Harness tab to a trusted-local owner', () => {
    const markup = renderWith(localOwner);
    expect(markup).toContain('workbench.modules.harness.title');
  });

  it('hides the Harness tab from a remote owner even though can_manage_agents is true', () => {
    // A remote Instance owner keeps can_manage_agents, which is exactly why the
    // Harness tab needs the trusted-local canUseHarness predicate on top of it:
    // without it the tab navigates into the AppShell local-only redirect.
    const remoteOwner: InstanceAuthorizationValue = {
      ...localOwner,
      remote: true,
    };
    const markup = renderWith(remoteOwner);
    expect(markup).toContain('workbench.modules.agents.title');
    expect(markup).not.toContain('workbench.modules.harness.title');
  });
});
