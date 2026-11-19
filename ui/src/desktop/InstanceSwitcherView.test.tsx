// @vitest-environment jsdom
// ui/design: desktop/status-dots/*; desktop/instances/instances-*, switching-*, offline-*.
import { cleanup, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it } from 'vitest';
import en from '../i18n/en.json';
import states from './__fixtures__/instance-client-v1/states.json';
import { InstanceSwitcherView, type InstanceSwitcherViewProps } from './InstanceSwitcherView';
import type { DesktopClientState, Instance } from './bridge';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
afterEach(cleanup);

const item = (id: string, available: boolean, extra: Partial<Instance> = {}): Instance => ({
  id, name: id, host: `${id}.avibe.bot`, url: `https://${id}.avibe.bot`, relationship: 'owner', owner_email: null, available, ...extra,
});

const base = (instances: Instance[], patch: Partial<DesktopClientState> = {}): DesktopClientState => ({
  ...(states.signed_in as unknown as DesktopClientState),
  local: { ...states.signed_in.local, slug: 'max', instance_id: 'local' } as DesktopClientState['local'],
  catalog: { status: 'ready', instances },
  ...patch,
});

function view(state: DesktopClientState, extra: Partial<InstanceSwitcherViewProps> = {}) {
  const noop = () => {};
  render(<I18nextProvider i18n={i18n}><InstanceSwitcherView state={state} localInstanceId="local" busy={false} pendingId={null} failure={null}
    onSelect={noop} onLocal={noop} onCancel={noop} onRefresh={noop} onConsent={noop} onAccount={noop} onAdd={noop} {...extra} /></I18nextProvider>);
}

/** The tone each row's dot reads as, or null when the column is empty. */
function tone(id: string) {
  const row = document.querySelector(`[data-instance-id="${id}"]`)!;
  const column = row.querySelector('[data-line-glyph]');
  expect(column).toBeTruthy();
  const dot = column!.querySelector('[data-status-indicator]');
  if (!dot) return null;
  return dot.classList.contains('bg-mint') ? 'running' : dot.classList.contains('bg-gold') ? 'connecting' : dot.classList.contains('bg-muted') ? 'stopped' : 'unknown';
}

it('reads green for reachable, grey for offline, gold only for the row being opened', () => {
  view(base([item('local', true), item('studio', true), item('weekend', false), item('team', true, { relationship: 'shared' }), item('lab', false, { relationship: 'shared' })]), { pendingId: 'studio' });
  expect(tone('local')).toBe('running');
  expect(tone('studio')).toBe('connecting');
  expect(tone('weekend')).toBe('stopped');
  expect(tone('team')).toBe('running');
  expect(tone('lab')).toBe('stopped');
  const opening = document.querySelector('[data-instance-id="studio"]')!;
  expect(opening.querySelector('[role="status"]')!.className).toContain('text-gold-ink');
  expect(opening.querySelector('svg.animate-spin')!.getAttribute('class')).toContain('text-gold-ink');
});

it.each([['unavailable', [item('local', false), item('studio', true)]], ['unlisted', [item('studio', true)]]] as const)(
  'leaves this computer without a dot when the catalog has it %s, keeping it openable', (_, instances) => {
    view(base([...instances]));
    expect(tone('local')).toBeNull();
    const button = document.querySelector('[data-instance-id="local"] button') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.textContent).not.toContain(en.desktopClient.offline);
  });

it('greys an unreachable current remote and draws nothing for a current remote the catalog does not list', () => {
  const studio = item('studio', true);
  const committed = { kind: 'remote' as const, name: 'studio', instance_id: 'studio', canonical_origin: studio.url };
  view(base([item('local', true), studio], { connection: { status: 'remote_unreachable', committed, pending: null }, current_instance: studio }));
  expect(tone('studio')).toBe('stopped');
  cleanup();
  view(base([item('local', true)], { connection: { status: 'remote_connected', committed, pending: null }, current_instance: null }));
  expect(tone('studio')).toBeNull();
});

it('keeps a reachable row green after a failed open and announces Online once', () => {
  view(base([item('local', true), item('studio', true)]), { failure: { instanceId: 'studio', message: 'Could not open' } });
  expect(tone('studio')).toBe('running');
  expect(screen.getAllByRole('img', { name: en.desktopClient.online })).toHaveLength(2);
  const local = document.querySelector('[data-instance-id="local"] button')!;
  expect(local.hasAttribute('aria-describedby')).toBe(false);
  expect(local.textContent!.match(new RegExp(en.desktopClient.online, 'g'))).toBeNull();
});
