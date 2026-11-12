// Importing an existing phrase (ui/design: vault/mnemonic/import/*): paste, see each word checked
// against the BIP39 list as it lands, optionally add the passphrase, and compare account #0's
// addresses with the old wallet before anything is saved. A wrong passphrase is never an error,
// only different addresses, so the preview is the check.
import { AlertTriangle, ArrowUpCircle, ChevronDown, ChevronRight, CircleCheck, EyeOff, FileDown, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { SigningAddressList } from '@/components/ui/signing-address-list';
import { Textarea } from '@/components/ui/textarea';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import { IMPORTED_ADDRESSES, IMPORTED_WORDS } from './data';
import { ChainRow, SourceCards, WalletDialogHeading } from './create';
import { FIELD_LABEL, Notice, TierCards } from './parts';

export type ImportState = 'empty' | 'valid' | 'unknown-word' | 'checksum' | 'avault-old';

const WORDS: Record<Exclude<ImportState, 'empty'>, string[]> = {
  valid: IMPORTED_WORDS,
  'avault-old': IMPORTED_WORDS,
  'unknown-word': IMPORTED_WORDS.map((word, index) => (index === 6 ? 'abandn' : word)),
  // Every word is on the list; the last one breaks the checksum.
  checksum: IMPORTED_WORDS.map((word, index) => (index === 11 ? 'abandon' : word)),
};

function ParsedWords({ words, bad }: { words: string[]; bad?: number }) {
  return (
    <ol className="grid grid-cols-3 gap-1.5 max-md:grid-cols-2">
      {words.map((word, index) => (
        <li
          key={index}
          className={cn(
            'flex min-w-0 items-baseline gap-2 rounded-lg border px-2.5 py-1.5',
            bad === index ? 'border-[1.5px] border-destructive bg-destructive/10' : 'border-edge bg-field',
          )}
        >
          <span className="w-4 shrink-0 text-right font-mono text-[10.5px] text-muted">{index + 1}</span>
          <span className={cn('truncate font-mono text-[13px] font-semibold', bad === index ? 'text-destructive-ink' : 'text-foreground')}>{word}</span>
        </li>
      ))}
    </ol>
  );
}

function Status({ state }: { state: Exclude<ImportState, 'empty'> }) {
  const d = useDraft();
  if (state === 'unknown-word') {
    return (
      <Notice tone="error" icon={X}>
        <div className="flex flex-col gap-2">
          <span>
            <span className="font-semibold text-destructive-ink">{d('第 7 个词“abandn”不在助记词词表里。', 'Word #7, “abandn”, is not a seed phrase word.')}</span>{' '}
            {d('是不是“abandon”？', 'Did you mean “abandon”?')}
          </span>
          <span><Button type="button" size="xs" variant="secondary">{d('改成 abandon', 'Use abandon')}</Button></span>
        </div>
      </Notice>
    );
  }
  if (state === 'checksum') {
    return (
      <Notice tone="error" icon={AlertTriangle}>
        <span className="font-semibold text-destructive-ink">{d('12 个词都对得上词表，但合在一起校验不通过。', 'All 12 words are valid, but together they fail the checksum.')}</span>{' '}
        {d('通常是某个词抄成了另一个有效的词，或两个词顺序颠倒。请逐个对照原件。', 'Usually one word was copied as another valid word, or two are swapped. Compare each word with your original.')}
      </Notice>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-[12px] font-medium text-mint-ink">
      <CircleCheck className="size-3.5" />
      {d('12 个词 · 校验通过', '12 words · checksum OK')}
    </span>
  );
}

function Passphrase({ open }: { open: boolean }) {
  const d = useDraft();
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="flex flex-col rounded-[10px] border border-edge bg-surface-2">
      <button type="button" aria-expanded={open} className="flex items-center gap-1.5 px-3 py-2.5 text-left">
        <Chevron className="size-3.5 text-muted" />
        <span className="flex-1 text-[12.5px] font-semibold text-foreground">{d('BIP39 密码（passphrase，可选）', 'BIP39 passphrase (optional)')}</span>
        {!open ? <span className="text-[11px] text-muted">{d('大多数钱包没有设置', 'Most wallets don’t use one')}</span> : null}
      </button>
      {open ? (
        <div className="flex flex-col gap-2 px-3 pb-3">
          <Input type="password" value="correct horse" readOnly aria-label={d('BIP39 密码', 'BIP39 passphrase')} />
          <span className="text-[11.5px] leading-snug text-muted">
            {d('只有原钱包设置过才填。填错不会报错，只会得到另一组地址，所以请对照下面的地址。', 'Only if your old wallet used one. A wrong passphrase gives no error, just different addresses, so check them below.')}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function Preview() {
  const d = useDraft();
  return (
    <div className="flex flex-col gap-2 rounded-[10px] border border-mint/30 bg-mint/[0.06] p-3">
      <span className="flex items-center gap-2 text-[12.5px] font-semibold text-foreground">
        <span className="rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] text-violet-ink">#0</span>
        {d('和原钱包的第一个账户核对', 'Compare with your old wallet’s first account')}
      </span>
      <SigningAddressList addresses={IMPORTED_ADDRESSES[0]} />
      <span className="text-[11px] leading-snug text-muted">
        {d('MetaMask 的 Account 1、Phantom 的第一个账户应显示相同的 EVM 和 Solana 地址。Taproot 地址只在 Phantom 等支持 Taproot 的钱包里看得到。', 'MetaMask’s Account 1 and Phantom’s first account show the same EVM and Solana addresses. The Taproot address shows only in wallets that support Taproot, such as Phantom.')}
      </span>
    </div>
  );
}

/** The Add dialog importing a phrase on the standard tier. */
export function ImportForm({ state, passphraseOpen = false, scrolled = false }: {
  state: ImportState;
  passphraseOpen?: boolean;
  /** A phone scrolled past the type, name and source, to the words and the addresses. */
  scrolled?: boolean;
}) {
  const { t } = useTranslation();
  const d = useDraft();
  const words = state === 'empty' ? [] : WORDS[state];
  const ready = state === 'valid';
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {scrolled ? (
        <span className="pr-8 text-[14px] font-semibold text-foreground">
          {d('导入助记词', 'Import a seed phrase')} · <span className="font-mono">AGENT_WALLET</span>
        </span>
      ) : (
        <>
          <WalletDialogHeading />
          <ChainRow />
          <label className="flex flex-col gap-1.5">
            <span className={FIELD_LABEL}>{t('vaults.dialog.name')}</span>
            <Input value="AGENT_WALLET" readOnly className="font-mono" />
          </label>
          <SourceCards value="import" />
        </>
      )}
      <div className="flex flex-col gap-2">
        <span className="flex items-center gap-2">
          <span className={cn(FIELD_LABEL, 'flex-1')}>{d('粘贴助记词', 'Paste the phrase')}</span>
          {words.length ? (
            <Button type="button" variant="ghost" size="xs">
              <EyeOff className="size-3.5" />
              {d('隐藏', 'Hide')}
            </Button>
          ) : null}
        </span>
        {words.length === 0 ? (
          <>
            <Textarea
              value=""
              readOnly
              rows={4}
              spellCheck={false}
              autoComplete="off"
              placeholder={d('12 或 24 个英文单词，用空格分开。粘贴后会逐个检查。', '12 or 24 English words separated by spaces. Each word is checked as you paste.')}
              className="font-mono text-xs"
            />
            <span className="text-[11px] text-muted">{d('助记词只在这台电脑上处理，不会进入聊天或模型。', 'The phrase stays on this computer and never reaches a chat or a model.')}</span>
          </>
        ) : (
          <>
            <ParsedWords words={words} bad={state === 'unknown-word' ? 6 : undefined} />
            <span className="text-[11px] text-muted">{d('点任一个词可以直接修改。', 'Tap any word to fix it.')}</span>
            {state !== 'empty' ? <Status state={state === 'avault-old' ? 'valid' : state} /> : null}
          </>
        )}
      </div>
      {words.length ? <Passphrase open={passphraseOpen} /> : null}
      {state === 'valid' || state === 'avault-old' ? <Preview /> : null}
      {scrolled ? null : <TierCards value="standard" />}
      {state === 'avault-old' ? (
        <Notice tone="warning" icon={ArrowUpCircle}>
          <div className="flex flex-col gap-2">
            <span>
              <span className="font-semibold text-gold-ink">{d('助记词钱包需要 avault 0.1.11 或更新版本，当前是 0.1.10。', 'Seed phrase wallets need avault 0.1.11 or later; 0.1.10 is installed.')}</span>{' '}
              {d('更新后回到这里即可保存，已填写的内容会保留。现有密钥不受影响。', 'Update, then come back to save; what you entered stays. Your existing keys are unaffected.')}
            </span>
            <span><Button type="button" size="xs" variant="secondary">{d('前往设置 → 依赖', 'Open Settings → Dependencies')}</Button></span>
          </div>
        </Notice>
      ) : null}
      <div className="mt-1 flex justify-end gap-2">
        <Button type="button" variant="ghost">{t('vaults.dialog.cancel')}</Button>
        <Button type="button" disabled={!ready}>
          <FileDown className="size-4" />
          {d('导入钱包', 'Import wallet')}
        </Button>
      </div>
    </div>
  );
}

