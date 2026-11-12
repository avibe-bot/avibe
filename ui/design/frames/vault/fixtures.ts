// The Vault reads the shipped page and its dialogs make (`/api/vault/*`), served through the design
// network stub: a household of secrets, the grants an agent holds right now, and the requests agents
// are waiting on.
import type { SigningAddresses, VaultAuditEvent, VaultGrant, VaultRequest, VaultSecret } from '@/context/ApiContext';

import { DESIGN_NOW } from '../../shell/clock';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { workbenchFixtures } from '../workbench/fixtures';

const ago = (minutes: number) => new Date(DESIGN_NOW - minutes * 60_000).toISOString();
const inSeconds = (seconds: number) => new Date(DESIGN_NOW + seconds * 1000).toISOString();

// Addresses of the public synthetic test key (private key 1): never a real wallet.
export const syntheticAddresses: SigningAddresses = {
  eth: '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf',
  btc_legacy: '1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH',
  btc_segwit: 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4',
  btc_taproot: 'bc1pmfr3p9j00pfxjh0zmgp99y8zftmd3s5pmedqhyptwy6lm87hf5sspknck9',
};

export const secret = (name: string, extra: Partial<VaultSecret> = {}): VaultSecret => ({
  name,
  // A one-value secret, as the daemon lists it; a keypair has no fields.
  ...(extra.kind === 'keypair' ? {} : { default_field: 'value', fields: ['value'] }),
  tags: [],
  kind: 'static',
  protection: 'standard',
  signer_kind: null,
  source: 'user',
  description: null,
  policy: {},
  last_used_at: null,
  use_count: 0,
  created_at: ago(60 * 24 * 30),
  updated_at: ago(60 * 24 * 3),
  ...extra,
});

export const vaultSecrets: VaultSecret[] = [
  secret('OPENAI_API_KEY', {
    description: 'OpenAI 主账号',
    tags: ['ai'],
    policy: { allowed_hosts: ['api.openai.com'] },
    use_count: 142,
    last_used_at: ago(120),
  }),
  secret('GITHUB_TOKEN', {
    description: '提 PR 和读 CI 用的细粒度 token',
    tags: ['skill:pr-review', 'dev'],
    policy: { allowed_hosts: ['api.github.com', 'uploads.github.com'] },
    use_count: 12,
    last_used_at: ago(60 * 26),
  }),
  secret('AWS_DEPLOY', {
    description: '部署到 ap-east-1 的 IAM 用户',
    tags: ['skill:deploy-aws', 'prod'],
    default_field: 'access_key_id',
    fields: ['access_key_id', 'secret_access_key'],
    use_count: 31,
    last_used_at: ago(60 * 5),
  }),
  secret('STRIPE_LIVE_KEY', {
    protection: 'protected',
    description: '线上收款账号，每次使用都要批准',
    tags: ['payments', 'prod'],
    policy: { allowed_hosts: ['api.stripe.com'], always_ask: true },
  }),
  secret('ETH_HOT_KEY', {
    kind: 'keypair',
    signer_kind: 'local',
    description: '测试网合约部署（公开的合成测试密钥，切勿充值）',
    tags: ['crypto'],
    signing_addresses: syntheticAddresses,
    use_count: 3,
    last_used_at: ago(60 * 24 * 5),
  }),
];

export const grant = (id: string, selector: VaultGrant['source_selector'], members: string[], secondsLeft: number, extra: Partial<VaultGrant> = {}): VaultGrant => ({
  id,
  source_selector: selector,
  session_id: 'ses_deploy',
  purpose: 'access',
  status: 'active',
  request_id: null,
  created_at: ago(3),
  expires_at: inSeconds(secondsLeft),
  revoked_at: null,
  member_snapshot: members,
  member_count: members.length,
  runtime_member_count: members.length,
  ...extra,
});

export const vaultGrants: VaultGrant[] = [
  grant('grt_deploy', { tags: ['skill:deploy-aws'] }, ['AWS_DEPLOY'], 12 * 60 + 34),
  grant('grt_payments', { tags: ['payments'] }, ['STRIPE_LIVE_KEY'], 9 * 60 + 58, { session_id: null }),
];

const session = (id: string, title: string) => ({ id, title, label: null, platform: 'avibe', scope_kind: 'project', is_workbench: true });

const request = (id: string, type: string, secretName: string | null, card: Record<string, unknown>, extra: Partial<VaultRequest> = {}): VaultRequest => ({
  id,
  request_type: type,
  secret_name: secretName,
  requester: {},
  delivery: {},
  status: 'pending',
  message_id: null,
  created_at: ago(2),
  decided_at: null,
  expires_at: inSeconds(15 * 60),
  card: { request_type: type, secret_name: secretName, ...card },
  ...extra,
});

/** An agent asks for a secret that does not exist yet (`$ANTHROPIC_API_KEY`). */
export const provisionRequest = request('vrq_provision', 'provision', 'ANTHROPIC_API_KEY', {
  kind: 'static',
  default_protection: 'standard',
  spec: { description: 'Claude 备用通道', tags: ['ai'], policy: { allowed_hosts: ['api.anthropic.com'] } },
}, { session: session('ses_relay', '中转节点限流') });

export const generatedProvisionRequest = request('vrq_provision_generated', 'provision', 'DEPLOYMENT_ROOT_KEY', {
  kind: 'static',
  default_protection: 'standard',
  spec: { protection: 'standard', generate: { length: 64, charset: 'hex' } },
}, { session: session('ses_deploy', '部署密钥轮换') });

/** An agent runs a command with an always-ask standard secret. */
export const accessRequest = request('vrq_access', 'access', 'OPENAI_API_KEY', {
  kind: 'static',
  protection: 'standard',
  command: 'python scripts/sync_embeddings.py --since 2026-10-01',
  egress: 'api.openai.com',
  source_selector: { env: ['OPENAI_API_KEY'] },
  grant_options: [{ grant_id: 'grt_req_access', default_ttl_seconds: 300, session_binding_default: true, member_count: 1, member_snapshot: ['OPENAI_API_KEY'] }],
}, { session: session('ses_sync', '向量库每日同步') });

/** A skill asks for every secret it is tagged with. */
export const skillAccessRequest = request('vrq_skill', 'access', null, {
  kind: 'static',
  protection: 'standard',
  secret_names: ['AWS_DEPLOY', 'GITHUB_TOKEN'],
  command: 'bash deploy.sh --env prod --region ap-east-1',
  egress: 'sts.ap-east-1.amazonaws.com',
  source_selector: { tags: ['skill:deploy-aws'] },
  grant_options: [{
    grant_id: 'grt_req_skill',
    default_ttl_seconds: 900,
    session_binding_default: true,
    member_count: 2,
    member_snapshot: ['AWS_DEPLOY', 'GITHUB_TOKEN'],
    member_fields: [{ name: 'AWS_DEPLOY', default_field: 'access_key_id', fields: ['access_key_id', 'secret_access_key'] }],
    source_selector: { tags: ['skill:deploy-aws'] },
  }],
}, { session: session('ses_deploy', '上线 v3.2.5') });

/** An agent asks to sign an Ethereum transaction with a standard signing key. */
export const signRequest = request('vrq_sign', 'sign', 'ETH_HOT_KEY', {
  kind: 'keypair',
  protection: 'standard',
}, {
  session: session('ses_contract', '测试网部署合约'),
  delivery: { digest: '5f0b6e2a91c4d7e83b1a26f4c09d8e7712a3b4c5d6e7f8091a2b3c4d5e6f7081', scheme: 'ecdsa-secp256k1-recoverable' },
});

/** An agent asks to delete a secret it created. */
export const deleteRequest = request('vrq_delete', 'delete', 'GITHUB_TOKEN', {
  kind: 'static',
  protection: 'standard',
  description: '提 PR 和读 CI 用的细粒度 token',
  reason: '这个 token 已在 GitHub 上吊销，换成了新的 GITHUB_PR_TOKEN。',
  tags: ['skill:pr-review', 'dev'],
}, { session: session('ses_rotate', '轮换 GitHub token') });

export const vaultRequests = [provisionRequest, accessRequest, signRequest];

const auditEvents: VaultAuditEvent[] = [
  ['grant.created', 'AWS_DEPLOY', 3],
  ['secret.used', 'OPENAI_API_KEY', 120],
  ['secret.used', 'AWS_DEPLOY', 300],
  ['sign.approved', 'ETH_HOT_KEY', 60 * 24 * 5],
  ['secret.created', 'STRIPE_LIVE_KEY', 60 * 24 * 9],
].map(([event, name, minutes], index) => ({
  id: `vae_${index}`,
  ts: ago(minutes as number),
  event: event as string,
  secret_name: name as string,
  request_id: null,
  grant_id: null,
}));

const policy = { windowSeconds: 600, strictApprovals: false, parentValueSealAllowed: true };

/** Every Vault read, with these secrets, grants, and pending requests. */
export function vaultApi({
  secrets = vaultSecrets,
  grants = vaultGrants,
  requests = vaultRequests,
}: { secrets?: VaultSecret[]; grants?: VaultGrant[]; requests?: VaultRequest[] } = {}): Record<string, unknown> {
  return {
    '/api/vault/secrets': { ok: true, secrets },
    '/api/vault/grants': { ok: true, grants },
    '/api/vault/requests': { ok: true, requests },
    '/api/vault/audit': { ok: true, events: auditEvents },
    '/api/vault/avault-authorization': { ok: true, required: false, detected_at: null, attempt: null },
    '/api/vault/settings': { ok: true, settings: { unlock_window_seconds: 600, strict_approvals: false, last_grant_ttl: 300 }, policy },
    '/api/vault/signing-addresses': { ok: true, addresses: syntheticAddresses },
    // The create form checks avault's version before it lets a standard secret be saved.
    '/api/dependencies': { deps: [{ id: 'avault', kind: 'tool', required: true, installed: true, version: '0.1.6', status: 'ready' }] },
    // Tag suggestions offer each global skill as a `skill:<name>` tag.
    '/api/skills': { ok: true, skills: [{ name: 'deploy-aws' }, { name: 'pr-review' }, { name: 'pdf-tools' }] },
  };
}

/** A running install with the demo projects and Bots, and this Vault. */
export const vaultFixtures = (api: Record<string, unknown> = vaultApi(), extra: WorkbenchFixtures = {}): WorkbenchFixtures =>
  workbenchFixtures(api, extra);
