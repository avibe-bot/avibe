import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Download, FileText } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { FilePreview } from '@/components/ui/file-preview';
import { apiFetch } from '@/lib/apiFetch';
import { handleMediaDownloadClick } from '@/lib/downloadMedia';
import { isProxyMediaUrl } from '@/lib/mediaProxy';
import { formatBytes } from '@/lib/filePreview';
import { copyTextToClipboard } from '@/lib/utils';
import type { FilePreviewTarget } from '@/components/ui/file-viewer';

// The chat file viewer: a Dialog shell (header + copy/download) around the shared <FilePreview>
// kernel, which owns every renderer (Shiki, the JSON tree, papaparse, the Office parsers, images, PDF,
// HTML) and lazy-loads each. Default export so ``React.lazy`` can split it out of the main bundle.
//
// A lightweight /meta fetch resolves the real name / size / content-type before rendering, so the
// kernel can pick the right renderer for a proxy URL whose path is just a token. In-app preview is
// restricted to our own same-origin media proxy — a non-proxy URL is never auto-fetched (it would
// leak the viewer's network to a third-party host).

type Meta = { name: string; size: number | null; mime: string | null; ext: string | null };

export default function FileViewerModal({ target, onClose }: { target: FilePreviewTarget; onClose: () => void }) {
  const { t } = useTranslation();
  const proxy = isProxyMediaUrl(target.url);
  const [meta, setMeta] = React.useState<Meta>(() => ({ name: target.name || '', size: null, mime: null, ext: null }));
  const [metaLoaded, setMetaLoaded] = React.useState(false);
  // The kernel reports the file's text when it loads a text kind — enables the copy button (and stays
  // null for image / pdf / office, where copy is meaningless).
  const [text, setText] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!proxy) return; // non-proxy → never fetch; the body shows an error instead
    let alive = true;
    apiFetch(`${target.url}/meta`, { headers: { Accept: 'application/json' } })
      .then((res) => (res.ok ? res.json() : null))
      .then((m: { name?: string; size?: number; content_type?: string; ext?: string } | null) => {
        if (!alive) return;
        if (m) {
          setMeta({
            name: m.name || target.name || '',
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
  }, [target.url, target.name, proxy]);

  const copy = async () => {
    if (text == null) return;
    if (await copyTextToClipboard(text)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    }
  };

  const name = meta.name || target.name || '';
  const ext = name.includes('.') ? (name.split('.').pop() || '').toUpperCase() : '';
  const metaLine = [ext || null, formatBytes(meta.size) || null].filter(Boolean).join(' · ');

  let body: React.ReactNode;
  if (!proxy) body = <div className="vr-fileview-msg">{t('preview.failed')}</div>;
  else if (!metaLoaded) body = <div className="vr-fileview-msg">{t('common.loading')}</div>;
  else body = <FilePreview source={{ url: target.url, name, ext: meta.ext, mime: meta.mime, size: meta.size }} onText={setText} />;

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
          <Button asChild variant="ghost" size="icon" className="size-8 text-mint" aria-label={t('chat.media.download')}>
            <a href={`${target.url}?download=1`} download onClick={(e) => handleMediaDownloadClick(e, target.url, name || undefined)}>
              <Download className="size-4" />
            </a>
          </Button>
        </div>
        <div className="vr-fileview-body min-h-0 flex-1 overflow-hidden">{body}</div>
      </DialogContent>
    </Dialog>
  );
}
