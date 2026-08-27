/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { SettingsPageShell } from './SettingsPageShell';

afterEach(cleanup);

describe('SettingsPageShell', () => {
  it('leaves inline detail breadcrumbs to the desktop rail layout', () => {
    render(
      <SettingsPageShell
        activeTab="backends"
        title="Claude"
        subtitle="Provider settings"
        breadcrumb={<span>Backends</span>}
      >
        <div>body</div>
      </SettingsPageShell>,
    );

    expect(screen.getByText('Backends').parentElement?.className).toContain('hidden');
    expect(screen.getByText('Backends').parentElement?.className).toContain('md:block');
  });
});
