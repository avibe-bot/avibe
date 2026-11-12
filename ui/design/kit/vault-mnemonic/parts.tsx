// Building blocks the seed-phrase wallet proposal shares (ui/design: vault/mnemonic/*). Product
// tokens and src/components/ui primitives only; copy is draft copy (kit/draft.ts).
import type { ComponentType, ReactNode } from 'react';
import { Check, Eye, Server, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { useDraft } from '../draft';
import type { MnemonicChain } from './data';

export const FIELD_LABEL = 'text-[13px] font-medium text-foreground';

/** The Add dialog's branded header, as VaultSecretDialog draws it. */
export function DialogHeading({ icon: Icon, title, subtitle, tone = 'accent' }: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  subtitle: ReactNode;
  tone?: 'accent' | 'violet';
}) {
  return (
    <div className="flex min-w-0 items-start gap-3 pr-6">
      <span className={cn(
        'flex size-10 shrink-0 items-center justify-center rounded-xl',
        tone === 'violet' ? 'bg-violet/15 text-violet-ink' : 'bg-accent/15 text-accent-ink',
      )}>
        <Icon className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="break-words text-[15px] font-semibold text-foreground">{title}</span>
        <span className="break-words text-xs text-muted">{subtitle}</span>
      </div>
    </div>
  );
}

/** Where the user is in creating a wallet: three short steps, so the length is known up front. */
export function Stepper({ step }: { step: 1 | 2 | 3 }) {
  const d = useDraft();
  const labels = [d('设置', 'Set up'), d('抄下助记词', 'Write it down'), d('核对', 'Check')];
  return (
    <ol className="flex items-center gap-2" aria-label={d('步骤', 'Steps')}>
      {labels.map((label, index) => {
        const n = index + 1;
        const done = n < step;
        const current = n === step;
        return (
          <li key={label} className="flex min-w-0 flex-1 items-center gap-2">
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
export function WordGrid({ words, hidden = false, columns = 3, picked }: {
  words: string[];
  hidden?: boolean;
  columns?: 2 | 3;
  /** Positions (1-based) the check step asks for, drawn as blanks. */
  picked?: number[];
}) {
  const d = useDraft();
  return (
    <div className="relative">
      <ol
        className={cn('grid gap-1.5', columns === 3 ? 'grid-cols-3' : 'grid-cols-2', hidden && 'select-none blur-[7px]')}
        aria-hidden={hidden || undefined}
      >
        {words.map((word, index) => (
          <li key={index} className="flex min-w-0 items-baseline gap-2 rounded-lg border border-edge bg-field px-2.5 py-2">
            <span className="w-4 shrink-0 text-right font-mono text-[10.5px] text-muted">{index + 1}</span>
            <span className="truncate font-mono text-[13.5px] font-semibold text-foreground">
              {picked?.includes(index + 1) ? '·····' : word}
            </span>
          </li>
        ))}
      </ol>
      {hidden ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 rounded-lg">
          <Button type="button" variant="secondary" className="shadow-elevation-1">
            <Eye className="size-4" />
            {d('显示 12 个词', 'Show the 12 words')}
          </Button>
          <span className="rounded-full bg-popover/80 px-2.5 py-0.5 text-[11.5px] text-muted">{d('先确认没人能看到你的屏幕', 'Make sure no one can see your screen')}</span>
        </div>
      ) : null}
    </div>
  );
}

/** A one-line notice in the dialog: neutral, warning, error or success. */
export function Notice({ tone = 'neutral', icon: Icon, children, className }: {
  tone?: 'neutral' | 'warning' | 'error' | 'success' | 'violet';
  icon?: ComponentType<{ className?: string }>;
  children: ReactNode;
  className?: string;
}) {
  const box = {
    neutral: 'border-edge bg-surface-2 text-muted',
    warning: 'border-gold/40 bg-gold/10 text-foreground',
    error: 'border-destructive/40 bg-destructive/10 text-foreground',
    success: 'border-mint/30 bg-mint/[0.08] text-foreground',
    violet: 'border-violet/30 bg-violet-soft text-foreground',
  }[tone];
  const ink = { neutral: 'text-muted', warning: 'text-gold-ink', error: 'text-destructive-ink', success: 'text-mint-ink', violet: 'text-violet-ink' }[tone];
  return (
    <div role={tone === 'error' ? 'alert' : 'note'} className={cn('flex items-start gap-2.5 rounded-[10px] border px-3 py-2.5 text-[12.5px] leading-snug', box, className)}>
      {Icon ? <Icon className={cn('mt-0.5 size-4 shrink-0', ink)} /> : null}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/** The two tier cards as the Add dialog draws them: each keeps its description in view (owner,
 *  2026-11-12). */
export function TierCards({ value }: { value: 'standard' | 'protected' }) {
  const { t } = useTranslation();
  const cards = [
    { key: 'standard', icon: Server, title: t('vaults.dialog.standardProtection'), desc: t('vaults.dialog.standardHelp') },
    { key: 'protected', icon: ShieldCheck, title: t('vaults.dialog.protectedProtection'), desc: t('vaults.dialog.protectedHelp') },
  ] as const;
  return (
    <div className="flex flex-col gap-1.5">
      <span className={FIELD_LABEL}>{t('vaults.dialog.protection')}</span>
      <div className="grid grid-cols-2 gap-2.5">
        {cards.map(({ key, icon: Icon, title, desc }) => {
          const selected = value === key;
          return (
            <button
              key={key}
              type="button"
              aria-pressed={selected}
              className={cn(
                'flex flex-col gap-1.5 rounded-[10px] border p-3 text-left transition-colors',
                selected ? 'border-[1.5px] border-mint bg-mint-soft' : 'border-edge bg-field hover:bg-field-hover',
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
  );
}

/** One short line with the longer explanation behind an ⓘ: the shipped Add dialog's own. */
export { HintLine } from '@/components/ui/vault-key-builder-parts';

/** A chain's short label, from the shipped address labels. */
export function useChainLabel(): (chain: MnemonicChain) => string {
  const { t } = useTranslation();
  return (chain) => (chain === 'evm' ? t('vaults.addresses.eth') : chain === 'bitcoin' ? t('vaults.addresses.btcTaproot') : t('vaults.addresses.solana'));
}

/** `#1`: an account's position, the number an Agent passes as --account. */
export function AccountIndex({ index, className }: { index: number; className?: string }) {
  return (
    <span className={cn('inline-flex h-5 min-w-7 shrink-0 items-center justify-center rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] font-semibold text-violet-ink', className)}>
      #{index}
    </span>
  );
}

/** A shell command shown as the user would paste it to an Agent. */
export function CommandLine({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <code className={cn('block min-w-0 break-words rounded-md bg-surface px-2.5 py-1.5 font-mono text-[11.5px] text-foreground', className)}>
      {children}
    </code>
  );
}
