// Turning ask-before-signing off with a Passkey (contract v1.1), as the parent page shows it: the
// plain-key Edit dialog and a seed wallet's account cards. The Passkey window itself is the
// sandbox's own (vault-sandbox frames); these are the states around it.
import { VaultHdWalletDialog } from '@/components/ui/vault-hd-wallet-detail';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import type { AskTurnOffState } from '@/lib/useAskTurnOff';

import { frames } from '../../frame';
import { consolePage } from '../workbench/fixtures';
import { vaultApi, vaultFixtures, vaultSecrets } from './fixtures';
import { walletAccounts, walletFixtures, walletSecret } from './mnemonic/fixtures';

const noop = () => {};
// ETH_HOT_KEY with ask-before-signing on, and the turn-off state every switch shows.
const keyOn = (askTurnOff: AskTurnOffState) => ({
  ...vaultFixtures(vaultApi({
    grants: [],
    requests: [],
    secrets: vaultSecrets.map((s) => (s.name === 'ETH_HOT_KEY' ? { ...s, policy: { ...s.policy, always_ask: true } } : s)),
  })),
  askTurnOff,
});
const editKey = ['li:has-text("ETH_HOT_KEY") >> role=button[name="{t:vaults.rowActions}"]', 'role=menuitem[name="{t:vaults.editSecret}"]'];
const page = consolePage(<VaultsPage />);
const wallet = (askTurnOff: AskTurnOffState) => ({ ...walletFixtures, askTurnOff });
const accounts = () => <VaultHdWalletDialog secret={walletSecret(walletAccounts)} open onOpenChange={noop} onChanged={noop} />;

const ready: AskTurnOffState = { phase: 'ready' };
const working: AskTurnOffState = { phase: 'working' };
const failed = (failure: NonNullable<Extract<AskTurnOffState, { phase: 'ready' }>['failure']>): AskTurnOffState => ({ phase: 'ready', failure });

const edit = (name: string, title: string, state: AskTurnOffState) =>
  ({ name, title, width: 1192, height: 940, status: 'proposed' as const, route: '/vaults', fixtures: keyOn(state), click: editKey, render: page });
const card = (name: string, title: string, state: AskTurnOffState, width = 860, height = 1180) =>
  ({ name, title, width, height, status: 'proposed' as const, fixtures: wallet(state), render: accounts });

const designFrames = frames([
  edit('key-can-turn-off', '签名密钥 · 已打开，可用 Passkey 关闭', ready),
  edit('key-waiting', '签名密钥 · 等待 Passkey 确认', working),
  edit('key-setup-required', '签名密钥 · 还没有 Passkey（说明在哪设置）', failed('setup_required')),
  edit('key-expired', '签名密钥 · 确认超时，没有关闭', failed('expired')),
  edit('key-clock-skew', '签名密钥 · 设备时间不准', failed('clock_skew')),
  edit('key-factor-unavailable', '签名密钥 · Passkey 现在不能用于确认', failed('factor_unavailable')),
  edit('key-legacy-registration', '签名密钥 · 较早版本设置的 Passkey，暂不能确认', failed('legacy_registration')),
  edit('key-changed', '签名密钥 · 在别处改过', failed('changed')),
  edit('key-failed', '签名密钥 · 没能关闭（唯一的红色）', failed('failed')),
  card('wallet-can-turn-off', '钱包账户 · 打开的账户可用 Passkey 关闭（无锁）', ready),
  card('wallet-waiting', '钱包账户 · 等待 Passkey 确认', working),
  card('wallet-expired', '钱包账户 · 确认超时', failed('expired')),
  card('wallet-phone-waiting', '钱包账户 · 手机 · 等待确认', working, 390, 844),
]);

export default designFrames;
