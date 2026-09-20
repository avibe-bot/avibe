// Keyboard navigation for the connection dialog's method tabs.
//
// `SegmentedRadio` draws a real `radiogroup`, and a radio group is expected to
// answer arrow keys: Tab reaches the group, arrows move within it, Home and End
// jump to its ends. The primitive is shared with surfaces outside this lane, so
// the behavior is added here, around it, rather than inside it.
//
// The wrapper is `display: contents` (see `connection.css`): it lays nothing
// out, so the group keeps its own anchored row in the dialog grid, and keydown
// still reaches this handler because DOM events do not care about boxes.
//
// Selection and focus move together — the approved contract asks for focus to
// stay on the selected tab, which is also the standard single-select radio
// behavior a screen reader announces correctly. That is also why the group is a
// single tab stop: the arrows move from the SELECTION, so the focus Tab leaves
// behind has to be the selection too.
import * as React from 'react';

import { SegmentedRadio, type SegmentedRadioProps } from '@/components/ui/segmented';

const BACKWARD = new Set(['ArrowLeft', 'ArrowUp']);
const FORWARD = new Set(['ArrowRight', 'ArrowDown']);

export function MethodRadio<T extends string>({ value, onChange, options, disabled, ...rest }: SegmentedRadioProps<T>) {
  const ref = React.useRef<HTMLDivElement>(null);
  // The other half of the same pattern: a radio group is ONE tab stop, so Tab has to
  // reach the selection and the arrows continue from there. Without this every option is
  // its own stop, and a reader can hold focus on a tab that is not the selected one —
  // which is the state the arrows have no sensible answer for.
  React.useEffect(() => {
    const radios = ref.current?.querySelectorAll<HTMLElement>('[role="radio"]') ?? [];
    radios.forEach((radio, index) => { radio.tabIndex = options[index]?.id === value ? 0 : -1; });
  }, [options, value]);
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled || options.length < 2) return;
    const current = options.findIndex((option) => option.id === value);
    const index = BACKWARD.has(event.key) ? (current - 1 + options.length) % options.length
      : FORWARD.has(event.key) ? (current + 1) % options.length
      : event.key === 'Home' ? 0
      : event.key === 'End' ? options.length - 1
      : -1;
    if (index < 0) return;
    event.preventDefault();
    if (index !== current) onChange(options[index].id);
    // The selected tab keeps the focus, so the next arrow press continues from
    // where the reader is rather than from wherever Tab last left the group.
    ref.current?.querySelectorAll<HTMLElement>('[role="radio"]')[index]?.focus();
  };
  return (
    <div ref={ref} className="connection-radio" onKeyDown={onKeyDown}>
      <SegmentedRadio value={value} onChange={onChange} options={options} disabled={disabled} {...rest} />
    </div>
  );
}
