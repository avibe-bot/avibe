// Standard-tier Solana keys in the Vault (docs/plans/vault-solana.md §8), drawn before the consumer
// API lands: the Add dialog with a Chain choice, a Solana key in the list and in Edit, and the sign
// approval for exact message bytes. Existing copy is read from `src/i18n`; new copy is draft copy.
// The rows a Solana key does not change (name, description, tags, Advanced) are drawn only so the
// new ones sit where they will ship. Delete this file when the shipped components draw these states.
import type { ReactNode } from 'react';
import {
  Check,
  Copy,
  KeyRound,
  Lock,
  MoreHorizontal,
  PenTool,
  Pencil,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Wallet,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { SigningAddressList } from '@/components/ui/signing-address-list';
import { SegmentedRadio } from '@/components/ui/segmented';
import { Switch } from '@/components/ui/switch';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import type { SigningAddresses } from '@/context/ApiContext';
import { cn } from '@/lib/utils';

import { useDraft } from './draft';

const noop = () => {};
const FIELD_LABEL = 'text-[13px] font-medium text-foreground';

/** One address row, as `SigningAddressList` draws its rows. `whole` shows every character, wrapping
 *  where the row is narrow: an approval names the identity that signs, so it is never cut short. */
export function SolanaAddressRow({ address, whole = false }: { address: string; whole?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="w-[68px] shrink-0 text-[11px] font-medium text-muted">Solana</span>
      <code className={cn('min-w-0 flex-1 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11.5px] text-foreground', whole ? 'break-all' : 'truncate')} title={address}>
        {address}
      </code>
      <button type="button" aria-label={t('vaults.addresses.copy', { label: 'Solana' })} className="shrink-0 text-muted transition-colors hover:text-foreground">
        <Copy className="size-3.5" />
      </button>
    </div>
  );
}

type KeySource = 'generate' | 'import';
type Tier = 'standard' | 'protected';

/** The Add dialog with Signing key and Solana chosen. */
export function SolanaCreateDialog({
  source,
  tier = 'standard',
  importText = '',
  importRejected = false,
}: {
  source: KeySource;
  tier?: Tier;
  /** What the person pasted. Frames use text that is not a key. */
  importText?: string;
  /** avault refused the pasted text when the form was submitted. */
  importRejected?: boolean;
}) {
  const { t } = useTranslation();
  const d = useDraft();
  // Protected Solana keys arrive with the protected signing work; until then the tier cannot be used.
  const protectedBlocked = tier === 'protected';
  const canCreate = !protectedBlocked && (source === 'generate' || importText.trim().length > 0);
  const tiers = [
    { key: 'standard', icon: Server, title: t('vaults.dialog.standardProtection'), desc: t('vaults.dialog.standardHelp') },
    {
      key: 'protected',
      icon: ShieldCheck,
      title: t('vaults.dialog.protectedProtection'),
      desc: d('Solana 密钥暂不支持保护档。', "Protected isn't available for Solana keys yet."),
    },
  ] as const;
  return (
    <Dialog open onOpenChange={noop}>
      <DialogContent>
        <DialogTitle className="sr-only">{t('vaults.dialog.title')}</DialogTitle>
        <div className="flex min-w-0 items-start gap-3 pr-6">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent-ink">
            <KeyRound className="size-5" />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[15px] font-semibold text-foreground">{t('vaults.dialog.title')}</span>
            <span className="text-xs text-muted">{t('vaults.dialog.subtitle')}</span>
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.kindLabel')}</span>
            <SegmentedRadio
              value="keypair"
              onChange={noop}
              ariaLabel={t('vaults.dialog.kindLabel')}
              options={[
                { id: 'static', label: t('vaults.dialog.kindStatic') },
                { id: 'keypair', label: t('vaults.dialog.kindKeypair') },
              ]}
            />
          </div>

          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
            <Input value="SOL_DEVNET_KEY" readOnly className="font-mono" />
            <span className="text-[11px] text-muted">{t('vaults.dialog.nameHint')}</span>
          </label>

          {/* New: which chain family the key is for. It decides the curve and cannot change after saving. */}
          <div className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{d('链', 'Chain')}</span>
            <SegmentedRadio
              value="solana"
              onChange={noop}
              ariaLabel={d('链', 'Chain')}
              options={[
                { id: 'evm-bitcoin', label: 'EVM / Bitcoin' },
                { id: 'solana', label: 'Solana' },
              ]}
            />
          </div>

          <div className="flex flex-col gap-2.5 rounded-[10px] border border-edge bg-surface-2 px-3 py-3 shadow-elevation-0">
            <span className="text-xs text-muted">
              {d(
                'Solana 密钥由本机 avault 生成并就地封存，Avibe 和浏览器只会看到它的地址。',
                'avault creates the Solana key on this computer and seals it there. Avibe and this browser only see its address.',
              )}
            </span>
            <div className="grid grid-cols-2 gap-2">
              {(['generate', 'import'] as const).map((src) => (
                <Button key={src} type="button" size="sm" variant={source === src ? 'secondary' : 'ghost'}>
                  {src === 'generate' ? t('vaults.dialog.signingGenerate') : t('vaults.dialog.signingImport')}
                </Button>
              ))}
            </div>
            {source === 'generate' ? (
              <span className="text-xs text-muted">
                {d('保存时生成新密钥，保存后会显示地址。', 'A new key is created when you save. Its address appears once the key is saved.')}
              </span>
            ) : (
              <>
                <Textarea
                  value={importText}
                  readOnly
                  rows={4}
                  spellCheck={false}
                  autoComplete="off"
                  aria-label={t('vaults.dialog.signingImport')}
                  placeholder={d(
                    '粘贴 Solana CLI 密钥对（64 个数字的 JSON 数组）或 base58 私钥',
                    'Paste a Solana CLI keypair (a JSON array of 64 numbers) or a base58 secret key',
                  )}
                  className="font-mono text-xs"
                />
                {importRejected ? (
                  <span className="text-xs text-destructive-ink">{d('这不是有效的 Solana 私钥。', "This isn't a valid Solana secret key.")}</span>
                ) : (
                  <span className="text-xs text-muted">
                    {d(
                      '私钥在浏览器中加密后交给 avault 校验，保存后会显示地址。不支持助记词。',
                      'The key is encrypted in this browser for avault, which checks it. Its address appears once the key is saved. Recovery phrases are not supported.',
                    )}
                  </span>
                )}
              </>
            )}
          </div>

          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
            <Input value="" readOnly placeholder={t('vaults.dialog.descriptionPlaceholder')} />
          </label>

          <div className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.tags')}</span>
            <TagInput values={[]} onChange={noop} placeholder={t('vaults.dialog.tagsPlaceholder')} ariaLabel={t('vaults.dialog.tags')} />
            <span className="text-[11px] text-muted">{t('vaults.dialog.tagsHelp')}</span>
          </div>

          <div className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.protection')}</span>
            <div className="grid grid-cols-2 gap-2.5">
              {tiers.map(({ key, icon: Icon, title, desc }) => {
                const selected = tier === key;
                // Unavailable, and not chosen: it cannot be picked. Chosen before Solana was: it stays
                // chosen, and the form waits for the person to pick Standard themselves.
                const unavailable = key === 'protected' && !selected;
                return (
                  <button
                    key={key}
                    type="button"
                    aria-pressed={selected}
                    aria-disabled={unavailable || undefined}
                    className={cn(
                      'flex flex-col gap-1.5 rounded-[10px] border p-3 text-left',
                      selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-edge bg-field',
                      unavailable && 'opacity-55',
                    )}
                  >
                    <span className="flex items-center gap-2">
                      <Icon className={cn('size-4', selected ? 'text-mint-ink' : 'text-muted')} />
                      <span className="flex-1 text-[13.5px] font-semibold text-foreground">{title}</span>
                      {selected && <Check className="size-4 text-mint-ink" />}
                    </span>
                    <span className="text-[11.5px] leading-snug text-muted">{desc}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {protectedBlocked ? (
            <div role="alert" className="rounded-lg border border-gold/40 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
              {d('改选标准档即可创建这把密钥。', 'Choose Standard to create this key.')}
            </div>
          ) : (
            <div className="flex items-start gap-3 rounded-[10px] border border-edge bg-surface-2 px-3 py-3 shadow-elevation-0">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-[13px] font-semibold text-foreground">{t('vaults.dialog.alwaysAskSign')}</span>
                <span className="text-[11.5px] leading-snug text-muted">{t('vaults.dialog.alwaysAskSignHelp')}</span>
              </div>
              <Switch checked={false} onCheckedChange={noop} label={t('vaults.dialog.alwaysAskSign')} />
            </div>
          )}

          <div className="flex items-center gap-1.5 rounded-[10px] bg-surface-2 px-3 py-2.5">
            <SlidersHorizontal className="size-3.5 text-muted" />
            <span className="flex-1 text-xs font-semibold text-foreground">{t('vaults.dialog.advanced')}</span>
          </div>

          <div className="mt-1 flex justify-end gap-2">
            <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
            <Button type="button" disabled={!canCreate}>{t('vaults.dialog.createSecret')}</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** A signing key's row in the Vault list; `addresses` is whatever that key's curve derives. */
function VaultKeyRow({ name, description, usage, addresses, note }: {
  name: string;
  description: string;
  usage: string;
  addresses: ReactNode;
  note?: string;
}) {
  const { t } = useTranslation();
  return (
    <li className="flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-xl border border-edge bg-surface px-4 py-3 shadow-elevation-0 md:flex-nowrap">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-violet/10 text-violet-ink">
        <Wallet className="size-4" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-sm font-semibold">{name}</span>
          <Badge variant="secondary">{t('vaults.standard')}</Badge>
          <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
            <Wallet className="size-3" />
            {t('vaults.signing')}
          </Badge>
        </div>
        <span className="truncate text-xs text-muted">{description} · {usage}</span>
        <div className="mt-1 flex min-w-0 flex-col gap-1">{addresses}</div>
        {note ? <span role="note" className="mt-1 text-xs text-muted">{note}</span> : null}
      </div>
      <Button variant="ghost" size="icon" aria-label={t('vaults.rowActions')} className="shrink-0">
        <MoreHorizontal className="size-4" />
      </Button>
    </li>
  );
}

/** A Solana key beside an EVM / Bitcoin key in the Vault list: one Solana address, no Bitcoin notes. */
export function SolanaKeyList({ evmAddresses, solanaAddress }: { evmAddresses: SigningAddresses; solanaAddress: string }) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <ul className="flex flex-col gap-2 p-6">
      <VaultKeyRow
        name="ETH_HOT_KEY"
        description={d('测试网合约部署（公开的合成测试密钥，切勿充值）', 'Testnet contract deploys (public synthetic test key, never fund it)')}
        usage={t('vaults.used', { count: 3 })}
        addresses={<SigningAddressList addresses={evmAddresses} />}
        note={t('vaults.addresses.standardBitcoinTaprootWarning')}
      />
      <VaultKeyRow
        name="SOL_DEVNET_KEY"
        description={d('Devnet 自动化（公开的测试向量，切勿充值）', 'Devnet automation (public test vector, never fund it)')}
        usage={t('vaults.neverUsed')}
        addresses={<SolanaAddressRow address={solanaAddress} />}
      />
    </ul>
  );
}

/** The top of the Edit dialog for a Solana key: the chain is shown, and like kind and tier it is fixed. */
export function SolanaEditHeader({ name }: { name: string }) {
  const { t } = useTranslation();
  return (
    <Dialog open onOpenChange={noop}>
      <DialogContent>
        <DialogTitle className="sr-only">{t('vaults.edit.title')}</DialogTitle>
        <div className="flex min-w-0 items-start gap-3 pr-6">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent-ink">
            <Pencil className="size-5" />
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="text-[15px] font-semibold text-foreground">{t('vaults.edit.title')}</span>
            <span className="text-xs text-muted">{t('vaults.edit.subtitle')}</span>
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex items-center gap-3 rounded-xl border border-edge bg-surface-2 p-3.5 shadow-elevation-0">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted/10 text-muted">
              <Lock className="size-4" />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="truncate font-mono text-[15px] font-semibold text-foreground">{name}</span>
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant="secondary">{t('vaults.dialog.kindKeypair')}</Badge>
                <Badge variant="secondary">Solana</Badge>
                <Badge variant="secondary">{t('vaults.standard')}</Badge>
              </div>
            </div>
          </div>
          <p className="flex items-center gap-1.5 text-[11px] text-muted">
            <Lock className="size-3 shrink-0" />
            {t('vaults.edit.lockedHint')}
          </p>
          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.description')}</span>
            <Input value="" readOnly placeholder={t('vaults.dialog.descriptionPlaceholder')} />
          </label>
          <div className="mt-1 flex justify-end gap-2">
            <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
            <Button type="button">{t('vaults.dialog.save')}</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

const ROW = 'flex items-start gap-2.5 text-sm';
const ROW_LABEL = 'w-[74px] shrink-0 pt-0.5 text-[12px] text-muted';

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={ROW}>
      <span className={ROW_LABEL}>{label}</span>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">{children}</div>
    </div>
  );
}

/**
 * The approval for a standard Solana key that asks before every signature. The message is the exact
 * bytes to sign, as hex; an empty message is a valid one. Avibe decodes none of it, and says so.
 */
export function SolanaSignApproval({ keyName, address, session, messageHex }: {
  keyName: string;
  address: string;
  session: string;
  messageHex: string;
}) {
  const { t } = useTranslation();
  const d = useDraft();
  const bytes = messageHex.length / 2;
  return (
    <Dialog open onOpenChange={noop}>
      <DialogContent>
        <DialogTitle className="sr-only">{t('vaults.requests.reviewTitle')}</DialogTitle>
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex items-start gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-violet/15 text-violet-ink">
              <PenTool className="size-5" />
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[15px] font-semibold">{t('vaults.approval.signTitle')}</span>
              <span className="text-xs text-muted-foreground">{t('vaults.approval.signSubtitle')}</span>
            </div>
          </div>

          <div className="h-px bg-border" />

          <div className="flex flex-col gap-3.5">
            <DetailRow label={t('vaults.approval.key')}>
              <span className="truncate font-mono text-[13px] font-semibold">{keyName}</span>
              <Badge variant="secondary">{t('vaults.standard')}</Badge>
              <Badge variant="outline" className="border-violet/40 bg-violet-soft text-violet-ink">
                <Wallet className="size-3" />
                {t('vaults.signing')}
              </Badge>
            </DetailRow>
            <DetailRow label={t('vaults.approval.signingAs')}>
              <div className="min-w-0 flex-1"><SolanaAddressRow address={address} whole /></div>
            </DetailRow>
            <DetailRow label={t('vaults.approval.session')}>
              <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{session}</span>
            </DetailRow>
            <DetailRow label={d('待签名消息', 'Message')}>
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                {bytes > 0 ? (
                  <div className="flex min-w-0 items-start gap-2">
                    <code className="max-h-40 min-w-0 flex-1 overflow-y-auto break-all rounded-md bg-surface-2 px-2 py-1.5 font-mono text-[11.5px] leading-relaxed text-foreground">
                      {messageHex}
                    </code>
                    <button type="button" aria-label={d('复制待签名消息', 'Copy message')} className="shrink-0 pt-1 text-muted transition-colors hover:text-foreground">
                      <Copy className="size-3.5" />
                    </button>
                  </div>
                ) : (
                  <span className="text-[13px] text-foreground">{d('空消息', 'Empty message')}</span>
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant="secondary">ed25519</Badge>
                  <span className="text-[11.5px] text-muted">{d(`${bytes} 字节`, bytes === 1 ? '1 byte' : `${bytes} bytes`)}</span>
                </div>
              </div>
            </DetailRow>
          </div>

          {bytes > 0 ? (
            <div role="alert" className="rounded-lg border border-gold/40 bg-gold/10 px-3 py-2 text-xs text-gold-ink">
              {d('这条消息未解析，批准即为盲签。', 'This message is not decoded; approving it is blind signing.')}
            </div>
          ) : null}

          <div className="h-px bg-border" />

          <div className="flex items-center gap-3">
            <div className="ml-auto flex items-center gap-2">
              <Button type="button" variant="outline">{t('vaults.approval.deny')}</Button>
              <Button type="button">
                <PenTool className="size-4" />
                {t('vaults.approval.sign')}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
