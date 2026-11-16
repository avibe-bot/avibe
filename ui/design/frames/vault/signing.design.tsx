// Signing capability is shown alongside existing addresses, without changing keys.
import { VaultApprovalCard, VaultSigningIdentity } from '@/components/ui/vault-approval-card';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import type { VaultRequest, VaultSecret } from '@/context/ApiContext';

import { frames } from '../../frame';
import { consolePage, workbenchFixtures } from '../workbench/fixtures';

const standard: VaultSecret = {
  name: 'STANDARD_BTC_KEY', description: 'Public synthetic test key — never deposit funds',
  tags: ['test'], kind: 'keypair', protection: 'standard', signer_kind: 'local',
  source: 'user', policy: {}, last_used_at: null, use_count: 0,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  signing_addresses: {
    eth: '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf',
    btc_legacy: '1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH',
    btc_segwit: 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4',
    btc_taproot: 'bc1pmfr3p9j00pfxjh0zmgp99y8zftmd3s5pmedqhyptwy6lm87hf5sspknck9',
  },
};
const fixtures = workbenchFixtures({
  '/api/vault/secrets': { ok: true, secrets: [standard, { ...standard, name: 'PROTECTED_BTC_KEY', protection: 'protected' }] },
  '/api/vault/grants': { ok: true, grants: [] },
  '/api/vault/requests': { ok: true, requests: [] },
  '/api/vault/avault-authorization': { ok: true, required: false, detected_at: null, attempt: null },
  '/api/dependencies': { deps: [] },
  '/api/skills': { ok: true, skills: [] },
});
const request = {
  id: 'vrq_design_sign', request_type: 'sign', secret_name: standard.name, requester: {},
  status: 'pending', message_id: null, created_at: standard.created_at, decided_at: null, expires_at: null,
  delivery: { digest: '00'.repeat(32), scheme: 'schnorr-secp256k1-bip341-keypath' },
  card: { request_type: 'sign', secret_name: standard.name, kind: 'keypair', protection: 'standard' },
} satisfies VaultRequest;
const noop = () => {};

// A protected key's Bitcoin request: the card names the input from the persisted context (type, the
// requester's network label) and says the amounts are checked in the secure window. In the browser
// the card starts the vault sandbox, which no frame may load, so these are the desktop app's card
// (finished in the browser); in the browser only the bottom note and button differ.
const digest = '9c2e4b7a1f30d58e6a4b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f';
const protectedBtc = (scheme: string, signing_context: Record<string, unknown>): VaultRequest => ({
  ...request,
  id: `vrq_design_${String(signing_context.kind)}`,
  secret_name: 'PROTECTED_BTC_KEY',
  delivery: { digest, scheme, signing_context },
  card: { request_type: 'sign', secret_name: 'PROTECTED_BTC_KEY', kind: 'keypair', protection: 'protected' },
});
const ecdsaContext = (type: 'p2pkh' | 'p2wpkh', network: string) => ({
  kind: `bitcoin-${type}-v1`, addressType: type, network, sighashType: 1, digest,
  digestAlgorithm: type === 'p2pkh' ? 'bitcoin-legacy-sighash-all' : 'bitcoin-bip143-sighash-all',
});
const desktopFixtures = { ...fixtures, desktopShell: true };
const protectedCard = (r: VaultRequest) => () => <div className="p-6"><VaultApprovalCard onResolved={noop} onCancel={noop} request={r} /></div>;

const designFrames = frames([
  {
    name: 'capabilities', title: 'Vault · Bitcoin signing limitations',
    width: 1200, height: 800, status: 'shipped', route: '/vaults', fixtures,
    render: consolePage(<VaultsPage />),
  },
  {
    name: 'keypath', title: 'Vault · BIP86 approval identity',
    width: 640, height: 460, status: 'shipped', fixtures,
    render: () => <div className="p-6"><VaultApprovalCard onResolved={noop} onCancel={noop} request={request} /></div>,
  },
  {
    name: 'protected-keypath', title: 'Vault · Protected Bitcoin secure-window warning',
    width: 640, height: 180, status: 'shipped', fixtures,
    // The same view as the real approval card, without its custody/network container.
    render: () => <div className="p-6"><VaultSigningIdentity scheme="schnorr-secp256k1-bip341-keypath" addresses={standard.signing_addresses ?? null} protectedKey /></div>,
  },
  {
    name: 'raw', title: 'Vault · Raw BIP340 is not the Taproot address',
    width: 640, height: 460, status: 'shipped', fixtures,
    render: () => <div className="p-6"><VaultApprovalCard onResolved={noop} onCancel={noop} request={{ ...request, delivery: { ...request.delivery, scheme: 'schnorr-secp256k1-bip340' } }} /></div>,
  },
  {
    name: 'protected-btc-legacy', title: 'Vault · Protected Legacy (P2PKH) request: input type, network label, secure-window review (desktop app)',
    width: 640, height: 480, status: 'shipped', fixtures: desktopFixtures,
    render: protectedCard(protectedBtc('ecdsa-secp256k1-der', ecdsaContext('p2pkh', 'testnet'))),
  },
  {
    name: 'protected-btc-segwit', title: 'Vault · Protected SegWit (P2WPKH) request: input type, network label, secure-window review (desktop app)',
    width: 640, height: 480, status: 'shipped', fixtures: desktopFixtures,
    render: protectedCard(protectedBtc('ecdsa-secp256k1-der', ecdsaContext('p2wpkh', 'bitcoin'))),
  },
  {
    name: 'protected-btc-taproot', title: 'Vault · Protected Taproot request: input type, network label, secure-window review (desktop app)',
    width: 640, height: 480, status: 'shipped', fixtures: desktopFixtures,
    render: protectedCard(protectedBtc('schnorr-secp256k1-bip341-keypath', {
      kind: 'bitcoin-taproot-keypath-v1', network: 'signet', digestAlgorithm: 'bip341-sighash-default-keypath', digest,
    })),
  },
]);

export default designFrames;
