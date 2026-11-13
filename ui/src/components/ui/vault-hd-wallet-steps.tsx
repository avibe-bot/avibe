// The seed-phrase wallet's own parts of the Add key dialog (ui/design: vault/mnemonic/*): the words
// grid, the three-step progress, the backup and check steps, the done screen, and the import panel.
// The dialog (vault-secret-form) owns the state and the submit; these draw it.
import { useMemo, useState } from 'react';
import type { ComponentType, ReactNode } from 'react';
import {
  AlertTriangle, ArrowLeft, ArrowUpCircle, Bot, Check, ChevronDown, ChevronRight, CircleCheck, Copy, Eye, EyeOff, Loader2, WalletCards, X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { HdAccountAddresses, HdQuestion, PhraseCheck } from '@/lib/vaultHdWallet';
import { cn, copyTextToClipboard } from '@/lib/utils';
import { Button } from './button';
import { Input } from './input';
import { SigningAddressRow } from './signing-address-list';
import { InfoHint } from './info-hint';
import { Textarea } from './textarea';

/** A one-line notice in the dialog. */
export function HdNotice({ tone, icon: Icon, children }: {
  tone: 'warning' | 'error' | 'violet';
  icon: ComponentType<{ className?: string }>;
  children: ReactNode;
}) {
  const box = { warning: 'border-gold/40 bg-gold/10', error: 'border-destructive/40 bg-destructive/10', violet: 'border-violet/30 bg-violet-soft' }[tone];
  const ink = { warning: 'text-gold-ink', error: 'text-destructive-ink', violet: 'text-violet-ink' }[tone];
  return (
    <div role={tone === 'error' ? 'alert' : 'note'} className={cn('flex items-start gap-2.5 rounded-[10px] border px-3 py-2.5 text-[12.5px] leading-snug text-foreground', box)}>
      <Icon className={cn('mt-0.5 size-4 shrink-0', ink)} />
      <div className="flex min-w-0 flex-1 flex-wrap gap-x-1">{children}</div>
    </div>
  );
}

/** Where the user is in creating a wallet: three short steps, so the length is known up front. */
export function HdStepper({ step }: { step: 1 | 2 | 3 }) {
  const { t } = useTranslation();
  const labels = [t('vaults.hd.steps.setup'), t('vaults.hd.steps.backup'), t('vaults.hd.steps.check')];
  return (
    <ol className="flex items-center gap-2" aria-label={t('vaults.hd.steps.label')}>
      {labels.map((label, index) => {
        const n = index + 1;
        const done = n < step;
        const current = n === step;
        return (
          <li key={label} aria-current={current ? 'step' : undefined} className="flex min-w-0 flex-1 items-center gap-2">
            <span className={cn(
              'flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold',
              done ? 'bg-mint text-primary-foreground' : current ? 'border-[1.5px] border-mint text-mint-ink' : 'border border-border-strong text-muted',
            )}>
              {done ? <Check className="size-3" strokeWidth={3} /> : n}
            </span>
            <span className={cn('truncate text-xs', current ? 'font-semibold text-foreground' : 'text-muted')}>{label}</span>
            {n < 3 ? <span className="h-px min-w-3 flex-1 bg-border" /> : null}
          </li>
        );
      })}
    </ol>
  );
}

/** The words in order, numbered as a paper backup lists them. */
export function WordGrid({ words, hidden, bad, onReveal }: { words: string[]; hidden?: boolean; bad?: number; onReveal?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="relative">
      <ol
        aria-label={t('vaults.hd.steps.backup')}
        aria-hidden={hidden || undefined}
        className={cn('grid grid-cols-3 gap-1.5 max-sm:grid-cols-2', hidden && 'pointer-events-none select-none blur-[7px]')}
      >
        {words.map((word, index) => (
          <li
            key={index}
            className={cn(
              'flex min-w-0 items-baseline gap-2 rounded-lg border px-2.5 py-2',
              bad === index ? 'border-[1.5px] border-destructive bg-destructive/10' : 'border-edge bg-field',
            )}
          >
            <span className="w-4 shrink-0 text-right font-mono text-[10.5px] text-muted">{index + 1}</span>
            <span className={cn('truncate font-mono text-[13.5px] font-semibold', bad === index ? 'text-destructive-ink' : 'text-foreground')}>
              {hidden ? t('vaults.hd.hiddenWord') : word}
            </span>
          </li>
        ))}
      </ol>
      {hidden ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
          <Button type="button" variant="secondary" className="shadow-elevation-1" onClick={onReveal}>
            <Eye className="size-4" />
            {t('vaults.hd.backup.show', { count: words.length })}
          </Button>
          <span className="rounded-full bg-popover/80 px-2.5 py-0.5 text-[11.5px] text-muted">{t('vaults.hd.backup.screen')}</span>
        </div>
      ) : null}
    </div>
  );
}

/** Step 2: the words, hidden until asked for. */
export function HdBackupStep({ words }: { words: string[] }) {
  const { t } = useTranslation();
  const [hidden, setHidden] = useState(true);
  const [copied, setCopied] = useState(false);
  return (
    <>
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-semibold text-foreground">{t('vaults.hd.backup.title', { count: words.length })}</span>
        <span className="text-[12.5px] leading-snug text-muted">{t('vaults.hd.backup.body')}</span>
      </div>
      <WordGrid words={words} hidden={hidden} onReveal={() => setHidden(false)} />
      {!hidden ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => {
              // Same fallback as other Vault copies (LAN HTTP has no Clipboard API); "copied" only on success.
              void copyTextToClipboard(words.join(' ')).then((ok) => setCopied(ok));
            }}
          >
            <Copy className="size-3.5" />
            {copied ? t('vaults.hd.backup.copied') : t('vaults.hd.backup.copy')}
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setHidden(true)}>
            <EyeOff className="size-3.5" />
            {t('vaults.hd.backup.hide')}
          </Button>
          <span className="text-[11px] text-muted">{t('vaults.hd.backup.clipboard')}</span>
        </div>
      ) : null}
      <HdNotice tone="violet" icon={Bot}>{t('vaults.hd.backup.agentNote')}</HdNotice>
    </>
  );
}

/** Step 3: three quick taps against the paper copy. */
export function HdCheckStep({ quiz, chosen, onChoose }: {
  quiz: HdQuestion[];
  chosen: Record<number, string>;
  onChoose: (position: number, word: string) => void;
}) {
  const { t } = useTranslation();
  const wrong = quiz.find((q) => chosen[q.position] && chosen[q.position] !== q.answer);
  return (
    <>
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-semibold text-foreground">{t('vaults.hd.check.title')}</span>
        <span className="text-[12.5px] leading-snug text-muted">{t('vaults.hd.check.body')}</span>
      </div>
      <div className="flex flex-col gap-3">
        {quiz.map((q) => (
          <div key={q.position} role="radiogroup" aria-label={t('vaults.hd.check.word', { position: q.position })} className="flex flex-col gap-1.5">
            <span className="text-[12.5px] text-muted">{t('vaults.hd.check.word', { position: q.position })}</span>
            <div className="grid grid-cols-3 gap-1.5">
              {q.options.map((option) => {
                const picked = chosen[q.position] === option;
                const right = picked && option === q.answer;
                const bad = picked && option !== q.answer;
                return (
                  <button
                    key={option}
                    type="button"
                    role="radio"
                    aria-checked={picked}
                    onClick={() => onChoose(q.position, option)}
                    className={cn(
                      'flex min-w-0 items-center justify-center gap-1.5 rounded-lg border px-2 py-2 font-mono text-[13px] font-semibold',
                      right ? 'border-[1.5px] border-mint bg-mint-soft text-mint-ink'
                        : bad ? 'border-[1.5px] border-destructive bg-destructive/10 text-destructive-ink'
                          : 'border-edge bg-field text-foreground hover:bg-field-hover',
                    )}
                  >
                    {right ? <Check className="size-3.5 shrink-0" /> : bad ? <X className="size-3.5 shrink-0" /> : null}
                    <span className="truncate">{option}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      {wrong ? (
        <HdNotice tone="error" icon={X}>
          <span className="font-semibold text-destructive-ink">{t('vaults.hd.check.wrong', { position: wrong.position })}</span>
          {t('vaults.hd.check.wrongHint', { position: wrong.position })}
        </HdNotice>
      ) : null}
    </>
  );
}


/** Account #0's three addresses, in the shipped address list. */
/** A wallet account's three addresses, for comparing with another wallet: the Bitcoin row says it
 *  is Taproot, since Phantom and others show a SegWit (bc1q) address by default. */
export function HdAccountAddressList({ account }: { account: Pick<HdAccountAddresses, 'eth' | 'btc_taproot' | 'solana'> }) {
  const { t } = useTranslation();
  const label = 'w-[104px] whitespace-nowrap';
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <SigningAddressRow label={t('vaults.addresses.eth')} value={account.eth} labelClassName={label} />
      <SigningAddressRow
        label={t('vaults.hd.btcTaproot')}
        value={account.btc_taproot}
        labelClassName={label}
        hint={<InfoHint hover content={t('vaults.hd.btcTaprootInfo')} label={t('vaults.dialog.moreInfo')} />}
      />
      <SigningAddressRow label={t('vaults.addresses.solana')} value={account.solana} labelClassName={label} />
    </div>
  );
}

/** Done: account #0 is ready and checked, and the user learns how to point an Agent at it. */
export function HdDoneStep({ name, account }: { name: string; account: Pick<HdAccountAddresses, 'eth' | 'btc_taproot' | 'solana'> }) {
  const { t } = useTranslation();
  return (
    <>
      <div className="flex flex-col items-center gap-2 pt-2 text-center">
        <span className="flex size-12 items-center justify-center rounded-2xl bg-mint-soft text-mint-ink">
          <Check className="size-6" strokeWidth={2.5} />
        </span>
        <span className="text-[16px] font-semibold text-foreground">{t('vaults.hd.done.title', { name })}</span>
        <span className="max-w-sm text-[12.5px] leading-snug text-muted">{t('vaults.hd.done.body')}</span>
      </div>
      <div className="flex flex-col gap-2 rounded-[10px] border border-edge bg-surface-2 p-3">
        <span className="flex items-center gap-2 text-[12.5px] font-semibold text-foreground">
          <span className="rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] text-violet-ink">{t('vaults.hd.accountIndex', { account: 0 })}</span>
          {t('vaults.hd.mainAccount')}
        </span>
        <HdAccountAddressList account={account} />
        <span className="flex items-center gap-1.5 text-[11.5px] text-mint-ink">
          <CircleCheck className="size-3.5 shrink-0" />
          {t('vaults.hd.done.verified')}
        </span>
      </div>
      <span className="text-[12px] text-muted">{t('vaults.hd.done.recovery')}</span>
      <div className="flex flex-col gap-1.5">
        <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-foreground">
          <Bot className="size-3.5 text-muted" />
          {t('vaults.hd.done.howTitle')}
        </span>
        <code className="block min-w-0 break-words rounded-md bg-surface px-2.5 py-1.5 font-mono text-[11.5px] text-foreground">
          {t('vaults.hd.signCommand', { name, account: 0 })}
        </code>
        <span className="text-[11px] text-muted">{t('vaults.hd.done.howHint')}</span>
      </div>
    </>
  );
}

/** The import panel: paste, every word checked, the optional passphrase, and #0 to compare. */
export function HdImportFields({
  text, onTextChange, check, passphrase, onPassphraseChange, preview, disabled,
}: {
  text: string;
  onTextChange: (next: string) => void;
  check: PhraseCheck;
  passphrase: string;
  onPassphraseChange: (next: string) => void;
  /** Account #0 derived in this browser from the valid words and passphrase. */
  preview: HdAccountAddresses | null;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const [passphraseOpen, setPassphraseOpen] = useState(Boolean(passphrase));
  const words = useMemo(() => text.normalize('NFKD').toLowerCase().trim().split(/\s+/u).filter(Boolean), [text]);
  const Chevron = passphraseOpen ? ChevronDown : ChevronRight;
  const bad = !check.ok && check.reason === 'unknown-word' ? check.position - 1 : undefined;
  return (
    <>
      <label className="flex flex-col gap-2">
        <span className="text-[13px] font-medium text-foreground">{t('vaults.hd.import.label')}</span>
        <Textarea
          value={text}
          onChange={(event) => onTextChange(event.target.value)}
          rows={3}
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          disabled={disabled}
          aria-invalid={(!check.ok && check.reason !== 'empty') || undefined}
          placeholder={t('vaults.hd.import.placeholder')}
          className="font-mono text-xs"
        />
      </label>
      {words.length > 0 ? <WordGrid words={words} bad={bad} /> : <span className="text-[11px] text-muted">{t('vaults.hd.import.local')}</span>}
      {check.ok ? (
        <span className="flex items-center gap-1.5 text-[12px] font-medium text-mint-ink">
          <CircleCheck className="size-3.5" />
          {t('vaults.hd.import.ok', { count: check.words.length })}
        </span>
      ) : check.reason === 'unknown-word' ? (
        <HdNotice tone="error" icon={X}>
          <div className="flex flex-col gap-2">
            <span>
              <span className="font-semibold text-destructive-ink">{t('vaults.hd.import.unknown', { position: check.position, word: check.word })}</span>
              {check.suggestions[0] ? <span className="ml-1">{t('vaults.hd.import.didYouMean', { word: check.suggestions[0] })}</span> : null}
            </span>
            {check.suggestions.length > 0 ? (
              <span className="flex flex-wrap gap-1.5">
                {check.suggestions.map((suggestion) => (
                  <Button
                    key={suggestion}
                    type="button"
                    size="xs"
                    variant="secondary"
                    onClick={() => onTextChange(words.map((w, i) => (i === check.position - 1 ? suggestion : w)).join(' '))}
                  >
                    {t('vaults.hd.import.use', { word: suggestion })}
                  </Button>
                ))}
              </span>
            ) : null}
          </div>
        </HdNotice>
      ) : check.reason === 'checksum' ? (
        <HdNotice tone="error" icon={AlertTriangle}>
          <span className="font-semibold text-destructive-ink">{t('vaults.hd.import.checksum', { count: words.length })}</span>
          {t('vaults.hd.import.checksumHint')}
        </HdNotice>
      ) : check.reason === 'count' ? (
        <HdNotice tone="error" icon={AlertTriangle}>{t('vaults.hd.import.count', { count: check.count })}</HdNotice>
      ) : null}
      {check.ok ? (
        <div className="flex flex-col rounded-[10px] border border-edge bg-surface-2">
          <button
            type="button"
            aria-expanded={passphraseOpen}
            onClick={() => setPassphraseOpen((open) => !open)}
            className="flex items-center gap-1.5 px-3 py-2.5 text-left"
          >
            <Chevron className="size-3.5 text-muted" />
            <span className="flex-1 text-[12.5px] font-semibold text-foreground">{t('vaults.hd.import.passphrase')}</span>
            {!passphraseOpen ? <span className="text-[11px] text-muted">{t('vaults.hd.import.passphraseFolded')}</span> : null}
          </button>
          {passphraseOpen ? (
            <div className="flex flex-col gap-2 px-3 pb-3">
              <Input
                type="password"
                value={passphrase}
                onChange={(event) => onPassphraseChange(event.target.value)}
                autoComplete="off"
                disabled={disabled}
                aria-label={t('vaults.hd.import.passphrase')}
              />
              <span className="text-[11.5px] leading-snug text-muted">{t('vaults.hd.import.passphraseHint')}</span>
            </div>
          ) : null}
        </div>
      ) : null}
      {check.ok && preview ? (
        <div className="flex flex-col gap-2 rounded-[10px] border border-mint/30 bg-mint/[0.06] p-3">
          <span className="flex items-center gap-2 text-[12.5px] font-semibold text-foreground">
            <span className="rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] text-violet-ink">{t('vaults.hd.accountIndex', { account: 0 })}</span>
            {t('vaults.hd.import.previewTitle')}
          </span>
          <HdAccountAddressList account={preview} />
          <span className="text-[11px] leading-snug text-muted">{t('vaults.hd.import.previewHint')}</span>
        </div>
      ) : null}
    </>
  );
}

/** avault too old for a wallet: nothing typed is lost, and the way to update is one click away. */
export function HdAvaultOld({ version, installed, onOpenDependencies }: { version: string; installed: string; onOpenDependencies?: () => void }) {
  const { t } = useTranslation();
  return (
    <HdNotice tone="warning" icon={ArrowUpCircle}>
      <div className="flex flex-col gap-2">
        <span>
          <span className="font-semibold text-gold-ink">{t('vaults.hd.avaultOld', { version, installed })}</span>
          {t('vaults.hd.avaultOldHint')}
        </span>
        {onOpenDependencies ? (
          <span><Button type="button" size="xs" variant="secondary" onClick={onOpenDependencies}>{t('vaults.hd.openDependencies')}</Button></span>
        ) : null}
      </div>
    </HdNotice>
  );
}

/** The footer of the backup and check steps: back, and on to the check or to creating the wallet. */
export function HdStepFooter({ step, onBack, onNext, canCreate, submitting }: {
  step: 'backup' | 'check';
  onBack: () => void;
  /** Backup only: on to the check. On the check step the button submits the form. */
  onNext?: () => void;
  canCreate?: boolean;
  submitting?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="mt-1 flex items-center justify-end gap-2">
      <Button type="button" variant="ghost" className="mr-auto" disabled={submitting} onClick={onBack}>
        <ArrowLeft className="size-4" />
        {step === 'check' ? t('vaults.hd.check.again') : t('vaults.hd.back')}
      </Button>
      {step === 'backup' ? (
        <Button type="button" onClick={onNext}>{t('vaults.hd.backup.done')}</Button>
      ) : (
        <Button type="submit" disabled={submitting || !canCreate}>
          {submitting ? <Loader2 className="size-4 animate-spin" /> : <WalletCards className="size-4" />}
          {t('vaults.hd.createCta')}
        </Button>
      )}
    </div>
  );
}

/**
 * The address check failed: the wallet's shown #0 is not what avault would spend. `not-saved` means
 * the server refused before saving; `deleted` means a saved one was removed; `stored` means it is
 * still saved (blocked here) and must be deleted.
 */
export function HdMismatchBlock({ outcome, error, busy, onClose, onDelete }: {
  outcome: 'not-saved' | 'deleted' | 'stored';
  error?: string | null;
  busy?: boolean;
  onClose: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const detail = outcome === 'not-saved' ? t('vaults.hd.mismatchNotSaved') : outcome === 'deleted' ? t('vaults.hd.mismatchDeleted') : t('vaults.hd.mismatchHint');
  return (
    <>
      <HdNotice tone="error" icon={AlertTriangle}>
        <span className="font-semibold text-destructive-ink">{t('vaults.hd.mismatch')}</span>
        <span>{detail}</span>
      </HdNotice>
      {error ? <span role="alert" className="text-xs text-destructive-ink">{error}</span> : null}
      <div className="mt-1 flex justify-end gap-2">
        {outcome === 'stored' ? (
          <Button type="button" variant="destructive" disabled={busy} onClick={onDelete}>{t('vaults.hd.deleteWallet')}</Button>
        ) : (
          // Nothing is kept: closing reports no success.
          <Button type="button" onClick={onClose}>{t('vaults.hd.close')}</Button>
        )}
      </div>
    </>
  );
}
