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
  it('shows the Harness tab to a local owner', () => {
    const markup = renderWith(localOwner);
    expect(markup).toContain('workbench.modules.harness.title');
  });

  it('shows the Harness tab for an Editor', () => {
    const remoteOwner: InstanceAuthorizationValue = {
      ...localOwner,
      remote: true,
      instanceRole: 'editor',
      capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_manage_instance: false },
    };
    const markup = renderWith(remoteOwner);
    expect(markup).toContain('workbench.modules.agents.title');
    expect(markup).toContain('workbench.modules.harness.title');
  });
});
