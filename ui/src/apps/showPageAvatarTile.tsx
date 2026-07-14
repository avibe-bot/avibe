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
// which (a) differs from the failed URL, re-attempting a previously-failed icon,
// and (b) is a new `src` the browser fetches on its own. A stale icon simply
// cannot survive a payload refresh that changed the token.
export const ShowPageAvatarContent: React.FC<{ iconUrl: string | null; letter: string }> = ({ iconUrl, letter }) => {
  // Track the URL that failed (not a bare boolean) so a later payload refresh to a
  // NEW versioned URL retries instead of staying stuck on the letter fallback.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  if (iconUrl && failedUrl !== iconUrl) {
    return (
      <img
        src={iconUrl}
        alt=""
        draggable={false}
        className="size-full object-cover"
        onError={() => setFailedUrl(iconUrl)}
      />
    );
  }
  return <>{letter}</>;
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
