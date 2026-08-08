import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Download, FileText } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { FilePreview } from '@/components/ui/file-preview';
import { apiFetch } from '@/lib/apiFetch';
import { handleMediaDownloadClick } from '@/lib/downloadMedia';
import { contentUrl, downloadFile } from '@/lib/filesApi';
import { isProxyMediaUrl } from '@/lib/mediaProxy';
import { formatBytes } from '@/lib/filePreview';
import { copyTextToClipboard } from '@/lib/utils';
import type { FilePreviewTarget } from '@/components/ui/file-viewer-context';

// The chat file viewer: a Dialog shell (header + copy/download) around the shared <FilePreview>
// kernel, which owns every renderer (Shiki, the JSON tree, papaparse, the Office parsers, images, PDF,
// HTML) and lazy-loads each. Default export so ``React.lazy`` can split it out of the main bundle.
//
// A lightweight /meta fetch resolves media-proxy metadata before rendering. Local file targets are
// already classified from `/api/files/meta` by the caller and use the same-origin Files content API.
// Arbitrary non-proxy URLs are never auto-fetched, so preview cannot leak a request to a third party.

type Meta = { name: string; size: number | null; mime: string | null; ext: string | null };

export default function FileViewerModal({ target, onClose }: { target: FilePreviewTarget; onClose: () => void }) {
  const { t } = useTranslation();
  const local = target.kind === 'local';
  const localPath = local ? target.path : null;
  const mediaUrl = target.kind === 'local' ? null : target.url;
  const targetName = target.name || '';
  const proxy = mediaUrl ? isProxyMediaUrl(mediaUrl) : false;
  const [meta, setMeta] = React.useState<Meta>(() => target.kind === 'local'
    ? { name: target.name, size: target.size, mime: target.mime, ext: target.ext }
    : { name: targetName, size: null, mime: null, ext: null });
  const [metaLoaded, setMetaLoaded] = React.useState(local);
  // The kernel reports the file's text when it loads a text kind — enables the copy button (and stays
  // null for image / pdf / office, where copy is meaningless).
  const [text, setText] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!proxy || !mediaUrl) return; // local metadata is supplied; arbitrary remote URLs are never fetched
    let alive = true;
    apiFetch(`${mediaUrl}/meta`, { headers: { Accept: 'application/json' } })
      .then((res) => (res.ok ? res.json() : null))
      .then((m: { name?: string; size?: number; content_type?: string; ext?: string } | null) => {
        if (!alive) return;
        if (m) {
          setMeta({
            name: m.name || targetName,
            size: typeof m.size === 'number' ? m.size : null,
            mime: m.content_type || null,
            ext: m.ext || null,
          });
        }
        setMetaLoaded(true);
      })
      .catch(() => {
        if (alive) setMetaLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [mediaUrl, targetName, proxy]);

  const copy = async () => {
    if (text == null) return;
    if (await copyTextToClipboard(text)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  const name = meta.name || targetName;
  const ext = name.includes('.') ? (name.split('.').pop() || '').toUpperCase() : '';
  const metaLine = [ext || null, formatBytes(meta.size) || null].filter(Boolean).join(' · ');

  let body: React.ReactNode;
  const previewUrl = localPath ? contentUrl(localPath) : mediaUrl || '';
  if (!local && !proxy) body = <div className="vr-fileview-msg">{t('preview.failed')}</div>;
  else if (!metaLoaded) body = <div className="vr-fileview-msg">{t('common.loading')}</div>;
  else body = <FilePreview source={{ url: previewUrl, name, ext: meta.ext, mime: meta.mime, size: meta.size }} onText={setText} />;

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {/* Reuse the shared Dialog: overlay, focus-trap, scroll-lock, Escape, outside-click close, the
          built-in top-right close X, and the mobile bottom-sheet all come for free. ``pr-12`` on the
          header leaves room for that close X. */}
      {/* Definite height (not just max-h): the FilePreview kernel scrolls internally via h-full, which
          needs a resolved parent height — a content-driven box would collapse it. */}
      <DialogContent aria-describedby={undefined} className="flex h-[80vh] w-full max-w-3xl flex-col gap-0 overflow-hidden p-0 max-md:h-[82dvh]">
        <div className="flex items-center gap-2 border-b border-border px-4 py-3 pr-12">
          <FileText className="size-4 shrink-0 text-muted" />
          <div className="min-w-0 flex-1">
            <DialogTitle className="truncate text-[13px] font-semibold text-foreground">{name || t('chat.media.preview')}</DialogTitle>
            {metaLine && <div className="font-mono text-[10px] text-muted">{metaLine}</div>}
          </div>
          {text != null && (
            <Button variant="ghost" size="icon" className="size-8" onClick={copy} aria-label={t('common.copy')}>
              {copied ? <Check className="size-4 text-mint" /> : <Copy className="size-4" />}
            </Button>
          )}
          {local && localPath ? (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-8 text-mint"
              aria-label={t('chat.media.download')}
              onClick={() => downloadFile(localPath)}
            >
              <Download className="size-4" />
            </Button>
          ) : (
            <Button asChild variant="ghost" size="icon" className="size-8 text-mint" aria-label={t('chat.media.download')}>
              <a href={`${mediaUrl}?download=1`} download onClick={(e) => handleMediaDownloadClick(e, mediaUrl || '', name || undefined)}>
                <Download className="size-4" />
              </a>
            </Button>
          )}
        </div>
        <div className="vr-fileview-body min-h-0 flex-1 overflow-hidden">{body}</div>
      </DialogContent>
    </Dialog>
  );
}
