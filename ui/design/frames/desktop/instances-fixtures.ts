import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { AccountLoginViewProps } from '@/desktop/AccountLoginView';
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
  ['account', 'Bottom account menu · upward opening'],
  ['instances', 'Local, owned and shared instances'],
  ['empty', 'Signed in · no other instances'],
  ['loading', 'Instance list loading · local stays available'],
  ['list-error', 'Instance discovery failed'],
  ['email', 'Email sign-in · registration disclosure'],
  ['code', 'Verify email code'],
  ['code-error', 'Invalid or expired code'],
  ['registering', 'Automatic local registration in progress'],
  ['registered', 'Local registration and remote access ready'],
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
  fixture: { state, lang, connection: ['signed-out', 'email', 'code', 'code-error', 'setup-entry'].includes(state) ? 'signed-out' : state === 'switching' ? 'switching' : state === 'offline' ? 'offline' : 'signed-in', updateAvailable: ['updates', 'offline'].includes(state) },
  fixtures: workbenchFixtures({
    ...agentsApi(),
    ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: false },
    // Local Runtime matches the rc9 shell shown in the local updater fixture.
    '/api/version': { current: state === 'signed-out' ? '3.2.6rc9' : '3.2.4', latest: state === 'signed-out' ? '3.2.6rc9' : '3.2.4', has_update: false, error: null },
  }, { copy: copyFor(lang === 'en' ? en : zh) }),
})));

// Static props for the actual product card. Native lifecycle and credentials are
// deliberately absent: embedded design documents cannot acquire Desktop IPC.
export function loginViewProps(fixture: DesktopInstanceFixture): AccountLoginViewProps | null {
  if (!['email', 'code', 'code-error'].includes(fixture.state)) return null;
  const verify = fixture.state !== 'email';
  const noAction = () => {};
  return {
    verify, email: 'max@example.test', value: verify ? '123456' : '', busy: false,
    errorMessage: fixture.state === 'code-error' ? (fixture.lang === 'en' ? en : zh).desktopClient.errors.code : null,
    onValueChange: noAction, onSubmit: noAction, onChangeEmail: noAction,
    onResendCode: noAction, onContinueLocal: noAction,
  };
}
