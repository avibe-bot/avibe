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
  className,
  inputClassName,
}) => {
  const [draft, setDraft] = useState('');

  const commit = (raw: string) => {
    const cleaned = normalize(raw);
    if (!cleaned) return;
    if (!values.includes(cleaned)) onChange([...values, cleaned]);
    setDraft('');
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
    setDraft('');
  };

  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1.5 focus-within:border-mint',
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
            aria-label={`Remove ${value}`}
            className="text-muted hover:text-foreground"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        onBlur={() => commit(draft)}
        placeholder={values.length ? undefined : placeholder}
        aria-label={ariaLabel}
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
