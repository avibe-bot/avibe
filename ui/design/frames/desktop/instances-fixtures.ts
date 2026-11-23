import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import type { DesktopAccountRowViewProps } from '@/desktop/DesktopAccountRowView';
import type { InstanceSwitcherViewProps } from '@/desktop/InstanceSwitcherView';
import type { AccountSecondFactorViewProps } from '@/desktop/AccountSecondFactorView';
import type { AccountDetailsViewProps } from '@/desktop/AccountDetailsView';
import type { FrameSpec } from '../../frame';
import type { DesktopInstanceFixture, DesktopInstanceState } from '../../kit/desktop-instances';
import { workbenchFixtures, homeFactsApi } from '../workbench/fixtures';
import { agentsApi } from '../agents/fixtures';

// Bind shipped copy to the frame's language, regardless of the canvas language.
export function copyFor(value: object, prefix = ''): Record<string, { zh: string; en: string }> {
  return Object.fromEntries(Object.entries(value).flatMap(([key, entry]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof entry === 'string' ? [[path, { zh: entry, en: entry }]] : entry && typeof entry === 'object' ? Object.entries(copyFor(entry, path)) : [];
  }));
}
const states: [DesktopInstanceState, string][] = [
  ['signed-out', 'Workbench · bottom account row beneath existing tools'],
  ['updates', 'Workbench · Desktop update popover'],
  ['setup-entry', 'Setup completion · optional sign-in handoff'],
  ['account', 'Footer · direct instance entry'],
  ['account-remote-disabled', 'Footer · unregistered local fallback'],
  ['account-long', 'Footer · long instance name'],
  ['instances', 'Current instance and grouped other instances'],
  ['current-shared', 'Current shared instance · ownership retained'],
  ['offline-shared', 'Shared offline instance · disabled'],
  ['profile', 'Account · local remote access address'],
  ['profile-error', 'Account · unresolved persistence error'],
  ['sign-out-error', 'Sign-out cleanup failure · explicit retry'],
  ['empty', 'Signed in · no other instances'],
  ['loading', 'Instance list loading · local stays available'],
  ['list-error', 'Instance discovery failed'],
  ['enroll', 'Two-step verification required · add an authenticator'],
  ['consent', 'Instance authorization modal'],
  ['switch-error', 'Failed switch · retry on selected row'],
  ['upgrade-error', 'Selected instance requires upgrade'],
  ['offline-help', 'Offline row · disabled with page refresh'],
  ['long-content', 'Long instance names and shared account email'],
  ['registering', 'Automatic local registration in progress'],
  ['registration-error', 'Registered · tunnel not ready'],
  ['conflict', 'Local instance linked to another account'],
  ['add', 'Add another computer or CLI server'],
  ['advanced', 'Advanced authorized HTTPS URL'],
  ['offline', 'Remote unreachable · return locally'],
  ['switching', 'Connecting · return locally'],
  ['mini', 'Mini · current instance badge'],
];
export const desktopInstanceFrames: (Omit<FrameSpec, 'render'> & { fixture: DesktopInstanceFixture })[] = states.flatMap(([state, title]) => (['zh', 'en'] as const).map(lang => ({
  name: `${state}-${lang}`,
  title: `${title} · ${lang}`,
  width: 1200, height: 800, themes: ['light', 'dark'], status: 'proposed', route: '/',
  fixture: { state, lang, connection: ['signed-out', 'setup-entry'].includes(state) ? 'signed-out' : state === 'switching' ? 'switching' : state === 'offline' ? 'offline' : 'signed-in', updateAvailable: ['updates', 'offline'].includes(state) },
  fixtures: workbenchFixtures({
    ...agentsApi(),
    ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: false },
    // Local Runtime matches the rc9 shell shown in the local updater fixture.
    '/api/version': { current: (state === 'signed-out' || state.startsWith('account')) ? '3.2.6rc9' : '3.2.4', latest: (state === 'signed-out' || state.startsWith('account')) ? '3.2.6rc9' : '3.2.4', has_update: false, error: null },
  }, { copy: copyFor(lang === 'en' ? en : zh) }),
})));

export function accountRowViewProps(fixture: DesktopInstanceFixture): Omit<DesktopAccountRowViewProps, 'localUpdater'> | null {
  if (fixture.state !== 'signed-out' && !fixture.state.startsWith('account')) return null;
  const signedIn = fixture.state !== 'signed-out';
  return {
    state: {
      version: 1, revision: 1, generation: 1,
      account: { status: signedIn ? 'signed_in' : 'signed_out', identity: signedIn ? { id: 'fixture', email: fixture.state === 'account-long' ? 'long-research-account-name@example.test' : 'max@example.test' } : null },
      connection: { status: 'local', committed: { kind: 'local', name: '小麦的 Mac', instance_id: null }, pending: null },
      local: { slug: !signedIn || fixture.state === 'account-remote-disabled' ? null : fixture.state === 'account-long' ? 'research-and-development-workstation' : 'max', name: fixture.state === 'account-long' ? 'Research-and-development-MacBook.local' : '小麦的 Mac.local', remote_access: fixture.state === 'account-remote-disabled' ? 'off' : 'on' },
      desktop: { version: '3.2.6rc9', update: { phase: 'current', latest: null, channel: 'test', skippedVersion: null, interactive: false } },
      remote_auxiliary: 'disabled_incomplete',
    },
    onPresent: () => {},
  };
}

export function switcherViewProps(fixture: DesktopInstanceFixture): InstanceSwitcherViewProps | null {
  if (!['instances', 'empty', 'loading', 'list-error', 'offline', 'switching', 'consent', 'switch-error', 'upgrade-error', 'offline-help', 'long-content', 'current-shared', 'offline-shared'].includes(fixture.state)) return null;
  const strings = (fixture.lang === 'en' ? en : zh).desktopAccount;
  const noop = () => {};
  const email = fixture.state === 'long-content' ? 'international-design-and-engineering-account@cross-region-research-and-development-workstations.example.test' : 'max@example.test';
  const state: InstanceSwitcherViewProps['state'] = {
    version: 1, revision: 1, generation: 1,
    account: { status: 'signed_in', identity: { id: 'fixture', email }, email, gate: null, methods: ['totp'] },
    connection: { status: 'local', committed: { kind: 'local', name: 'Max-2.local', instance_id: 'local' }, pending: null },
    enrollment: { status: 'paired_this_account', instance_id: 'local', error_code: null },
    local: { name: 'Max-2.local', instance_id: 'local', slug: 'max', remote_access: 'on' },
    catalog: { status: 'ready', instances: [
      { id: 'local', name: 'max', host: 'max-app.avibe.bot', url: 'https://max-app.avibe.bot', relationship: 'owner', owner_email: 'max@example.test', available: true },
      { id: 'studio', name: 'Studio', host: 'studio-app.avibe.bot', url: 'https://studio-app.avibe.bot', relationship: 'owner', owner_email: 'max@example.test', available: true },
      { id: 'weekend', name: 'Weekend lab', host: 'weekend-app.avibe.bot', url: 'https://weekend-app.avibe.bot', relationship: 'owner', owner_email: 'max@example.test', available: false },
      { id: 'team', name: fixture.state === 'long-content' ? '跨时区产品研发团队 — Product design and research workstation' : 'Product team', host: 'team-app.avibe.bot', url: 'https://team-app.avibe.bot', relationship: 'shared', owner_email: 'zhou.min.design-and-research@example.test', available: true },
    ] },
    desktop: { version: '3.2.6rc19', update: { phase: 'current', latest: null, channel: 'test', skippedVersion: null, interactive: false } },
    instance_update: null, consent: null, remote_auxiliary: 'local',
  };
  if (['empty', 'loading', 'list-error'].includes(fixture.state)) { state.catalog.instances = []; state.local.slug = null; }
  if (fixture.state === 'loading') state.catalog.status = 'loading';
  if (fixture.state === 'list-error') state.catalog.status = 'error';
  const target = { kind: 'remote' as const, name: 'Studio', instance_id: 'studio', canonical_origin: 'https://studio-app.avibe.bot' };
  if (fixture.state === 'switching') state.connection = { ...state.connection, status: 'switching', pending: target };
  if (fixture.state === 'offline') {
    state.connection = { status: 'remote_unreachable', committed: target, pending: null };
    state.current_instance = state.catalog.instances.find((item) => item.id === target.instance_id) ?? null;
  }
  if (fixture.state === 'current-shared') {
    state.current_instance = state.catalog.instances.find((item) => item.id === 'team') ?? null;
    state.catalog.instances = state.catalog.instances.filter((item) => item.id !== 'team');
    state.connection = { status: 'remote_connected', committed: { kind: 'remote', name: 'Product team', instance_id: 'team', canonical_origin: 'https://team-app.avibe.bot' }, pending: null };
  }
  if (fixture.state === 'offline-shared') state.catalog.instances = state.catalog.instances.map((item) => item.id === 'team' ? { ...item, available: false } : item);
  if (fixture.state === 'consent') state.consent = { instance_id: 'team', instance_name: 'Product team', host: 'team-app.avibe.bot', scope: 'openid email profile' };
  return { state, localInstanceId: 'local', busy: false, pendingId: fixture.state === 'switching' ? 'studio' : null,
    failure: ['switch-error', 'upgrade-error'].includes(fixture.state) ? { instanceId: 'studio', message: fixture.state === 'upgrade-error' ? strings.errors.upgrade : strings.errors.request } : null,
    onAccount: noop, onAdd: noop, onSelect: noop, onLocal: noop, onCancel: noop, onRefresh: noop, onConsent: noop };
}

export function factorViewProps(fixture: DesktopInstanceFixture): AccountSecondFactorViewProps | null {
  if (fixture.state !== 'enroll') return null;
  const noop = () => {};
  return { code: '', busy: false, material: null, errorMessage: null, onCodeChange: noop, onVerify: noop, onEnroll: noop, onLocal: noop };
}

export function profileViewProps(fixture: DesktopInstanceFixture): AccountDetailsViewProps | null {
  if (!['profile', 'profile-error'].includes(fixture.state)) return null;
  return { errorMessage: fixture.state === 'profile-error' ? (fixture.lang === 'en' ? en : zh).desktopAccount.errors.request : null, email: 'long-research-account-name@example.test', busy: false,
    onBack: () => {}, onSignOut: () => {} };
}
