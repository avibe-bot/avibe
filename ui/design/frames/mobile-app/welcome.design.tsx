import { frames } from '../../frame';
import { WelcomeIntro, WelcomePage } from '../../kit/mobile-app';
import { bilingual, loginStates, smallCodeFixture } from './fixtures';
import { WelcomeScreen } from '@/mobile/MobileOnboarding';

const noop = () => {};
const allProviders = ['google', 'github', 'apple'] as const;

const designFrames = frames([
  ...bilingual('intro', '欢迎 · 云间来信', () => <WelcomePage />),
  ...loginStates.flatMap(({ name, title, fixture }) => bilingual(name, title, () => <WelcomeIntro fixture={fixture} />)),
  // Google, GitHub and Apple on Welcome (owner, 2026-11-18: layout B). Email stays the primary
  // action; the other accounts follow avibe.bot's 「或」 as one row of marks.
  ...bilingual('providers-row', '欢迎 · 邮箱为主，Google / GitHub / Apple 为一行图标', () => <WelcomeScreen locked={false} providers={allProviders} onContinue={noop} onProvider={noop} />),
  ...bilingual('providers-row-error', '欢迎 · Google 邮箱未验证时的提示', () => <WelcomeScreen locked={false} providers={allProviders} errorKey="mobileApp.provider.googleEmailUnverified" onContinue={noop} onProvider={noop} />),
  ...bilingual('code-small', '验证码 · 长邮箱与小屏', () => <WelcomeIntro fixture={smallCodeFixture} />, [375, 667]),
]);

export default designFrames;
