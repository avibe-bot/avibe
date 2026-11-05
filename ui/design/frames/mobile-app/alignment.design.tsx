// Reference captures only: unchanged shipped components, separate from proposed screens.
import { ErrorFallback } from '@/components/ui/error-fallback';
import { frames } from '../../frame';
import { PhoneScreen } from '../../shell/PhoneScreen';
import drawerFrames from '../mobile/apps-drawer.design';
import { account } from './fixtures';

const drawer = drawerFrames[0];
const designFrames = frames([
  {
    ...drawer,
    name: 'account-reference',
    title: '对齐依据 · 已上线的账户抽屉',
    fixtures: {
      ...drawer.fixtures,
      api: { ...drawer.fixtures?.api, '/api/session': { remote: true, authenticated: true, email: account.email } },
    },
    click: ['role=button[name="{t:more.account}"]'],
  },
  {
    name: 'error-reference', title: '对齐依据 · 已上线的 ErrorFallback',
    width: 390, height: 844, status: 'shipped', fixtures: { bots: [] },
    render: () => <PhoneScreen tabBar={<></>}><ErrorFallback error="Connection unavailable" reset={() => {}} variant="page" /></PhoneScreen>,
  },
]);
export default designFrames;
