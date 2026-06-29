import { useState } from 'react';
import type { KeyboardEvent, ClipboardEvent } from 'react';
import { X } from 'lucide-react';

import { cn } from '@/lib/utils';

export type TagInputProps = {
  values: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  /**
   * Normalize/validate a raw entry. Return the cleaned value to accept it, or
   * `null` to reject. Defaults to a trimmed, non-empty string.
   */
  normalize?: (raw: string) => string | null;
  ariaLabel?: string;
  /** Localized aria-label for a chip's remove button. Defaults to English. */
  removeLabel?: (value: string) => string;
  /** Notified when the uncommitted draft becomes non-empty / empty, so the form
   *  can block submitting while a chip is half-typed. */
  onPendingChange?: (pending: boolean) => void;
  className?: string;
  inputClassName?: string;
};

const defaultNormalize = (raw: string): string | null => {
  const trimmed = raw.trim();
  return trimmed.length ? trimmed : null;
};

/**
 * Chip-style multi-value input: type a value and press Enter or comma to add a
 * tag, click the × (or Backspace on an empty field) to remove one. Used for
 * vault secret tags and allowed-host lists.
 */
export const TagInput: React.FC<TagInputProps> = ({
  values,
  onChange,
  placeholder,
  normalize = defaultNormalize,
  ariaLabel,
  removeLabel = (value) => `Remove ${value}`,
  onPendingChange,
  className,
  inputClassName,
}) => {
  const [draft, setDraft] = useState('');

  const setDraftSafe = (next: string) => {
    setDraft(next);
    onPendingChange?.(next.trim().length > 0);
  };

  // Live feedback so a typed-but-uncommitted value the matcher would reject (a URL,
  // a host:port) is visibly invalid instead of being silently dropped on submit.
  const draftInvalid = draft.trim().length > 0 && normalize(draft) === null;

  const commit = (raw: string) => {
    const cleaned = normalize(raw);
    if (!cleaned) return;
    if (!values.includes(cleaned)) onChange([...values, cleaned]);
    setDraftSafe('');
  };

  const removeAt = (index: number) => onChange(values.filter((_, i) => i !== index));

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      commit(draft);
    } else if (event.key === 'Backspace' && draft === '' && values.length) {
      event.preventDefault();
      removeAt(values.length - 1);
    }
  };

  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const text = event.clipboardData.getData('text');
    if (!text.includes(',') && !text.includes('\n')) return;
    event.preventDefault();
    const parts = text.split(/[,\n]/);
    const next = [...values];
    for (const part of parts) {
      const cleaned = normalize(part);
      if (cleaned && !next.includes(cleaned)) next.push(cleaned);
    }
    onChange(next);
    setDraftSafe('');
  };

  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-1.5 rounded-md border bg-surface px-2 py-1.5',
        draftInvalid ? 'border-destructive' : 'border-border focus-within:border-mint',
        className,
      )}
    >
      {values.map((value, index) => (
        <span
          key={value}
          className="flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-foreground"
        >
          {value}
          <button
            type="button"
            onClick={() => removeAt(index)}
            aria-label={removeLabel(value)}
            className="text-muted hover:text-foreground"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(event) => setDraftSafe(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        onBlur={() => commit(draft)}
        placeholder={values.length ? undefined : placeholder}
        aria-label={ariaLabel}
        aria-invalid={draftInvalid || undefined}
        autoComplete="off"
        spellCheck={false}
        className={cn(
          'min-w-[8ch] flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground',
          inputClassName,
        )}
      />
    </div>
  );
};
