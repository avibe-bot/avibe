// @vitest-environment jsdom
// ui/design: update/instance-and-app. The owner updated this computer's desktop app while meaning to update
// the instance it showed: one panel, one "Update", nothing saying whose. With two subjects in the panel,
// each version, state and button must say whose it is.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { UpdatePanel, UpdateSubjectsPanel, type UpdateSubjectSection } from './UpdatePanel';
import { modelFromDesktopState, modelFromRuntime } from './updatePanelModel';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });
afterEach(cleanup);

const CHECKED = '2026-11-20T07:00:00Z';
const idle = { checking: false, installing: false, restarting: false, result: null, prereleases: true, autoUpdate: false, checkedAt: CHECKED };
const instance = (extra = {}, view = {}) => modelFromRuntime(
  { current: '3.2.8rc2', latest: '3.2.8rc4', has_update: true, error: null, ...extra }, { ...idle, ...view },
);
const app = (phase: 'available' | 'current') => modelFromDesktopState({
  phase, latest: phase === 'available' ? '3.2.8-rc.4' : null, channel: 'test', prereleases: true, skippedVersion: null,
  interactive: true, autoUpdate: false, checkedAt: CHECKED, hostActions: ['check', 'prereleases', 'auto_update', 'install'],
}, { current: phase === 'available' ? '3.2.8-rc.2' : '3.2.8-rc.4' });

function mount(instanceSection: UpdateSubjectSection, appSection: UpdateSubjectSection) {
  render(<I18nextProvider i18n={i18n}>
    <UpdateSubjectsPanel instanceName="alex" instance={instanceSection} app={appSection} check={{ run: () => {} }} close={() => {}} />
  </I18nextProvider>);
  const section = (kind: string) => within(document.querySelector<HTMLElement>(`[data-update-subject="${kind}"]`)!);
  return { alex: section('instance'), thisApp: section('app') };
}

it('gives each subject its own named version and a button that says what it updates', () => {
  const updateInstance = vi.fn();
  const updateApp = vi.fn();
  const { alex, thisApp } = mount(
    { model: instance(), actions: { update: { run: updateInstance } } },
    { model: app('available'), actions: { update: { run: updateApp } } },
  );
  // The instance first, under its own name, with its version beside it; then this computer's app with its own.
  expect(document.querySelectorAll('[data-update-subject]')[0].getAttribute('data-update-subject')).toBe('instance');
  expect(alex.getByRole('button', { name: 'alex' })).toBeTruthy();
  expect(alex.getByText('3.2.8rc2')).toBeTruthy();
  expect(thisApp.getByRole('button', { name: en.update.subject.app })).toBeTruthy();
  // No bare "Current version", and no bare "Update": the two would be the same ambiguity again.
  expect(screen.queryByText(en.update.row.current)).toBeNull();
  expect(screen.queryByRole('button', { name: en.update.action.update })).toBeNull();
  // Each offer says what installing does to that subject.
  expect(alex.getByText(/alex restarts to finish/)).toBeTruthy();
  expect(thisApp.getByText(/Avibe on this computer restarts to finish\. alex is not affected\./)).toBeTruthy();

  fireEvent.click(alex.getByRole('button', { name: 'Update alex' }));
  expect(updateInstance).toHaveBeenCalledOnce();
  expect(updateApp).not.toHaveBeenCalled();
  fireEvent.click(thisApp.getByRole('button', { name: 'Update this app' }));
  expect(updateApp).toHaveBeenCalledOnce();
  expect(updateInstance).toHaveBeenCalledOnce();
});

it('names what was updated, and the other subject still says it is behind', () => {
  const { alex, thisApp } = mount(
    { model: instance({ current: '3.2.8rc4', has_update: false }, { result: { ok: true, target: '3.2.8rc4' } }), actions: { update: { run: () => {} } } },
    { model: app('available'), actions: { update: { run: () => {} } } },
  );
  expect(alex.getByText('alex updated to 3.2.8rc4')).toBeTruthy();
  expect(alex.queryByRole('button', { name: 'Update alex' })).toBeNull();
  expect(thisApp.getByText(en.update.status.available)).toBeTruthy();
  expect(thisApp.getByRole('button', { name: 'Update this app' })).toBeTruthy();
});

it("keeps a subject's last check and switches behind its name, and sends them to that subject only", () => {
  const instancePrereleases = vi.fn();
  const appPrereleases = vi.fn();
  const { alex, thisApp } = mount(
    { model: instance(), actions: { setPrereleases: instancePrereleases } },
    { model: app('current'), actions: { setPrereleases: appPrereleases } },
  );
  expect(screen.queryByRole('switch')).toBeNull();
  fireEvent.click(alex.getByRole('button', { name: 'alex' }));
  expect(alex.getByText(en.update.row.checkedAt)).toBeTruthy();
  fireEvent.click(alex.getByRole('switch', { name: en.update.setting.prereleases }));
  expect(instancePrereleases).toHaveBeenCalledExactlyOnceWith(false);
  expect(appPrereleases).not.toHaveBeenCalled();
  expect(thisApp.queryByRole('switch')).toBeNull();
});

it('shows an instance it cannot reach, or one the account cannot update, without a button', () => {
  const { alex } = mount({ model: null, actions: {} }, { model: app('current'), actions: {} });
  expect(alex.getByText(en.desktopClient.disconnected)).toBeTruthy();
  expect(alex.queryByRole('button', { name: /Update/ })).toBeNull();
  cleanup();
  // It answers, but no longer accepts the app's sign-in: not "disconnected".
  const refused = mount({ model: null, unavailable: 'unauthorized', actions: {} }, { model: app('current'), actions: {} });
  expect(refused.alex.queryByText(en.desktopClient.disconnected)).toBeNull();
  expect(refused.alex.getByText("Can't read alex's update status here.")).toBeTruthy();
  cleanup();
  // A member who cannot manage the instance: its container passes no install action.
  const member = mount({ model: instance(), actions: {} }, { model: app('current'), actions: {} });
  expect(member.alex.getByText(en.update.status.available)).toBeTruthy();
  expect(member.alex.queryByRole('button', { name: /Update/ })).toBeNull();
});

it("lets a subject whose install failed with no offer left try again through the panel's check", () => {
  // The desktop app's updater ends a failed install in `error` and drops the offer: its container has no install action.
  const failed = modelFromDesktopState({
    phase: 'error', latest: null, channel: 'test', prereleases: true, skippedVersion: null, interactive: true,
    autoUpdate: false, checkedAt: CHECKED, hostActions: ['check', 'prereleases', 'auto_update', 'install'],
  }, { current: '3.2.8-rc.2', installFailed: true });
  const check = vi.fn();
  render(<I18nextProvider i18n={i18n}>
    <UpdateSubjectsPanel instanceName="alex" instance={{ model: instance(), actions: {} }} app={{ model: failed, actions: {} }} check={{ run: check }} />
  </I18nextProvider>);
  const thisApp = within(document.querySelector<HTMLElement>('[data-update-subject="app"]')!);
  expect(thisApp.getByText(en.update.failure.install.title)).toBeTruthy();
  fireEvent.click(thisApp.getByRole('button', { name: en.update.action.retry }));
  expect(check).toHaveBeenCalledOnce();
});

it('keeps the single panel as it ships: "Current version", one "Update", the offer without a name', () => {
  render(<I18nextProvider i18n={i18n}><UpdatePanel model={app('available')} actions={{ update: { run: () => {} } }} /></I18nextProvider>);
  expect(screen.getByText(en.update.row.current)).toBeTruthy();
  expect(screen.getByRole('button', { name: en.update.action.update })).toBeTruthy();
  expect(screen.getByText(en.update.status.availableDetail)).toBeTruthy();
});
