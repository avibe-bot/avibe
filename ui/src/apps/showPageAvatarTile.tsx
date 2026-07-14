import { useEffect, useState } from 'react';
import clsx from 'clsx';

import { showPageAvatar, showPageIconUrl } from './showPageAvatar';
import { subscribeShowPageIconRefresh } from './showPageIconRefresh';

// The icon-or-letter CONTENT of a Show Page avatar, WITHOUT any tile wrapper:
// the page's own HTML icon (§7.1f) rendered as an <img>, falling back to the
// letter avatar when there is no icon OR the image fails to load (onError).
// Shared by ShowPageAvatarTile and the Dock / mobile-drawer / window-title-bar
// tiles — each provides its own accent-box wrapper — so the icon + fallback rule
// lives in one place.
export const ShowPageAvatarContent: React.FC<{ iconUrl: string | null; letter: string }> = ({ iconUrl, letter }) => {
  // Track the URL that failed (not a bare boolean) so a later inventory refresh
  // to a NEW icon path retries instead of staying stuck on the letter fallback.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  // Bumped on every inventory refresh and used as the <img> key: remounting forces
  // an ALREADY-LOADED icon to re-request. The URL is stable (session id only), so a
  // page that overwrites favicon.svg or repoints <link rel="icon"> keeps the same
  // src — without a remount the browser never re-fetches and the tile shows the old
  // icon until a full reload. The remount issues a fresh request that the backend's
  // `no-cache` revalidates: a 304 (unchanged) reuses the bytes, a change pulls the
  // new ones. Clearing failedUrl in the same tick lets a previously-failed icon
  // (added after its <link>, or a transient 404) re-attempt too (§7.1f review).
  const [refresh, setRefresh] = useState(0);
  useEffect(
    () =>
      subscribeShowPageIconRefresh(() => {
        setFailedUrl(null);
        setRefresh((n) => n + 1);
      }),
    [],
  );
  if (iconUrl && failedUrl !== iconUrl) {
    return (
      <img
        key={refresh}
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
  iconPath?: string | null;
  className?: string;
}> = ({ sessionId, title, iconPath, className }) => {
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
      <ShowPageAvatarContent iconUrl={showPageIconUrl(sessionId, iconPath)} letter={letter} />
    </span>
  );
};
