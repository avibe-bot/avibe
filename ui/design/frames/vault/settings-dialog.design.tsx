// Protected-tier settings (the Vaults page's gear): how long the vault stays unlocked in this browser,
// and whether every approval and reveal asks for the passkey.
import { VaultsPage } from '@/components/workbench/VaultsPage';

import { frames } from '../../frame';
import { consolePage, phoneShell } from '../workbench/fixtures';
import { vaultApi, vaultFixtures } from './fixtures';

const fixtures = vaultFixtures(vaultApi({ grants: [], requests: [] }));
const strict = vaultFixtures({
  ...vaultApi({ grants: [], requests: [] }),
  '/api/vault/settings': {
    ok: true,
    settings: { unlock_window_seconds: 300, strict_approvals: true, last_grant_ttl: 300 },
    policy: { windowSeconds: 300, strictApprovals: true, parentValueSealAllowed: true },
  },
});
const open = ['role=button[name="{t:vaults.settings.title}"]'];

const designFrames = frames([
  {
    name: 'default',
    title: '保护档设置 · 解锁 10 分钟',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/vaults',
    fixtures,
    click: open,
    render: consolePage(<VaultsPage />),
  },
  {
    name: 'strict',
    title: '保护档设置 · 严格授权',
    width: 1192,
    height: 760,
    status: 'shipped',
    route: '/vaults',
    fixtures: strict,
    click: open,
    render: consolePage(<VaultsPage />),
  },
  {
    name: 'phone',
    title: '保护档设置 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/vaults',
    fixtures,
    click: open,
    render: phoneShell('/vaults', <VaultsPage />),
  },
]);

export default designFrames;
