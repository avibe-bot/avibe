import { frames } from '../../frame';
import { AccountPage, LicensesScreen } from '../../kit/mobile-app';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { bilingual, licensesFixture, personal } from './fixtures';

/**
 * The App's open-source licenses page and its entry on Account; approved by 产品伙伴 on 2026-11-11.
 * The page body and the entry are the shipped `ui/src/mobile/LicensesPage.tsx`; the screen around
 * them is the approved A composition. The fixture is illustrative; the App reads generated data.
 */
const withLicenseCopy = (specs: ReturnType<typeof bilingual>) => specs.map(frame => {
  const copy = frame.name.endsWith('-en') ? en.mobileApp.licenses : zh.mobileApp.licenses;
  const keys = Object.fromEntries(Object.entries(copy).map(([key, text]) => [`mobileApp.licenses.${key}`, { zh: text, en: text }]));
  return { ...frame, fixtures: { ...frame.fixtures, copy: { ...frame.fixtures?.copy, ...keys } } };
});

const designFrames = frames([
  ...withLicenseCopy(bilingual('entry', '开源许可 · 账户里的入口', () => <AccountPage instance={personal} licenses />)),
  ...withLicenseCopy(bilingual('default', '开源许可 · 首屏', () => <LicensesScreen licenses={licensesFixture} />)),
  ...withLicenseCopy(bilingual('group-open', '开源许可 · 展开一组许可文本（整页）', () => <LicensesScreen licenses={licensesFixture} openGroup={0} />, [390, 2400])),
]);

export default designFrames;
