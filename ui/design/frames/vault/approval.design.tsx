// The one approval dialog (VaultApprovalDialog around VaultApprovalCard) as the Vaults page's Review opens
// it: an agent asks to use a secret, a skill asks for its tagged set, an agent asks to sign or to delete.
// The chat cards and the floating approval bar open the same dialog.
import type { VaultRequest } from '@/context/ApiContext';
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { consolePage, phoneShell } from '../workbench/fixtures';
import { accessRequest, deleteRequest, signRequest, skillAccessRequest, vaultApi, vaultFixtures } from './fixtures';

const page = consolePage(<VaultsPage />);
const phone = phoneShell('/vaults', <VaultsPage />);
const pending = (request: VaultRequest) => vaultFixtures(vaultApi({ grants: [], requests: [request] }));
const review = ['role=button[name="{t:vaults.requests.review}"]'];

const designFrames = frames([
  {
    name: 'access',
    title: '审批 · Agent 想使用一个密钥',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(accessRequest),
    click: review,
    render: page,
  },
  {
    name: 'access-skill',
    title: '审批 · 技能 deploy-aws 想使用它的一组密钥',
    width: 1192,
    height: 960,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(skillAccessRequest),
    click: review,
    render: page,
  },
  {
    name: 'sign',
    title: '审批 · Agent 想签名一笔以太坊交易',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(signRequest),
    click: review,
    render: page,
  },
  {
    name: 'delete',
    title: '审批 · Agent 想删除一个密钥',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(deleteRequest),
    click: review,
    render: page,
  },
  {
    name: 'access-phone',
    title: '审批 · 使用密钥 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(accessRequest),
    click: review,
    render: phone,
  },
  {
    name: 'sign-phone',
    title: '审批 · 签名 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures: pending(signRequest),
    click: review,
    render: phone,
  },
]);

export default designFrames;
