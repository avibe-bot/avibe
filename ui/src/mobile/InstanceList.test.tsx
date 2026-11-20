/* @vitest-environment jsdom */
// Exercise both consumers: waiting means another row is opening, so it must not
// turn an online or offline computer amber. Existing onboarding tests cover
// opening itself, but not the shared visual/accessibility status contract.
import { cleanup, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it } from 'vitest';
import en from '@/i18n/en.json';
import { InstanceSheetPage, InstancesPage } from './MobileOnboarding';
import type { MobileInstance, MobileSessionState } from './bridge';

const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const instance = (id: string, extra: Partial<MobileInstance> = {}): MobileInstance => ({
  id, name: id, host: `${id}.example.com`, url: `https://${id}.example.com`, available: true,
  relationship: 'owner', owner_email: null, ...extra,
});
const longName = 'A shared research workstation with a deliberately long readable name';
const session: Extract<MobileSessionState, { status: 'signedIn' }> = {
  status: 'signedIn', account: { id: 'account', email: 'max@example.com' }, selected_instance_id: 'current',
  instances: [instance('current'), instance('opening'), instance('online'), instance('offline', { available: false }),
    instance('shared', { name: longName, relationship: 'shared', owner_email: 'owner@example.com' })],
};
const noop = () => {};
const row = (name: string) => screen.getByText(name).closest('button')!;
const dot = (name: string) => row(name).querySelector('[data-status-indicator]')!;
afterEach(cleanup);

describe.each(['page', 'sheet'] as const)('instance dots in the real %s', (surface) => {
  const mount = (openingId: string | null = null, failure = false, stale = false) => {
    const common = { session, locked: Boolean(openingId), onRefresh: noop, onAdd: noop, onAccount: noop,
      rows: { openingId, expandedId: null, failure: failure ? { id: 'online', reason: 'mobileApp.sheet.openFailed' as const } : null, onOpen: noop } };
    return render(<I18nextProvider i18n={i18n}>{surface === 'page'
      ? <InstancesPage {...common} busy={null} errorKey={stale ? 'mobileApp.errors.network' : null} onReopen={noop} />
      : <InstanceSheetPage {...common} refreshing={false} stale={stale} onClose={noop} />
    }</I18nextProvider>);
  };
  it('keeps online/current/shared green and offline gray with meaningful accessible labels', () => {
    mount();
    for (const name of ['current', 'online', longName]) {
      expect(dot(name).className).toContain('bg-mint');
      expect(within(row(name)).getByRole('img', { name: 'Online' })).toBeTruthy();
      expect(row(name).getAttribute('aria-label') ?? row(name).textContent).toBeTruthy();
    }
    expect(screen.getByRole('button', { description: 'Online' })).toBe(row('current'));
    expect(dot('offline').className).toContain('bg-muted');
    expect(dot('offline').getAttribute('aria-hidden')).toBe('true');
    expect(row('offline').textContent).toContain('Offline');
    expect(row('offline').querySelector('.lucide-chevron-down')).toBeTruthy();
    expect(row(longName).querySelector('.lucide-users')).toBeTruthy();
    expect(row('online').querySelector('.lucide-laptop')).toBeTruthy();
    expect(screen.getByText(longName).className).toContain('break-words');
    expect(screen.getByText(longName).parentElement!.parentElement!.className).toContain('min-w-0');
  });
  it('makes only the opening row amber while waiting online/offline rows retain their status', () => {
    mount('opening');
    expect(dot('opening').className).toContain('bg-gold');
    expect(dot('opening').getAttribute('aria-hidden')).toBe('true');
    expect(within(row('opening')).getByRole('status').className).toContain('text-gold-ink');
    expect(dot('online').className).toContain('bg-mint');
    expect(dot('offline').className).toContain('bg-muted');
    expect(row('online').disabled).toBe(true);
    expect(row('offline').disabled).toBe(true);
    expect(row('offline').textContent).toContain('Offline');
  });
  it('exposes opening on the current block only on the full page', () => {
    mount('current');
    expect(dot('current').className).toContain(surface === 'page' ? 'bg-gold' : 'bg-mint');
    if (surface === 'page') expect(within(row('current')).getByRole('status').textContent).toBe('Opening…');
  });
  it('keeps failures in the existing red text with Retry and refresh failures in the banner', () => {
    mount(null, true, true);
    const failed = screen.getByRole('alert').parentElement!.parentElement!.parentElement!;
    expect(failed.querySelector('[data-status-indicator]')?.className).toContain('bg-mint');
    expect(screen.getByRole('alert').className).toContain('text-destructive-ink');
    expect(within(failed).getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getAllByRole('status').some((node) => node.className.includes('border-gold'))).toBe(true);
  });
});

// Max 2026-11-19 10:53: the current block follows the connection, as the Desktop's current row does.
describe('the current block follows the connection', () => {
  const offlineCurrent = { ...session, instances: [instance('current', { available: false }), ...session.instances.slice(1)] };
  const rows = (extra: Partial<{ openingId: string | null; failure: { id: string; reason: 'mobileApp.sheet.openFailed' } | null }> = {}) =>
    ({ openingId: null, expandedId: null, failure: null, onOpen: noop, ...extra });
  const page = (props: { onBack?: () => void; rows?: ReturnType<typeof rows>; locked?: boolean } = {}) => render(<I18nextProvider i18n={i18n}>
    <InstancesPage session={offlineCurrent} busy={null} locked={props.locked ?? false} errorKey={null} rows={props.rows ?? rows()}
      onRefresh={noop} onAccount={noop} onAdd={noop} onReopen={noop} onBack={props.onBack} />
  </I18nextProvider>);
  const block = () => screen.getByRole('heading', { name: en.mobileApp.sheet.youreIn }).nextElementSibling as HTMLElement;
  const blockDot = () => block().querySelector('[data-status-indicator]')!;
  const live = (surface: string) => {
    if (surface === 'sheet') render(<I18nextProvider i18n={i18n}><InstanceSheetPage session={offlineCurrent} locked={false} refreshing={false} stale={false}
      rows={rows()} onClose={noop} onRefresh={noop} onAdd={noop} /></I18nextProvider>);
    else page({ onBack: noop });
  };
  it.each(['sheet', 'page over a live instance'])('stays green and openable in the %s though the catalog says offline', (surface) => {
    live(surface);
    expect(blockDot().className).toContain('bg-mint');
    expect(within(block()).getByRole('img', { name: 'Online' })).toBeTruthy();
    expect(block().textContent).not.toContain('Offline');
    expect(within(block()).getByRole('button').hasAttribute('disabled')).toBe(false);
  });
  it('is gold while reopening and grey when reopening failed, with nothing open', () => {
    page({ rows: rows({ openingId: 'current' }), locked: true });
    expect(blockDot().className).toContain('bg-gold');
    cleanup();
    render(<I18nextProvider i18n={i18n}><InstancesPage session={session} busy={null} locked={false} errorKey={null}
      rows={rows({ failure: { id: 'current', reason: 'mobileApp.sheet.openFailed' } })} onRefresh={noop} onAccount={noop} onAdd={noop} onReopen={noop} /></I18nextProvider>);
    expect(blockDot().className).toContain('bg-muted');
    expect(blockDot().getAttribute('aria-hidden')).toBe('true');
    expect(within(block()).getByRole('alert')).toBeTruthy();
    expect(within(block()).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
  it('keeps the catalog word with nothing open and no attempt under way', () => {
    page();
    expect(blockDot().className).toContain('bg-muted');
    expect(block().textContent).toContain('Offline');
  });
});
