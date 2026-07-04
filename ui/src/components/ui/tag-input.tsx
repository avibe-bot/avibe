import { useId, useMemo, useState } from 'react';
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
  /**
   * Selectable candidate values. When present, a dropdown offers them (all on
   * focus, filtered as the user types), so values can be picked instead of typed.
   * Already-selected values are hidden. Entries must be pre-normalized — clicking
   * one adds it verbatim, bypassing {@link normalize}.
   */
  suggestions?: string[];
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
 *
 * When {@link TagInputProps.suggestions} is passed, a dropdown lets the user pick
 * from candidate values (arrow keys + Enter, or click); otherwise it stays a
 * plain free-text chip input.
 */
export const TagInput: React.FC<TagInputProps> = ({
  values,
  onChange,
  placeholder,
  normalize = defaultNormalize,
  ariaLabel,
  removeLabel = (value) => `Remove ${value}`,
  onPendingChange,
  suggestions,
  className,
  inputClassName,
}) => {
  const [draft, setDraft] = useState('');
  const [focused, setFocused] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const listId = useId();
  const hasSuggestions = (suggestions?.length ?? 0) > 0;

  const setDraftSafe = (next: string) => {
    setDraft(next);
    setHighlight(-1);
    onPendingChange?.(next.trim().length > 0);
  };

  // Live feedback so a typed-but-uncommitted value the matcher would reject (a URL,
  // a host:port) is visibly invalid instead of being silently dropped on submit.
  const draftInvalid = draft.trim().length > 0 && normalize(draft) === null;

  // Candidates not already chosen, matched against the draft (substring, case-insensitive).
  // With an empty draft this is the full list, so focusing reveals everything on offer; the
  // dropdown itself caps height and scrolls, so no arbitrary count limit is imposed.
  const matches = useMemo(() => {
    if (!suggestions?.length) return [];
    const query = draft.trim().toLowerCase();
    const chosen = new Set(values);
    return suggestions.filter((option) => !chosen.has(option) && (!query || option.toLowerCase().includes(query)));
  }, [suggestions, draft, values]);

  // The list shows while focused with candidates on offer. Escape is intentionally not
  // handled here: inside a Radix Dialog it registers Escape on document with capture, so a
  // bubble-phase handler can't scope it to the list — Escape stays the dialog's own close
  // key, and the list dismisses on blur / selection instead.
  const listOpen = focused && matches.length > 0;

  const add = (value: string) => {
    if (!values.includes(value)) onChange([...values, value]);
    setDraft('');
    setHighlight(-1);
    onPendingChange?.(false);
  };

  const commit = (raw: string) => {
    const cleaned = normalize(raw);
    if (cleaned) add(cleaned);
  };

  const removeAt = (index: number) => onChange(values.filter((_, i) => i !== index));

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' && matches.length) {
      event.preventDefault();
      setHighlight((h) => (h + 1) % matches.length);
    } else if (event.key === 'ArrowUp' && matches.length) {
      event.preventDefault();
      setHighlight((h) => (h <= 0 ? matches.length - 1 : h - 1));
    } else if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      if (event.key === 'Enter' && listOpen && highlight >= 0 && highlight < matches.length) {
        add(matches[highlight]);
      } else {
        commit(draft);
      }
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
    <div className="relative">
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
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false);
            commit(draft);
          }}
          placeholder={values.length ? undefined : placeholder}
          aria-label={ariaLabel}
          aria-invalid={draftInvalid || undefined}
          role={hasSuggestions ? 'combobox' : undefined}
          aria-expanded={hasSuggestions ? listOpen : undefined}
          aria-controls={hasSuggestions && listOpen ? listId : undefined}
          aria-autocomplete={hasSuggestions ? 'list' : undefined}
          autoComplete="off"
          spellCheck={false}
          className={cn(
            'min-w-[8ch] flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground',
            inputClassName,
          )}
        />
      </div>
      {listOpen && (
        <ul
          id={listId}
          role="listbox"
          className="absolute left-0 right-0 top-full z-50 mt-1 max-h-48 overflow-y-auto rounded-md border border-border bg-surface py-1 shadow-lg"
        >
          {matches.map((option, index) => (
            <li key={option} role="option" aria-selected={index === highlight}>
              <button
                type="button"
                // mousedown (not click) so the input never blurs first, keeping focus
                // and the list open for picking several in a row.
                onMouseDown={(event) => {
                  event.preventDefault();
                  add(option);
                }}
                onMouseEnter={() => setHighlight(index)}
                className={cn(
                  'flex w-full items-center px-2.5 py-1.5 text-left font-mono text-xs',
                  index === highlight ? 'bg-surface-2 text-foreground' : 'text-muted hover:text-foreground',
                )}
              >
                {option}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
