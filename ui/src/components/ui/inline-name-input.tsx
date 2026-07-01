import { useRef, useState } from 'react';

// Shared inline name editor for new-entry and rename rows across the file tree and File Browser:
// autofocus, select-all on focus (rename keeps the current name so it can be tweaked), Enter
// commits, Esc/blur cancels. Cancel-on-blur is suppressed right after an Enter/Esc so committing
// (which also blurs) doesn't double-fire.
export const InlineNameInput: React.FC<{
  initial: string;
  placeholder?: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
  className?: string;
}> = ({ initial, placeholder, onCommit, onCancel, className }) => {
  const [value, setValue] = useState(initial);
  const committed = useRef(false);
  return (
    <input
      autoFocus
      value={value}
      placeholder={placeholder}
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setValue(e.target.value)}
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
        if (!committed.current) onCancel();
      }}
      className={
        className ??
        'min-w-0 flex-1 rounded border border-cyan bg-surface px-1 py-0 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none'
      }
    />
  );
};
