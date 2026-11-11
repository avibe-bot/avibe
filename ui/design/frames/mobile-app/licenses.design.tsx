import { frames } from '../../frame';
import { AccountPage, LicensesPage } from '../../kit/mobile-app';
import { bilingual, licensesFixture, personal } from './fixtures';

/** The App's open-source licenses page and its entry on Account; approved by 产品伙伴 on 2026-11-11. */
const expanded = { ...licensesFixture, groups: licensesFixture.groups.map((group, index) => ({ ...group, open: index === 0 })) };

const designFrames = frames([
  ...bilingual('entry', '开源许可 · 账户里的入口', () => <AccountPage instance={personal} licenses />),
  ...bilingual('default', '开源许可 · 首屏', () => <LicensesPage fixture={licensesFixture} />),
  ...bilingual('group-open', '开源许可 · 展开一组许可文本（整页）', () => <LicensesPage fixture={expanded} />, [390, 2400]),
]);

export default designFrames;
