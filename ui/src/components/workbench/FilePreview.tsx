import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { Markdown } from '../ui/markdown';
import { contentUrl, readArrayBuffer } from '../../lib/filesApi';

// Reusable RENDERED (non-editor) file preview for the windowed apps. Covers what the editor can't
// show as text: images, rendered Markdown, and rich documents (DOCX / XLSX / PPTX / PDF). The heavy
// Office parsers are dynamic-imported inside each sub-view, so they only load when that document
// type is actually opened (one lazy chunk per lib), matching how Monaco / the chat FileViewer load.
type FilePreviewProps =
  | { kind: 'image'; src: string; name?: string; className?: string }
  | { kind: 'markdown'; content: string; className?: string }
  | { kind: 'docx' | 'xlsx' | 'pptx' | 'pdf'; path: string; name?: string; className?: string };

export const FilePreview: React.FC<FilePreviewProps> = (props) => {
  if (props.kind === 'markdown') {
    return (
      <div className={clsx('h-full min-h-0 overflow-auto bg-surface', props.className)}>
        <Markdown content={props.content} interactive={false} className="vr-fileview-md mx-auto max-w-3xl px-6 py-5" />
      </div>
    );
  }
  if (props.kind === 'image') return <ImagePreview src={props.src} name={props.name} className={props.className} />;
  if (props.kind === 'pdf') return <PdfView path={props.path} className={props.className} />;
  if (props.kind === 'docx') return <DocxView path={props.path} className={props.className} />;
  if (props.kind === 'xlsx') return <XlsxView path={props.path} className={props.className} />;
  return <PptxView path={props.path} className={props.className} />;
};

// Shared loading / error chrome for the document views.
const Centered: React.FC<{ className?: string; children: React.ReactNode }> = ({ className, children }) => (
  <div className={clsx('grid h-full min-h-0 place-items-center bg-surface p-6 text-center text-[12.5px] text-muted', className)}>{children}</div>
);

// Fit-to-container by default; click toggles 1:1 actual size (zoom-in / zoom-out cursor).
const ImagePreview: React.FC<{ src: string; name?: string; className?: string }> = ({ src, name, className }) => {
  const { t } = useTranslation();
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [actual, setActual] = useState(false);
  useEffect(() => {
    setStatus('loading');
    setActual(false);
  }, [src]);

  if (status === 'error') return <Centered className={clsx('!bg-[#0c0c0f]', className)}>{t('apps.fileBrowser.previewFailed')}</Centered>;
  return (
    <div className={clsx('grid h-full min-h-0 place-items-center overflow-auto bg-[#0c0c0f] p-4', className)}>
      {status === 'loading' && <div className="col-start-1 row-start-1 text-[12px] text-muted">{t('common.loading')}</div>}
      <img
        src={src}
        alt={name || ''}
        onLoad={() => setStatus('ready')}
        onError={() => setStatus('error')}
        onClick={() => setActual((a) => !a)}
        draggable={false}
        className={clsx('col-start-1 row-start-1 select-none', actual ? 'max-w-none cursor-zoom-out' : 'max-h-full max-w-full cursor-zoom-in object-contain', status !== 'ready' && 'opacity-0')}
      />
    </div>
  );
};

// PDF: the browser's built-in viewer via an <iframe>. The backend serves PDF inline, so no JS engine
// is shipped. Same-origin, so it frames without CSP trouble.
const PdfView: React.FC<{ path: string; className?: string }> = ({ path, className }) => {
  const { t } = useTranslation();
  return (
    <iframe
      title={t('apps.fileBrowser.preview')}
      src={contentUrl(path)}
      className={clsx('h-full w-full border-0 bg-white', className)}
    />
  );
};

// Small hook: fetch the file bytes once per path, exposing loading/error. Shared by the Office views.
function useFileBytes(path: string) {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; bytes: ArrayBuffer | null }>({ status: 'loading', bytes: null });
  useEffect(() => {
    let alive = true;
    setState({ status: 'loading', bytes: null });
    readArrayBuffer(path)
      .then((b) => alive && setState({ status: 'ready', bytes: b }))
      .catch(() => alive && setState({ status: 'error', bytes: null }));
    return () => {
      alive = false;
    };
  }, [path]);
  return state;
}

// DOCX → docx-preview renders the document into a scrollable container (white pages on a neutral mat,
// like a document viewer). The lib + its styles are dynamic-imported, so they only load for .docx.
const DocxView: React.FC<{ path: string; className?: string }> = ({ path, className }) => {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const { status, bytes } = useFileBytes(path);
  const [render, setRender] = useState<'idle' | 'done' | 'error'>('idle');

  useEffect(() => {
    if (status !== 'ready' || !bytes || !ref.current) return;
    let alive = true;
    const container = ref.current;
    setRender('idle');
    (async () => {
      try {
        const { renderAsync } = await import('docx-preview');
        if (!alive) return;
        container.innerHTML = '';
        await renderAsync(bytes, container, undefined, { className: 'docx-rendered', inWrapper: true, ignoreLastRenderedPageBreak: true });
        if (alive) setRender('done');
      } catch {
        if (alive) setRender('error');
      }
    })();
    return () => {
      alive = false;
    };
  }, [status, bytes]);

  if (status === 'error' || render === 'error') return <Centered className={className}>{t('apps.fileBrowser.previewFailed')}</Centered>;
  return (
    <div className={clsx('relative h-full min-h-0 overflow-auto bg-neutral-200 p-4', className)}>
      {(status === 'loading' || render === 'idle') && (
        <div className="absolute inset-0 grid place-items-center text-[12px] text-muted">
          <Loader2 className="size-5 animate-spin" />
        </div>
      )}
      <div ref={ref} className="mx-auto" />
    </div>
  );
};

// XLSX → SheetJS parses then renders each sheet as an HTML table; a tab bar switches sheets. sheet_to_html
// HTML-escapes cell text, so the injected markup is safe. Cell styling is added via the wrapper.
const XlsxView: React.FC<{ path: string; className?: string }> = ({ path, className }) => {
  const { t } = useTranslation();
  const { status, bytes } = useFileBytes(path);
  const [sheets, setSheets] = useState<{ name: string; html: string; truncated: boolean }[] | null>(null);
  const [active, setActive] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (status !== 'ready' || !bytes) return;
    let alive = true;
    setSheets(null);
    setActive(0);
    setFailed(false);
    (async () => {
      try {
        const XLSX = await import('xlsx');
        if (!alive) return;
        const wb = XLSX.read(bytes, { type: 'array' });
        // Clamp the rendered range: sheet_to_html mounts every cell into the DOM, so a huge sheet
        // would freeze the tab. Bound it (and flag it) like the chat CSV viewer does.
        const MAX_ROWS = 1000;
        const MAX_COLS = 60;
        const out = wb.SheetNames.map((name) => {
          const ws = wb.Sheets[name];
          let truncated = false;
          if (ws['!ref']) {
            const r = XLSX.utils.decode_range(ws['!ref']);
            if (r.e.r - r.s.r > MAX_ROWS) {
              r.e.r = r.s.r + MAX_ROWS;
              truncated = true;
            }
            if (r.e.c - r.s.c > MAX_COLS) {
              r.e.c = r.s.c + MAX_COLS;
              truncated = true;
            }
            ws['!ref'] = XLSX.utils.encode_range(r);
          }
          return { name, html: XLSX.utils.sheet_to_html(ws, { editable: false }), truncated };
        });
        if (alive) setSheets(out);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [status, bytes]);

  if (status === 'error' || failed) return <Centered className={className}>{t('apps.fileBrowser.previewFailed')}</Centered>;
  if (!sheets) return <Centered className={className}><Loader2 className="size-5 animate-spin" /></Centered>;
  if (sheets.length === 0) return <Centered className={className}>{t('apps.fileBrowser.empty')}</Centered>;

  return (
    <div className={clsx('flex h-full min-h-0 flex-col bg-white text-neutral-900', className)}>
      {sheets[active]?.truncated && (
        <div className="shrink-0 border-b border-amber-300 bg-amber-50 px-3 py-1 text-[11.5px] text-amber-800">{t('apps.fileBrowser.previewTruncated')}</div>
      )}
      <div
        className="min-h-0 flex-1 overflow-auto p-2 text-[12px] [&_table]:border-collapse [&_td]:border [&_td]:border-neutral-300 [&_td]:px-2 [&_td]:py-0.5 [&_th]:border [&_th]:border-neutral-300 [&_th]:bg-neutral-100 [&_th]:px-2 [&_th]:py-0.5"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: sheets[active]?.html ?? '' }}
      />
      {sheets.length > 1 && (
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-t border-border bg-surface-2 px-2 py-1">
          {sheets.map((s, i) => (
            <button
              key={s.name + i}
              type="button"
              onClick={() => setActive(i)}
              className={clsx('shrink-0 rounded px-2 py-0.5 text-[11.5px] transition', i === active ? 'bg-cyan-soft text-foreground' : 'text-muted hover:bg-foreground/[0.06] hover:text-foreground')}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

// PPTX → PptxViewJS renders one slide to a canvas; a control bar pages through. Canvas (not HTML), so
// text isn't selectable, but it's read-only browsing. The lib is dynamic-imported for .pptx only.
const PptxView: React.FC<{ path: string; className?: string }> = ({ path, className }) => {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewerRef = useRef<{ goToSlide: (i: number, c?: HTMLCanvasElement | null) => Promise<unknown>; destroy: () => void } | null>(null);
  const { status, bytes } = useFileBytes(path);
  const [count, setCount] = useState(0);
  const [index, setIndex] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (status !== 'ready' || !bytes || !canvasRef.current) return;
    let alive = true;
    setFailed(false);
    setCount(0);
    setIndex(0);
    (async () => {
      try {
        const { PPTXViewer } = await import('pptxviewjs');
        if (!alive || !canvasRef.current) return;
        const viewer = new PPTXViewer({ canvas: canvasRef.current, slideSizeMode: 'fit' });
        await viewer.loadFile(bytes);
        if (!alive) {
          viewer.destroy();
          return;
        }
        viewerRef.current = viewer;
        setCount(viewer.getSlideCount());
        await viewer.renderSlide(0, canvasRef.current);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
      viewerRef.current?.destroy();
      viewerRef.current = null;
    };
  }, [status, bytes]);

  const go = (next: number) => {
    if (!viewerRef.current || next < 0 || next >= count) return;
    setIndex(next);
    void viewerRef.current.goToSlide(next, canvasRef.current);
  };

  if (status === 'error' || failed) return <Centered className={className}>{t('apps.fileBrowser.previewFailed')}</Centered>;
  return (
    <div className={clsx('flex h-full min-h-0 flex-col bg-[#0c0c0f]', className)}>
      <div className="grid min-h-0 flex-1 place-items-center overflow-auto p-4">
        {status === 'loading' && <div className="col-start-1 row-start-1 text-[12px] text-muted"><Loader2 className="size-5 animate-spin" /></div>}
        <canvas ref={canvasRef} className="col-start-1 row-start-1 max-h-full max-w-full" />
      </div>
      {count > 1 && (
        <div className="flex shrink-0 items-center justify-center gap-3 border-t border-border bg-surface-2 px-2 py-1.5 text-[12px] text-muted">
          <button type="button" disabled={index <= 0} onClick={() => go(index - 1)} className="grid size-6 place-items-center rounded transition hover:bg-foreground/10 hover:text-foreground disabled:opacity-30">
            <ChevronLeft className="size-4" />
          </button>
          <span className="tabular-nums">{index + 1} / {count}</span>
          <button type="button" disabled={index >= count - 1} onClick={() => go(index + 1)} className="grid size-6 place-items-center rounded transition hover:bg-foreground/10 hover:text-foreground disabled:opacity-30">
            <ChevronRight className="size-4" />
          </button>
        </div>
      )}
    </div>
  );
};
