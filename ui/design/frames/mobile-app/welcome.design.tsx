import { frames } from '../../frame';
import { InstancePicker, WelcomeIntro, WelcomePage } from '../../kit/mobile-app';
import { bilingual, loginStates, mixedInstances, smallCodeFixture } from './fixtures';

const designFrames = frames([
  ...bilingual('intro', '欢迎 · 云间来信', () => <WelcomePage />),
  ...loginStates.flatMap(({ name, title, fixture }) => bilingual(name, title, () => <WelcomeIntro fixture={fixture} />)),
  ...bilingual('success', '验证成功 → 选择实例', () => <InstancePicker state="mixed" instances={mixedInstances} />),
  ...bilingual('code-small', '验证码 · 长邮箱与小屏', () => <WelcomeIntro fixture={smallCodeFixture} />, [375, 667]),
]);

export default designFrames;
