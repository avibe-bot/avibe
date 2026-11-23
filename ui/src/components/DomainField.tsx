import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';

/** What avibe.bot last said about the name in the field. */
export type DomainCheck = 'free' | 'taken' | 'checking' | 'invalid' | 'reclaim';

/** The name a prefix becomes. */
export const DOMAIN_SUFFIX = '-app.avibe.bot';

/**
 * The domain being claimed, drawn as the address itself: the prefix typed in place, followed by
 * `-app.avibe.bot`, large, with one word under it saying whether it can be had. The same field
 * names a domain wherever one is chosen (binding this computer; changing its domain).
 */
export function DomainField({ name, check, readOnly = false, disabled = false, onChange, label }: {
  name: string;
  check: DomainCheck | null;
  readOnly?: boolean;
  disabled?: boolean;
  onChange?: (name: string) => void;
  /** The field's accessible name. */
  label: string;
}) {
  const { t } = useTranslation();
  const word = check === 'free' ? t('domainField.free')
    : check === 'taken' ? t('domainField.taken')
      : check === 'reclaim' ? t('domainField.reclaim')
        : check === 'checking' ? t('domainField.checking')
          : check === 'invalid' ? t('domainField.invalid') : null;
  const tone = check === 'free' || check === 'reclaim' ? 'text-mint-ink' : check === 'taken' ? 'text-destructive-ink' : 'text-muted';
  return <div data-domain-field>
    <label className={cn('flex min-w-0 cursor-text items-baseline rounded-xl border bg-field px-4 py-3 font-mono text-2xl focus-within:ring-2 focus-within:ring-mint/30',
      check === 'taken' ? 'border-destructive' : 'border-edge')}>
      {readOnly
        ? <span className="min-w-0 break-all font-semibold">{name}</span>
        : <input aria-label={label} aria-invalid={check === 'taken' || check === 'invalid' || undefined} value={name} disabled={disabled}
          size={Math.max(name.length, 1)} spellCheck={false} autoCapitalize="none" autoComplete="off"
          className="min-w-0 max-w-full shrink bg-transparent font-semibold text-foreground outline-none disabled:opacity-60"
          onChange={(event) => onChange?.(event.target.value)} />}
      <span className="shrink-0 text-muted">{t('devices.claim.suffix')}</span>
    </label>
    <p role="status" className={cn('mt-2 min-h-5 text-sm', tone)}>{word}</p>
  </div>;
}
