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

  // The landing block is opt-in precisely so the fifteen sections that never ask
  // for it keep the heading they shipped with.
  it('keeps the section heading for callers that do not ask for the landing block', () => {
    render(
      <SettingsPageShell activeTab="service" title="Service" subtitle="Run state">
        <div>body</div>
      </SettingsPageShell>,
    );

    const heading = screen.getByRole('heading', { name: 'Service' });
    expect(heading.className).toContain('text-[28px]');
    expect(heading.className).toContain('font-bold');
    expect(screen.getByText('Run state').className).toContain('text-[14px]');
  });

  it('draws the source landing block when asked for it', () => {
    render(
      <SettingsPageShell activeTab="general" title="General" subtitle="Language and appearance" titleScale="landing">
        <div>body</div>
      </SettingsPageShell>,
    );

    // design.pen Ozmrf: 27/600 over a 13 muted line.
    const heading = screen.getByRole('heading', { name: 'General' });
    expect(heading.className).toContain('text-[27px]');
    expect(heading.className).toContain('font-semibold');
    expect(screen.getByText('Language and appearance').className).toContain('text-[13px]');
  });
});
