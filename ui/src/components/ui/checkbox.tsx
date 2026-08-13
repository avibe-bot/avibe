import { Check } from 'lucide-react';
import clsx from 'clsx';

export interface CheckboxProps {
  checked: boolean;
  onCheckedChange?: (next: boolean) => void;
  disabled?: boolean;
  label?: string;
  className?: string;
  /**
   * Render a non-interactive visual box instead of a button. Use this inside a
   * clickable row that already owns the toggle and the `checkbox` role — it
   * avoids nested buttons and the double-toggle that follows from both the row
   * and the checkbox firing the same handler.
   */
  presentational?: boolean;
}

/**
 * Lightweight controlled checkbox (no Radix dependency — the repo only pulls
 * in `@radix-ui/react-dialog`, and a checkbox this simple doesn't warrant a
 * new one). Mint fill + dark check when checked; bordered surface when not.
 * Visual contract mirrors design.pen's multi-select rows.
 */
export function Checkbox({ checked, onCheckedChange, disabled, label, className, presentational }: CheckboxProps) {
  const visual = clsx(
    'flex size-[18px] shrink-0 items-center justify-center rounded-[5px] border transition-colors',
    checked ? 'border-transparent bg-mint text-primary-foreground' : 'border-border-strong bg-surface-3 text-transparent',
    disabled && 'cursor-not-allowed opacity-50',
    className,
  );
  if (presentational) {
    return (
      <span aria-hidden className={clsx(visual, 'pointer-events-none')}>
        <Check className="size-3" strokeWidth={3} />
      </span>
    );
  }
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      className={visual}
    >
      <Check className="size-3" strokeWidth={3} />
    </button>
  );
}
