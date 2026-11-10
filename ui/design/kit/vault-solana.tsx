// What a standard-tier Solana key adds to the Vault (docs/plans/vault-solana.md §8), drawn before the
// consumer API is on the default branch. Only the new parts live here: the Chain choice and Solana key
// builder of the Add dialog, the notice a Protected choice gets, and the message a sign approval shows.
// Everything else on those surfaces is the shipped component and is not redrawn. New copy is draft copy.
// Delete this file when the shipped form and approval card draw these states.
import { ChevronDown, ChevronRight, Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { SegmentedRadio } from '@/components/ui/segmented';
import { SigningAddressRow } from '@/components/ui/signing-address-list';
import { Textarea } from '@/components/ui/textarea';
import { DetailRow } from '@/components/ui/vault-approval-card';

import { useDraft } from './draft';

const noop = () => {};

/**
 * The Add dialog's signing-key builder with Solana chosen. It sits where the secp256k1 builder sits
 * today, below Name. avault creates or checks the key, so nothing here shows an address before saving.
 */
export function SolanaKeyBuilder({ source, importText = '', importRejected = false }: {
  source: 'generate' | 'import';
  /** What the person pasted. Frames use text that is not a key. */
  importText?: string;
  /** avault refused the pasted text when the form was submitted. */
  importRejected?: boolean;
}) {
  const { t } = useTranslation();
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {/* Which chain family the key is for. It decides the curve and cannot change after saving. */}
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-foreground">{d('链', 'Chain')}</span>
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

      <div className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-2 p-3 shadow-elevation-0">
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
    </div>
  );
}

/**
 * Shown under the tier cards while Protected is chosen for a Solana key, until protected Solana keys
 * ship. The tier is not changed for the person: Create stays unavailable until they pick Standard.
 */
export function SolanaProtectedNotice() {
  const d = useDraft();
  return (
    <div role="alert" className="rounded-lg border border-gold/40 bg-gold/10 px-3 py-2 text-sm text-gold-ink">
      {d('Solana 密钥暂不支持保护档。改选标准档即可创建这把密钥。', "Protected isn't available for Solana keys yet. Choose Standard to create this key.")}
    </div>
  );
}

/**
 * What the sign approval shows for a standard Solana key that asks before every signature, in place
 * of the hash row: the address that signs and the size of the message, a notice that the message is
 * not decoded, and one Details section with the exact bytes. The bytes are never the summary, however
 * long the message is; Copy takes all of them whether or not Details is open. An empty message is a
 * valid message: it has no bytes to show and nothing left undecoded.
 */
export function SolanaSignRows({ address, messageHex, details = false }: {
  address: string;
  /** The exact message bytes, as hex. */
  messageHex: string;
  /** Details is open. */
  details?: boolean;
}) {
  const { t } = useTranslation();
  const d = useDraft();
  const bytes = messageHex.length / 2;
  const copyLabel = d('复制待签名消息', 'Copy message');
  const Chevron = details ? ChevronDown : ChevronRight;
  return (
    <div className="flex min-w-0 flex-col gap-3.5">
      <DetailRow label={t('vaults.approval.signingAs')}>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <SigningAddressRow label="Solana" value={address} />
        </div>
      </DetailRow>
      <DetailRow label={d('待签名消息', 'Message')}>
        <span className="text-sm text-foreground">
          {bytes > 0 ? d(`${bytes} 字节`, bytes === 1 ? '1 byte' : `${bytes} bytes`) : d('空消息（0 字节）', 'Empty message (0 bytes)')}
        </span>
        <Badge variant="secondary">ed25519</Badge>
        {bytes > 0 ? (
          <button type="button" aria-label={copyLabel} className="ml-auto shrink-0 text-muted transition-colors hover:text-foreground">
            <Copy className="size-3.5" />
          </button>
        ) : null}
      </DetailRow>
      {bytes > 0 ? (
        <>
          <div role="alert" className="rounded-lg border border-gold/40 bg-gold/10 px-3 py-2 text-xs text-gold-ink">
            {d('这条消息未解析，批准即为盲签。', 'This message is not decoded; approving it is blind signing.')}
          </div>
          <div className="flex min-w-0 flex-col rounded-lg bg-surface-2">
            <button type="button" aria-expanded={details} className="flex items-center gap-1.5 px-3 py-2.5 text-left">
              <Chevron className="size-3.5 text-muted" />
              <span className="flex-1 text-xs font-semibold text-foreground">{d('详情', 'Details')}</span>
            </button>
            {details ? (
              <div className="flex min-w-0 flex-col gap-1.5 px-3 pb-3">
                <span className="text-xs text-muted">{d('完整的待签名字节（十六进制）', 'Exact bytes to sign (hex)')}</span>
                <code className="max-h-40 min-w-0 overflow-y-auto break-all rounded-md bg-surface px-2 py-1.5 font-mono text-xs leading-relaxed text-foreground">
                  {messageHex}
                </code>
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
