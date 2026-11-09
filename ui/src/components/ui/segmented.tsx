import clsx from 'clsx';
import type { ComponentType } from 'react';

import { segmentedItemClass, segmentedTrackClass, type SegmentedTone } from './segmented-classes';

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
  /** An optional leading icon, drawn at the label's size. */
  icon?: ComponentType<{ className?: string }>;
};

export type { SegmentedTone } from './segmented-classes';

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
      className={clsx(segmentedTrackClass, className, disabled && 'opacity-60')}
    >
      {options.map((opt) => {
        const active = value === opt.id;
        const Icon = opt.icon;
        return (
          <button
            key={opt.id}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => onChange(opt.id)}
            className={clsx(segmentedItemClass(active, tone), disabled && 'cursor-not-allowed')}
          >
            {Icon && <Icon className="size-3 shrink-0" />}
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
