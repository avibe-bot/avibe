import clsx from 'clsx';

import { showPageAvatar } from './showPageAvatar';

// The icon-free tile for a Show Page: the first grapheme of the title on a tint
// hashed from the session id (the same letter avatar the Dock renders). Shared
// by both App Library views so a pinned page reads identically in the Dock, the
// Apps view, and the Show Pages view.
export const ShowPageAvatarTile: React.FC<{ sessionId: string; title: string; className?: string }> = ({
  sessionId,
  title,
  className,
}) => {
  const { letter, accentVar } = showPageAvatar(sessionId, title);
  return (
    <span
      aria-hidden
      className={clsx(
        'flex size-9 shrink-0 items-center justify-center rounded-lg border text-[14px] font-bold leading-none',
        className,
      )}
      style={{
        color: `var(${accentVar})`,
        backgroundColor: `color-mix(in srgb, var(${accentVar}) 16%, transparent)`,
        borderColor: `color-mix(in srgb, var(${accentVar}) 34%, transparent)`,
      }}
    >
      {letter}
    </span>
  );
};
