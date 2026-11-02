// The protected tier's setup and unlock panel, as the add / provide dialog shows it once Protected is chosen.
// The panel takes the vault's state as a prop; frames pass that state directly, because reaching it in the
// dialog would start the vault sandbox (an iframe from sandbox.avibe.bot), which no frame may load.
import type { useProtectedVault } from '@/lib/useProtectedVault';
import { VaultProtectedUnlock } from '@/components/ui/vault-protected-unlock';

import { frames } from '../../frame';
import { vaultFixtures } from './fixtures';

type Vault = ReturnType<typeof useProtectedVault>;
const idle = () => Promise.resolve();
const unavailable = () => Promise.reject(new Error('design frame'));

/** The protected vault in one state, as `useProtectedVault` reports it; its actions do nothing. */
const vault = (status: Vault['status'], error: string | null = null): Vault => ({
  status,
  error,
  setError: () => {},
  refresh: idle,
  setupPasskey: idle,
  unlockPasskey: idle,
  sealValue: unavailable,
  signProtectedRequest: unavailable,
  approveProtectedRelease: unavailable,
  revealProtectedValue: unavailable,
  afterCreated: () => {},
  lock: () => {},
  lockAndResetForPolicyChange: () => {},
  discardAndRefresh: idle,
  hasPasskey: () => true,
  passkeyUsableHere: () => true,
});

// The dialog's column: DialogContent is max-w-lg with 24 px padding.
const panel = (state: Vault, secretName?: string) => () => (
  <div className="mx-auto w-full max-w-lg p-6">
    <VaultProtectedUnlock vault={state} secretName={secretName} onContinueInBrowser={() => {}} />
  </div>
);

const fixtures = vaultFixtures();

const designFrames = frames([
  {
    name: 'setup',
    title: '保护档 · 首次设置 passkey',
    width: 560,
    height: 520,
    status: 'shipped',
    fixtures,
    render: panel(vault('needs-setup')),
  },
  {
    name: 'unlock',
    title: '保护档 · 用 passkey 解锁',
    width: 560,
    height: 440,
    status: 'shipped',
    fixtures,
    render: panel(vault('locked'), 'STRIPE_LIVE_KEY'),
  },
  {
    name: 'unlocked',
    title: '保护档 · 已解锁',
    width: 560,
    height: 140,
    status: 'shipped',
    fixtures,
    render: panel(vault('unlocked')),
  },
  {
    name: 'checking',
    title: '保护档 · 正在检查',
    width: 560,
    height: 140,
    status: 'shipped',
    fixtures,
    render: panel(vault('checking')),
  },
  {
    name: 'error',
    title: '保护档 · 检查失败',
    width: 560,
    height: 200,
    status: 'shipped',
    fixtures,
    render: panel(vault('error', 'vmk-discovery-failed')),
  },
  {
    name: 'setup-phone',
    title: '保护档 · 首次设置 · 手机',
    width: 390,
    height: 600,
    status: 'shipped',
    fixtures,
    render: panel(vault('needs-setup')),
  },
]);

export default designFrames;
