import { SecondFactorScreen } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { account, bilingual, longEmail, noop } from './fixtures';

// Second step of mobile sign-in for an account with two-step verification
// (avibe.bot owns it: passkeys, an authenticator app, recovery codes).
type SecondFactorProps = Parameters<typeof SecondFactorScreen>[0];
type SecondFactorSession = SecondFactorProps['session'];

const challengeSession = (
  methods: SecondFactorSession['methods'] = ['passkey', 'totp'],
  email: string | null = account.email,
): SecondFactorSession => ({
  status: 'secondFactor',
  email,
  gate: 'challenge',
  methods,
});

const secondFactor = (props: Partial<SecondFactorProps> = {}) => () => (
  <SecondFactorScreen
    session={challengeSession()}
    step="passkey"
    code=""
    busy={null}
    locked={false}
    errorKey={null}
    retryMinutes={15}
    onStep={noop}
    onCodeChange={noop}
    onVerify={noop}
    onPasskey={noop}
    onOpenSecurity={noop}
    onSetupDone={noop}
    onDifferentAccount={noop}
    {...props}
  />
);

const states: { name: string; title: string; props: Partial<SecondFactorProps>; size?: [number, number] }[] = [
  { name: 'passkey', title: '两步验证 · 通行密钥优先', props: {} },
  { name: 'passkey-waiting', title: '两步验证 · 等待系统通行密钥面板', props: { busy: 'passkey', locked: true } },
  { name: 'passkey-failed', title: '两步验证 · 通行密钥没完成', props: { errorKey: 'mobileApp.twoFactor.passkeyFailed' } },
  { name: 'totp', title: '两步验证 · 身份验证器验证码', props: { step: 'totp' } },
  { name: 'totp-partial', title: '验证器验证码 · 输入中', props: { step: 'totp', code: '28' } },
  { name: 'totp-verifying', title: '验证器验证码 · 验证中', props: { step: 'totp', code: '287082', busy: 'verify', locked: true } },
  { name: 'totp-invalid', title: '验证器验证码 · 不正确', props: { step: 'totp', code: '287083', errorKey: 'mobileApp.twoFactor.codeInvalid' } },
  { name: 'totp-rate-limited', title: '验证器验证码 · 尝试过多', props: { step: 'totp', code: '287082', errorKey: 'mobileApp.twoFactor.rateLimited' } },
  { name: 'totp-network', title: '验证器验证码 · 网络错误保留输入', props: { step: 'totp', code: '287082', errorKey: 'mobileApp.errors.network' } },
  { name: 'totp-only', title: '只有身份验证器 · 没有通行密钥入口', props: { session: challengeSession(['totp']), step: 'totp' } },
  { name: 'recovery', title: '使用恢复码', props: { step: 'recovery', code: 'k7m2p-9xq4r' } },
  { name: 'recovery-invalid', title: '恢复码不正确或已用过', props: { step: 'recovery', code: 'k7m2p-9xq4r', errorKey: 'mobileApp.twoFactor.recoveryInvalid' } },
  { name: 'setup-required', title: '必须先开启 · 去 avibe.bot 设置', props: { session: { status: 'secondFactor', email: account.email, gate: 'enroll', methods: [] }, step: 'setup' } },
  {
    name: 'totp-small',
    title: '验证器验证码 · 长邮箱与小屏',
    props: { session: challengeSession(['passkey', 'totp'], longEmail), step: 'totp', code: '28' },
    size: [375, 667],
  },
];

const designFrames = frames(states.flatMap(({ name, title, props, size }) =>
  bilingual(name, title, secondFactor(props), size)));

export default designFrames;
