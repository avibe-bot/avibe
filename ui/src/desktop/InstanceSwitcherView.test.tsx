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

const studio = item('studio', true);
const committed = { kind: 'remote' as const, name: 'studio', instance_id: 'studio', canonical_origin: studio.url };
const onRemote = (status: 'remote_connected' | 'remote_unreachable', instances: Instance[], current: Instance | null = studio) =>
  base(instances, { connection: { status, committed, pending: null }, current_instance: current });
const dotLabel = (id: string) => document.querySelector(`[data-instance-id="${id}"] [data-status-indicator]`)?.getAttribute('aria-label') ?? null;
const rowText = (id: string) => document.querySelector(`[data-instance-id="${id}"]`)!.textContent!;

it('reads other rows from the catalog: green reachable, grey offline, gold only for the row being opened', () => {
  view(base([item('local', true), item('studio', true), item('weekend', false), item('team', true, { relationship: 'shared' }), item('lab', false, { relationship: 'shared' })]), { pendingId: 'studio' });
  expect(tone('studio')).toBe('connecting');
  expect(tone('weekend')).toBe('stopped');
  expect(tone('team')).toBe('running');
  expect(dotLabel('team')).toBe(en.desktopClient.online);
  expect(tone('lab')).toBe('stopped');
  const opening = document.querySelector('[data-instance-id="studio"]')!;
  expect(opening.querySelector('[role="status"]')!.className).toContain('text-gold-ink');
  expect(opening.querySelector('svg.animate-spin')!.getAttribute('class')).toContain('text-gold-ink');
});

// Max 2026-11-19 09:26: the current row follows the connection, as the sidebar's bottom row does.
it.each([['listed', [item('local', false), studio]], ['unlisted', [studio]]] as const)(
  'draws the current local row green from the connection with this computer %s in the catalog', (_, instances) => {
    view(base([...instances]));
    expect(tone('local')).toBe('running');
    expect(dotLabel('local')).toBe(en.desktopClient.connected);
  });

it('draws the current row gold while switching, labelled as the bottom row is', () => {
  view(base([item('local', true), studio], { connection: { status: 'switching', committed: { kind: 'local', name: 'max', instance_id: null }, pending: committed } }), { pendingId: 'studio' });
  expect(tone('local')).toBe('connecting');
  expect(dotLabel('local')).toBe(en.desktopClient.connecting);
  expect(tone('studio')).toBe('connecting');
});

it.each([['unavailable', [item('local', false), studio]], ['unlisted', [studio]]] as const)(
  'leaves this computer among the other rows without a dot when the catalog has it %s, keeping it openable', (_, instances) => {
    view(onRemote('remote_connected', [...instances]));
    expect(tone('local')).toBeNull();
    const button = document.querySelector('[data-instance-id="local"] button') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.textContent).not.toContain(en.desktopClient.offline);
  });

it('greys an unreachable current remote', () => {
  view(onRemote('remote_unreachable', [item('local', true), studio]));
  expect(tone('studio')).toBe('stopped');
  expect(dotLabel('studio')).toBe(en.desktopClient.disconnected);
  expect(rowText('studio')).toContain(en.desktopClient.offline);
});

it.each([
  ['dropped from the catalog', [item('local', true)], { ...studio, available: false }],
  ['reported unavailable by the catalog', [item('local', true), item('studio', false)], studio],
  ['without a native identity', [item('local', true)], null],
] as const)('keeps a connected current remote green and not offline when it is %s', (_, instances, current) => {
  view(onRemote('remote_connected', [...instances], current));
  expect(tone('studio')).toBe('running');
  expect(dotLabel('studio')).toBe(en.desktopClient.connected);
  expect(rowText('studio')).not.toContain(en.desktopClient.offline);
  expect(screen.queryAllByRole('img', { name: en.desktopClient.online }).filter((node) => node.closest('[data-instance-id="studio"]'))).toHaveLength(0);
  expect((document.querySelector('[data-instance-id="studio"] button') as HTMLButtonElement).disabled).toBe(false);
});

it('keeps a reachable row green after a failed open and announces each dot once', () => {
  view(base([item('local', true), item('studio', true)]), { failure: { instanceId: 'studio', message: 'Could not open' } });
  expect(tone('studio')).toBe('running');
  expect(screen.getAllByRole('img', { name: en.desktopClient.online })).toHaveLength(1);
  expect(screen.getAllByRole('img', { name: en.desktopClient.connected })).toHaveLength(1);
  const local = document.querySelector('[data-instance-id="local"] button')!;
  expect(local.hasAttribute('aria-describedby')).toBe(false);
  expect(local.textContent).not.toContain(en.desktopClient.connected);
});
