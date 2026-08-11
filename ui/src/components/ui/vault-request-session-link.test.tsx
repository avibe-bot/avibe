import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { VaultRequestSessionLink } from './vault-request-session-link';

const session = {
  id: 'session/123',
  label: 'Regression chat',
  isWorkbench: true,
  isIdFallback: false,
};

const render = (ui: ReactElement) => renderToStaticMarkup(<MemoryRouter>{ui}</MemoryRouter>);

describe('VaultRequestSessionLink', () => {
  it('preserves the existing chat route when no request is selected', () => {
    expect(render(<VaultRequestSessionLink session={session} />)).toContain('href="/chat/session%2F123"');
  });
});
