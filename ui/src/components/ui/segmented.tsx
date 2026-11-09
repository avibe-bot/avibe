import clsx from 'clsx';

/**
 * Generic segmented-radio control. Promoted here from
 * `settings/shared/SegmentedRadio` once a non-Settings caller (the Workbench
 * Skills page) needed it — the shared home is `ui/`, and Settings keeps
 * importing it via a thin re-export shim.
 *
 * Visual contract:
 * - The whole row is one `role="radiogroup"` with the active option styled
 *   mint-on-mint-soft and inactive options muted.
 * - When `disabled` is true, the row dims and individual buttons reject
 *   clicks. Callers that need a stronger lock (e.g. defending against an iOS
 *   Safari quirk where `disabled` buttons still register a tap) should ALSO
 *   guard the state mutation site itself — see `useOAuthFlowLock`.
 *
 * The generic `T` is a string-literal union of the radio's possible values,
 * so `onChange` is typed strictly to the option ids.
 */
export type SegmentedRadioOption<T extends string> = {
  id: T;
  label: string;
};

/** Active-option color. Mint is the default; Show Pages tints by visibility. */
export type SegmentedTone = 'mint' | 'gold' | 'cyan' | 'muted';

const ACTIVE_TONES: Record<SegmentedTone, string> = {
  mint: 'border border-mint/30 bg-mint-soft font-bold text-mint-ink',
  gold: 'border border-gold/40 bg-gold/10 font-bold text-gold-ink',
  cyan: 'border border-cyan/40 bg-cyan-soft font-bold text-cyan-ink',
  muted: 'border border-edge bg-foreground/[0.06] font-bold text-foreground shadow-elevation-0',
};

export interface SegmentedRadioProps<T extends string> {
  value: T;
  onChange: (next: T) => void;
  options: ReadonlyArray<SegmentedRadioOption<T>>;
  ariaLabel: string;
  disabled?: boolean;
  tone?: SegmentedTone;
  className?: string;
}

export function SegmentedRadio<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  disabled,
  tone = 'mint',
  className,
}: SegmentedRadioProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      aria-disabled={disabled || undefined}
      className={clsx(
        // A filled track, no outline (ui/design: system/elevation/edges).
        'flex h-9 items-stretch gap-0.5 rounded-md border border-edge bg-field p-0.5',
        className,
        disabled && 'opacity-60',
      )}
    >
      {options.map((opt) => {
        const active = value === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => onChange(opt.id)}
            className={clsx(
              'press flex-1 whitespace-nowrap rounded-[4px] px-3 text-[12px]',
              active
                ? ACTIVE_TONES[tone]
                : 'font-medium text-muted hover:text-foreground active:bg-foreground/[0.04] active:text-foreground',
              disabled && 'cursor-not-allowed',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
