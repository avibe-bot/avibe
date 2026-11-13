// Creating a seed-phrase wallet with an optional BIP39 passphrase (owner request; 3.2.7 proposal).
// Standard tier only here: the browser derives the seed from the words and the passphrase and seals
// it for avault, as import already does. The protected-tier create happens in the sandbox window.
// The passphrase is taken exactly as typed, with the same NFKD the import path applies; it is entered
// twice because, unlike an import, there is no older wallet to compare #0 against.
import { ChevronDown, ChevronRight, KeyRound, Sparkles, TriangleAlert } from 'lucide-react';

import { Input } from '@/components/ui/input';

import { useDraft } from '../draft';
import { Footer, SourceCards, WalletDialogHeading } from './create';
import { Notice, TierCards } from './parts';

export type PassphraseState = 'folded' | 'empty' | 'match' | 'mismatch';

/** The folded section under the source hint: empty by default, two fields when opened. */
export function CreatePassphrase({ state }: { state: PassphraseState }) {
  const d = useDraft();
  const open = state !== 'folded';
  const Chevron = open ? ChevronDown : ChevronRight;
  const filled = state === 'match' || state === 'mismatch';
  return (
    <div className="flex flex-col rounded-[10px] border border-edge bg-surface-2">
      <button type="button" aria-expanded={open} className="flex items-center gap-1.5 px-3 py-2.5 text-left">
        <Chevron className="size-3.5 text-muted" />
        <span className="flex-1 text-[12.5px] font-semibold text-foreground">{d('添加 Passphrase（可选）', 'Add a passphrase (optional)')}</span>
        {!open ? <span className="text-[11px] text-muted">{d('大多数人不需要', 'Most people don’t need one')}</span> : null}
      </button>
      {open ? (
        <div className="flex flex-col gap-2.5 px-3 pb-3">
          <span className="text-[11.5px] leading-snug text-muted">
            {d(
              'Passphrase 和这些词一起决定钱包的地址。恢复时需要词，加上一字不差的 passphrase；忘了它，钱包就找不回来。',
              'The passphrase and the words together decide the wallet’s addresses. Recovering it needs the words plus this exact passphrase; forget it and the wallet can’t be recovered.',
            )}
          </span>
          <label className="flex flex-col gap-1">
            <span className="text-[12px] font-medium text-foreground">{d('Passphrase', 'Passphrase')}</span>
            <Input type="password" readOnly value={filled ? 'correct horse battery' : ''} autoComplete="off" aria-label="passphrase" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[12px] font-medium text-foreground">{d('再输入一次', 'Enter it again')}</span>
            <Input
              type="password"
              readOnly
              value={state === 'match' ? 'correct horse battery' : state === 'mismatch' ? 'correct horse batery' : ''}
              autoComplete="off"
              aria-label="passphrase again"
              aria-invalid={state === 'mismatch' || undefined}
            />
          </label>
          {state === 'mismatch' ? (
            <span role="alert" className="text-[11.5px] text-destructive-ink">{d('两次输入不一样。', 'The two entries don’t match.')}</span>
          ) : state === 'match' ? (
            <span className="text-[11.5px] text-mint-ink">{d('两次输入一致。区分大小写和空格。', 'They match. Case and spaces count.')}</span>
          ) : (
            <span className="text-[11.5px] text-muted">{d('区分大小写和空格，原样保留。两栏都空着就是不设置。', 'Case and spaces count and are kept as typed. Leave both empty for none.')}</span>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** Set up with a passphrase: the setup panel as the Add dialog draws it, with the section added. */
export function SetupWithPassphrase({ state }: { state: PassphraseState }) {
  const d = useDraft();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <WalletDialogHeading />
      <SourceCards value="create" />
      <CreatePassphrase state={state} />
      <TierCards value="standard" />
      <Footer primary={d('生成助记词', 'Generate the words')} disabled={state === 'mismatch'} primaryIcon={<Sparkles className="size-4" />} />
    </div>
  );
}

/** On the write-down step: the words alone are not the backup. */
export function PassphraseBackupReminder() {
  const d = useDraft();
  return (
    <Notice tone="warning" icon={KeyRound}>
      {d(
        '这个钱包还设置了 passphrase。把它和这些词分开记下：恢复时两样都要，passphrase 要一字不差。',
        'This wallet also has a passphrase. Write it down separately from the words: recovering needs both, and the passphrase must be exact.',
      )}
    </Notice>
  );
}

/** The done screen's recovery line when a passphrase was set (replaces the generic one). */
export function PassphraseRecoveryLine() {
  const d = useDraft();
  return (
    <Notice tone="warning" icon={TriangleAlert}>
      {d(
        '这台电脑丢失时，只有这 12 个词加上你设置的 passphrase（一字不差）能找回资金。只靠数据库备份无法恢复签名。',
        'If this computer is lost, only these 12 words plus the passphrase you set (exactly) can recover the funds. A database backup alone cannot restore signing.',
      )}
    </Notice>
  );
}
