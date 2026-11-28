import { PasskeySignInScreen, WelcomeScreen } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { bilingual, noop } from './fixtures';

// Passkey sign-in with no email first (#3946), shipped. Layout B stays as approved: email is
// the one primary action, Google, GitHub and Apple follow avibe.bot's 「或」 as one row of
// marks, and the passkey way in is one quiet link below them, as on avibe.bot's login card.
// Its step uses avibe.bot's own passkey copy; Back returns to Welcome.
const designFrames = frames([
  ...bilingual('passkey-link', '欢迎 · 邮箱为主，三方一行，下方通行密钥登录链接', () => (
    <WelcomeScreen locked={false} providers={['google', 'github', 'apple']} onContinue={noop} onProvider={noop} onPasskey={noop} />
  )),
  ...bilingual('passkey-step', '通行密钥登录 · 无需邮箱', () => (
    <PasskeySignInScreen busy={null} locked={false} errorKey={null} onPasskey={noop} onBack={noop} />
  )),
  ...bilingual('passkey-waiting', '通行密钥登录 · 等待系统面板', () => (
    <PasskeySignInScreen busy="primaryPasskey" locked errorKey={null} onPasskey={noop} onBack={noop} />
  )),
  ...bilingual('passkey-incomplete', '通行密钥登录 · 没有完成', () => (
    <PasskeySignInScreen busy={null} locked={false} errorKey="signIn.passkey.incomplete" onPasskey={noop} onBack={noop} />
  )),
]);

export default designFrames;
