import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { useImageViewer } from '@/components/ui/image-viewer-context';
import { handleMediaDownloadClick, mediaDownloadHref } from '@/lib/downloadMedia';
import { cn } from '@/lib/utils';

// When a markdown image is wrapped in a link (``[![](media)](href)``),
// react-markdown nests the image inside the outer <a>. The Markdown ``a`` handler
// wraps such children in this provider so the ChatImage inside renders WITHOUT
// its own download <a> (nested anchors are invalid HTML) and without the
// lightbox click (the surrounding link is the intended action) — just the
// capped <img>.
const LinkedImageContext = React.createContext(false);
export const LinkedImageProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <LinkedImageContext.Provider value={true}>{children}</LinkedImageContext.Provider>
);
export const LinkedImageContent: React.FC<{
  linked: React.ReactNode;
  unlinked: React.ReactNode;
}> = ({ linked, unlinked }) => (
  React.useContext(LinkedImageContext) ? linked : unlinked
);

// Preview caps (mirror the design): an image renders at its natural size, scaled
// down to fit a max width of 22rem and a max height of 15rem, never upscaled.
const MAX_W_PX = 22 * 16; // 352
const MAX_H_PX = 15 * 16; // 240
// The unknown-dimensions box: inline ``max-*`` so it beats the
// ``.vr-markdown img { max-width: 100% }`` descendant rule.
const IMG_BOX: React.CSSProperties = { maxWidth: 'min(22rem, 100%)', maxHeight: '15rem' };
const IMG_CLASS = 'block h-auto w-auto rounded-lg border border-border object-contain';
// When the box is reserved by the parent, the image just fills it.
const IMG_FILL_CLASS = 'block size-full rounded-lg border border-border object-contain';

// Process-lifetime cache of natural pixel dimensions keyed by src, so an image
// seen once (its ``load`` measured below) reserves its exact box on every later
// render — scrolling back to it, switching chats and returning — with zero shift.
// Media-proxy URLs are content-fingerprinted (the token changes when the file
// does), so a cached size never goes stale. Best-effort mirrored to sessionStorage
// so a reload stays shift-free too.
const DIMS_CACHE_KEY = 'vr-img-dims';
const dimsCache = new Map<string, { w: number; h: number }>();
try {
  const raw = sessionStorage.getItem(DIMS_CACHE_KEY);
  if (raw) {
    for (const [k, v] of Object.entries(JSON.parse(raw) as Record<string, { w: number; h: number }>)) {
      if (v && v.w > 0 && v.h > 0) dimsCache.set(k, v);
    }
  }
} catch {
  /* no sessionStorage (private mode / SSR) — the in-memory Map still works */
}
function rememberDims(src: string, w: number, h: number): void {
  if (!(w > 0 && h > 0) || dimsCache.has(src)) return;
  dimsCache.set(src, { w, h });
  try {
    sessionStorage.setItem(DIMS_CACHE_KEY, JSON.stringify(Object.fromEntries(dimsCache)));
  } catch {
    /* quota / unavailable — keep the in-memory cache */
  }
}

// Reserve the exact rendered box from known pixel dimensions: the widest the image
// will ever draw (its natural width, clamped so neither cap is exceeded) plus an
// ``aspect-ratio`` so the height is held before a single byte loads. ``maxWidth:
// 100%`` keeps it responsive on a narrow phone (it shrinks, height tracking the
// ratio). Returns ``undefined`` when dimensions are unknown.
function reservedStyle(w?: number, h?: number): React.CSSProperties | undefined {
  if (!w || !h || w <= 0 || h <= 0) return undefined;
  const displayW = Math.min(w, MAX_W_PX, Math.round((MAX_H_PX * w) / h));
  return { width: `${displayW}px`, maxWidth: '100%', aspectRatio: `${w} / ${h}` };
}

// One inline chat image — used by both the Markdown renderer (agent replies) and
// the user-attachment renderer (ChatPage). Capped to a preview width and shown
// at its natural aspect (never stretched / full-width); click to open the
// lightbox; hover reveals a download button.
//
// ``width`` / ``height`` are the source pixel dimensions when the server knows
// them (user uploads carry them on the attachment; agent images on the proxy
// URL). With them — or with a size measured on a prior load — the box is reserved
// up front so loading shifts NOTHING; without them the image still renders and its
// natural size is measured once for next time.
export const ChatImage: React.FC<{
  src: string;
  alt?: string;
  className?: string;
  width?: number;
  height?: number;
}> = ({ src, alt, className, width, height }) => {
  const { t } = useTranslation();
  const viewer = useImageViewer();
  const linked = React.useContext(LinkedImageContext);
  const [measured, setMeasured] = React.useState<{ w: number; h: number } | null>(null);

  // Source priority: server-provided > measured-this-mount > cached-from-before.
  const cached = dimsCache.get(src);
  const w = width ?? measured?.w ?? cached?.w;
  const h = height ?? measured?.h ?? cached?.h;
  const box = reservedStyle(w, h);

  // Only measure while the box is still unknown; once reserved there's nothing to
  // learn. Measuring re-renders this instance to apply the box (same size it
  // already is → no shift) and seeds the cache for future mounts.
  const onLoad = box
    ? undefined
    : (e: React.SyntheticEvent<HTMLImageElement>) => {
        const img = e.currentTarget;
        if (img.naturalWidth > 0 && img.naturalHeight > 0) {
          rememberDims(src, img.naturalWidth, img.naturalHeight);
          setMeasured({ w: img.naturalWidth, h: img.naturalHeight });
        }
      };

  // Inside a markdown link: a bare capped image — no nested download anchor and
  // no lightbox click stealing the link.
  if (linked) {
    return (
      <img
        src={src}
        alt={alt || ''}
        loading="lazy"
        onLoad={onLoad}
        style={box ?? IMG_BOX}
        className={cn('my-1 align-bottom', box ? IMG_FILL_CLASS : IMG_CLASS, className)}
      />
    );
  }

  return (
    <span className={cn('group relative my-1 inline-block max-w-full align-bottom', className)} style={box}>
      <img
        src={src}
        alt={alt || ''}
        loading="lazy"
        onLoad={onLoad}
        onClick={() => viewer?.open(src)}
        style={box ? undefined : IMG_BOX}
        className={cn(box ? IMG_FILL_CLASS : IMG_CLASS, viewer ? 'cursor-zoom-in' : '')}
      />
      <Button
        asChild
        variant="ghost"
        size="icon"
        // Overlay badge on the image corner: smaller than a standard icon button,
        // dark translucent, revealed on hover. Inline color/decoration override
        // because this <a> can render inside ``.vr-markdown`` whose ``a`` rule
        // would otherwise tint it cyan + underline.
        className="absolute right-2 top-2 size-7 rounded-md bg-black/55 text-white opacity-0 backdrop-blur-sm transition hover:bg-black/70 hover:text-white group-hover:opacity-100"
      >
        <a
          href={mediaDownloadHref(src)}
          download
          onClick={(e) => {
            e.stopPropagation();
            handleMediaDownloadClick(e, src);
          }}
          aria-label={t('chat.media.download')}
          style={{ color: '#fff', textDecoration: 'none' }}
        >
          <Download className="size-4" />
        </a>
      </Button>
    </span>
  );
};
