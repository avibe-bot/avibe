import { useState } from 'react';
import clsx from 'clsx';

import { showPageAvatar, showPageIconUrl } from './showPageAvatar';

// The icon-or-letter CONTENT of a Show Page avatar, WITHOUT any tile wrapper:
// the page's own HTML icon (§7.1f) rendered as an <img>, falling back to the
// letter avatar when there is no icon OR the image fails to load (onError).
// Shared by ShowPageAvatarTile and the Dock / mobile-drawer / window-title-bar
// tiles — each provides its own accent-box wrapper — so the icon + fallback rule
// lives in one place.
//
// Freshness needs no notifier/remount machinery: `iconUrl` is content-versioned
// (`?v=<token>` from the icon file's identity), so a changed icon changes the URL,
// which is a new `src` the browser fetches on its own.
//
// A load failure is retried a bounded number of times before falling back to the
// letter: `onError` remounts the <img> (a per-URL attempt count is the `key`, so it
// re-fetches) and only latches to the letter after MAX_ICON_LOAD_ATTEMPTS. In the
// versioned-URL model a permanently-absent icon arrives as a null `iconUrl` (letter,
// no <img>, no onError) — so onError only ever signals a TRANSIENT failure (a brief
// bytes-or-404 race window, a network blip), exactly the case worth retrying. The
// count is keyed to the URL, so a new versioned URL retries with a fresh budget.
const MAX_ICON_LOAD_ATTEMPTS = 3;
// INVARIANT (proven by live CDP real-input on the regression env, §7.1h item 3): in
// the Dock a tile is a framer-motion Reorder.Item, and its drag pan does NOT start
// when the real press target is the interactive <button> itself — only when the press
// lands on a non-interactive CHILD element. So the tile button must never be the
// direct press target: the avatar content MUST render as a FILLING child element —
// never a bare text node, and never `pointer-events-none` (which pushes the press
// back through to the button and re-breaks drag). Built-in tiles already render an
// <svg> child; here both the icon <img> and the letter fill the tile as real children.
export const ShowPageAvatarContent: React.FC<{ iconUrl: string | null; letter: string }> = ({ iconUrl, letter }) => {
  const [failure, setFailure] = useState<{ url: string; attempts: number } | null>(null);
  const attempts = failure && failure.url === iconUrl ? failure.attempts : 0;
  if (iconUrl && attempts < MAX_ICON_LOAD_ATTEMPTS) {
    return (
      <img
        key={`${iconUrl}#${attempts}`}
        src={iconUrl}
        alt=""
        // `draggable={false}` disables native image DnD (#906's real concern). NO
        // `pointer-events-none`: the filling <img> must itself BE the press target,
        // or the Dock Reorder pan can't start (see invariant above).
        draggable={false}
        className="size-full select-none object-cover"
        onError={() => setFailure({ url: iconUrl, attempts: attempts + 1 })}
      />
    );
  }
  // A FILLING element, never a bare text node: a raw string leaves the <button>
  // itself as the press target and the Dock drag won't start (see invariant above).
  return (
    <span aria-hidden className="grid size-full place-items-center select-none">
      {letter}
    </span>
  );
};

// The avatar tile for a Show Page: an accent-tinted rounded box (first grapheme
// on a session-hashed accent) wrapping the icon-or-letter content. Shared by the
// App Library views — Apps, Show Pages, and the ⌘K search results — so a page
// reads identically across them.
export const ShowPageAvatarTile: React.FC<{
  sessionId: string;
  title: string;
  iconVersion?: string | null;
  className?: string;
}> = ({ sessionId, title, iconVersion, className }) => {
  const { letter, accentVar } = showPageAvatar(sessionId, title);
  return (
    <span
      aria-hidden
      className={clsx(
        'flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-lg border text-[14px] font-bold leading-none',
        className,
      )}
      style={{
        color: `var(${accentVar})`,
        backgroundColor: `color-mix(in srgb, var(${accentVar}) 16%, transparent)`,
        borderColor: `color-mix(in srgb, var(${accentVar}) 34%, transparent)`,
      }}
    >
      <ShowPageAvatarContent iconUrl={showPageIconUrl(sessionId, iconVersion)} letter={letter} />
    </span>
  );
};
