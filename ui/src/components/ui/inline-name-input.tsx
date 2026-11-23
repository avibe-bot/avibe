import { useRef, useState } from 'react';

// Shared inline name editor for new-entry and rename rows across the file tree and File Browser:
// autofocus, select-all on focus (rename keeps the current name so it can be tweaked), Enter
// commits, Esc/blur cancels. Cancel-on-blur is suppressed right after an Enter/Esc so committing
// (which also blurs) doesn't double-fire. `commitOnBlur` (a name that is a label, not a file):
// leaving the field commits a non-empty name and keeps the previous one when it is empty.
export const InlineNameInput: React.FC<{
  initial: string;
  value?: string;
  onChange?: (value: string) => void;
  placeholder?: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
  commitOnBlur?: boolean;
  className?: string;
}> = ({ initial, value: controlledValue, onChange, placeholder, onCommit, onCancel, commitOnBlur = false, className }) => {
  const [uncontrolledValue, setUncontrolledValue] = useState(initial);
  const value = controlledValue ?? uncontrolledValue;
  const committed = useRef(false);
  return (
    <input
      autoFocus
      value={value}
      placeholder={placeholder}
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => {
        setUncontrolledValue(e.target.value);
        onChange?.(e.target.value);
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          committed.current = true;
          onCommit(value);
        } else if (e.key === 'Escape') {
          e.preventDefault();
          committed.current = true;
          onCancel();
        }
      }}
      onBlur={() => {
        if (committed.current) return;
        if (commitOnBlur && value.trim()) onCommit(value);
        else onCancel();
      }}
      className={
        className ??
        'min-w-0 flex-1 rounded border border-cyan px-1 py-0 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none bg-field'
      }
    />
  );
};
