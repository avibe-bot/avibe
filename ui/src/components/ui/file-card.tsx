import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Download, Eye, File, FileArchive, FileText, Image as ImageIcon } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiFetch } from '@/lib/apiFetch';
import { isProxyMediaUrl } from '@/lib/mediaProxy';
import { handleMediaDownloadClick, mediaDownloadHref } from '@/lib/downloadMedia';
import { useFileViewer } from '@/components/ui/file-viewer';
import { previewRenderKind, formatBytes } from '@/lib/filePreview';
import { cn } from '@/lib/utils';

// Download card for an agent-reply file that was rewritten to the same-origin
// media proxy (``/api/media/<token>``). Rendered by the shared
// Markdown renderer in place of a plain ``[label](proxy)`` link. The icon tile
// is tinted by file type; the type + size come from a lightweight ``/meta``
// fetch so we don't have to download the file to label it. Composed from the
// design-system ``Button`` (ghost/icon) rather than hand-rolled controls.

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif', 'ico', 'heic']);
const ARCHIVE_EXT = new Set(['zip', 'tar', 'gz', 'tgz', 'rar', '7z', 'bz2', 'xz']);
const DOC_EXT = new Set(['doc', 'docx', 'txt', 'md', 'rtf', 'csv', 'json', 'log', 'yaml', 'yml', 'xml', 'html']);

// Literal class strings (Tailwind can't see interpolated names) so the tint
// utilities are actually generated.
function kindOf(ext: string): { Icon: LucideIcon; tile: string } {
  if (IMAGE_EXT.has(ext)) return { Icon: ImageIcon, tile: 'bg-cyan/15 text-cyan' };
  if (ARCHIVE_EXT.has(ext)) return { Icon: FileArchive, tile: 'bg-gold/15 text-gold' };
  if (ext === 'pdf') return { Icon: FileText, tile: 'bg-cyan/15 text-cyan' };
  if (DOC_EXT.has(ext)) return { Icon: FileText, tile: 'bg-violet/15 text-violet' };
  return { Icon: File, tile: 'bg-mint/15 text-mint' };
}

function nodeText(node: React.ReactNode): string {
  if (node == null || node === false || node === true) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join('');
  if (React.isValidElement(node)) return nodeText((node.props as { children?: React.ReactNode }).children);
  return '';
}

type Meta = { name?: string; ext?: string; size?: number | null; content_type?: string };

export const FileCard: React.FC<{ href: string; children?: React.ReactNode }> = ({ href, children }) => {
  const { t } = useTranslation();
  const viewer = useFileViewer();
  const label = nodeText(children).trim();
  const [meta, setMeta] = React.useState<Meta | null>(null);

  React.useEffect(() => {
    // Only fetch metadata for our own media proxy — never auto-call an arbitrary
    // host that slipped into a non-proxy href.
    if (!isProxyMediaUrl(href)) return;
    let alive = true;
    apiFetch(`${href}/meta`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (alive && data) setMeta(data);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [href]);

  const ext = (meta?.ext || label.split('.').pop() || '').toLowerCase();
  const { Icon, tile } = kindOf(ext);
  const title = label || meta?.name || 'file';
  const metaLine = [ext ? ext.toUpperCase() : null, formatBytes(meta?.size) || null].filter(Boolean).join(' · ');
  // Show the preview ("eye") only for kinds we can render (gated on name + the
  // server's content-type). In-app preview is restricted to our own same-origin
  // proxy files — a non-proxy URL must never be auto-fetched (it'd leak the
  // viewer's network to a third-party host), so those keep a plain "open in new
  // tab" eye instead.
  const proxy = isProxyMediaUrl(href);
  // Gate the preview eye with the shared render classifier (not just the text-only previewKind) so the
  // chat offers in-app preview for every kind the kernel can render — PDF / Office / image included.
  const previewable = previewRenderKind(meta?.name || label, meta?.content_type, meta?.ext) !== null;

  return (
    <span className="my-1 inline-flex min-w-[240px] max-w-full items-center gap-3 rounded-[10px] border border-border bg-surface-2 px-3 py-2.5 align-middle no-underline">
      <span className={cn('grid size-10 shrink-0 place-items-center rounded-lg', tile)}>
        <Icon className="size-5" />
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[13px] font-semibold text-foreground">{title}</span>
        {metaLine && <span className="font-mono text-[10px] text-muted">{metaLine}</span>}
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-1.5">
        {previewable &&
          (proxy && viewer ? (
            <Button
              variant="ghost"
              size="icon"
              className="size-8"
              aria-label={t('chat.media.preview')}
              onClick={() => viewer.open({ url: href, name: meta?.name || label })}
            >
              <Eye className="size-4" />
            </Button>
          ) : (
            <Button asChild variant="ghost" size="icon" className="size-8" aria-label={t('chat.media.preview')}>
              <a href={href} target="_blank" rel="noopener noreferrer">
                <Eye className="size-4" />
              </a>
            </Button>
          ))}
        <Button asChild variant="ghost" size="icon" className="size-8 text-mint" aria-label={t('chat.media.download')}>
          <a
            href={mediaDownloadHref(href)}
            download
            onClick={(e) => handleMediaDownloadClick(e, href, meta?.name || label)}
          >
            <Download className="size-4" />
          </a>
        </Button>
      </span>
    </span>
  );
};
