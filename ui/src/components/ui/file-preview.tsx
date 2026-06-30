import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { Markdown } from '@/components/ui/markdown';
import { useTheme } from '@/context/ThemeContext';
import { apiFetch } from '@/lib/apiFetch';
import {
  CODE_HIGHLIGHT_MAX_BYTES,
  CSV_MAX_COLS,
  CSV_MAX_ROWS,
  JSON_TREE_MAX_BYTES,
  JSON_TREE_MAX_NODES,
  PREVIEW_MAX_BYTES,
  codeLanguage,
  previewRenderKind,
} from '@/lib/filePreview';

// ── Shared file-preview kernel ("Quick Look") ───────────────────────────────
// One read-only renderer for every previewable file, reused by the File Browser overlay, the editor's
// Source ⇄ Preview toggle, and the chat file viewer. A `source` is either a same-origin content `url`
// (fetched with apiFetch) or in-memory `text` (the editor's live, possibly-unsaved buffer). The kind
// is resolved from the name/mime (``previewRenderKind``), and every heavy engine — Shiki, the JSON
// tree, papaparse, and the Office parsers (docx-preview / SheetJS / PptxViewJS) — is dynamic-imported
// inside its own renderer, so none of them reach the apps/main bundle until that file type is opened.

export type PreviewSource = {
  name: string;
  mime?: string | null;
  /** Server-supplied extension (e.g. chat /meta `ext`) — used to classify when the name is just a
   *  descriptive label whose suffix doesn't match the real file type. */
  ext?: string | null;
  /** Same-origin, apiFetch-able content URL. Required for image / pdf / office; default fetch for text. */
  url?: string;
  /** In-memory text (the editor's live buffer). Used for text-derived kinds instead of fetching `url`. */
  text?: string;
  /** Known byte size, so an oversized text file is refused before being pulled into the page. */
  size?: number | null;
};

// JSON tree is the one renderer whose dep can't be dynamic-imported inline (it's a component), so it
// lives in its own module and loads lazily here.
const PreviewJson = React.lazy(() => import('@/components/ui/preview-json'));

export const FilePreview: React.FC<{ source: PreviewSource; className?: string; onText?: (text: string) => void }> = ({
  source,
  className,
  onText,
}) => {
  const { t } = useTranslation();
  const kind = previewRenderKind(source.name, source.mime, source.ext);

  if (!kind) return <Centered className={className}>{t('preview.unsupported')}</Centered>;
  if (kind === 'image')
    return source.url ? <ImageBody src={source.url} name={source.name} className={className} /> : <Centered className={className}>{t('preview.failed')}</Centered>;
  if (kind === 'pdf')
    return source.url ? <PdfView url={source.url} className={className} /> : <Centered className={className}>{t('preview.failed')}</Centered>;
  if (kind === 'docx' || kind === 'xlsx' || kind === 'pptx') {
    if (!source.url) return <Centered className={className}>{t('preview.failed')}</Centered>;
    if (kind === 'docx') return <DocxView url={source.url} className={className} />;
    if (kind === 'xlsx') return <XlsxView url={source.url} className={className} />;
    return <PptxView url={source.url} className={className} />;
  }
  // svg / html / markdown / json / csv / code — all derived from the file's text.
  return <TextPreview source={source} kind={kind} onText={onText} className={className} />;
};

// Shared loading / message chrome.
const Centered: React.FC<{ className?: string; children: React.ReactNode }> = ({ className, children }) => (
  <div className={clsx('grid h-full min-h-0 place-items-center bg-surface p-6 text-center text-[12.5px] text-muted', className)}>{children}</div>
);

const Spinner: React.FC<{ className?: string }> = ({ className }) => (
  <Centered className={className}>
    <Loader2 className="size-5 animate-spin" />
  </Centered>
);

// ── Image / SVG ──────────────────────────────────────────────────────────────
// Fit-to-container by default; click toggles 1:1 actual size. `src` is a content URL (raster) or a
// data: URL (SVG rendered from its text) — an <img> neutralizes any script in an SVG, so it's safe.
const ImageBody: React.FC<{ src: string; name?: string; className?: string }> = ({ src, name, className }) => {
  const { t } = useTranslation();
  const [status, setStatus] = React.useState<'loading' | 'ready' | 'error'>('loading');
  const [actual, setActual] = React.useState(false);
  React.useEffect(() => {
    setStatus('loading');
    setActual(false);
  }, [src]);

  if (status === 'error') return <Centered className={clsx('!bg-[#0c0c0f]', className)}>{t('preview.failed')}</Centered>;
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
        className={clsx(
          'col-start-1 row-start-1 select-none',
          actual ? 'max-w-none cursor-zoom-out' : 'max-h-full max-w-full cursor-zoom-in object-contain',
          status !== 'ready' && 'opacity-0',
        )}
      />
    </div>
  );
};

// ── PDF ───────────────────────────────────────────────────────────────────────
// The browser's built-in viewer via an <iframe>. The backend serves PDF inline (same-origin), so no
// JS engine is shipped and it frames without CSP trouble.
const PdfView: React.FC<{ url: string; className?: string }> = ({ url, className }) => {
  const { t } = useTranslation();
  return <iframe title={t('preview.title')} src={url} className={clsx('h-full w-full border-0 bg-white', className)} />;
};

// ── HTML ──────────────────────────────────────────────────────────────────────
// Render the markup in a fully sandboxed iframe: `sandbox=""` disables scripts, same-origin access,
// forms, and popups. But sandbox does NOT stop subresource fetches — a `<img src="https://…">` or CSS
// import in an untrusted file would still hit the network and leak the viewer's IP on preview. So we
// prepend a restrictive CSP <meta> (parsed before any subresource): network pinned to nothing, only
// inline styles + data: URIs allowed. Relative/remote asset references won't resolve — acceptable for
// a static, no-network preview (mirrors the no-auto-fetch policy of chat Markdown previews).
const HTML_PREVIEW_CSP =
  "default-src 'none'; img-src data:; media-src data:; font-src data:; style-src 'unsafe-inline' data:; base-uri 'none'; form-action 'none'";
const HtmlView: React.FC<{ html: string; className?: string }> = ({ html, className }) => {
  const { t } = useTranslation();
  const safeHtml = `<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_CSP}">\n${html}`;
  return (
    <iframe
      title={t('preview.title')}
      sandbox=""
      referrerPolicy="no-referrer"
      srcDoc={safeHtml}
      className={clsx('h-full w-full border-0 bg-white', className)}
    />
  );
};

// ── Office (docx / xlsx / pptx) ────────────────────────────────────────────────
// Fetch the file bytes once per URL. Shared by the Office views; their parsers want an ArrayBuffer.
function useFileBytes(url: string) {
  const [state, setState] = React.useState<{ status: 'loading' | 'ready' | 'error'; bytes: ArrayBuffer | null }>({ status: 'loading', bytes: null });
  React.useEffect(() => {
    let alive = true;
    setState({ status: 'loading', bytes: null });
    (async () => {
      try {
        const res = await apiFetch(url, { headers: { Accept: '*/*' } });
        if (!res.ok) throw new Error(`http ${res.status}`);
        const bytes = await res.arrayBuffer();
        if (alive) setState({ status: 'ready', bytes });
      } catch {
        if (alive) setState({ status: 'error', bytes: null });
      }
    })();
    return () => {
      alive = false;
    };
  }, [url]);
  return state;
}

// DOCX → docx-preview renders the document into a scrollable container (white pages on a neutral mat).
const DocxView: React.FC<{ url: string; className?: string }> = ({ url, className }) => {
  const { t } = useTranslation();
  const ref = React.useRef<HTMLDivElement>(null);
  const { status, bytes } = useFileBytes(url);
  const [render, setRender] = React.useState<'idle' | 'done' | 'error'>('idle');

  React.useEffect(() => {
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

  if (status === 'error' || render === 'error') return <Centered className={className}>{t('preview.failed')}</Centered>;
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

// XLSX → SheetJS parses, then each sheet renders as an HTML table; a tab bar switches sheets.
// sheet_to_html HTML-escapes cell text, so the injected markup is safe. The render range is clamped
// (rows × cols) because sheet_to_html mounts every cell into the DOM — a huge sheet would freeze.
const XlsxView: React.FC<{ url: string; className?: string }> = ({ url, className }) => {
  const { t } = useTranslation();
  const { status, bytes } = useFileBytes(url);
  const [sheets, setSheets] = React.useState<{ name: string; html: string; truncated: boolean }[] | null>(null);
  const [active, setActive] = React.useState(0);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
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

  if (status === 'error' || failed) return <Centered className={className}>{t('preview.failed')}</Centered>;
  if (!sheets) return <Spinner className={className} />;
  if (sheets.length === 0) return <Centered className={className}>{t('preview.empty')}</Centered>;

  return (
    <div className={clsx('flex h-full min-h-0 flex-col bg-white text-neutral-900', className)}>
      {sheets[active]?.truncated && (
        <div className="shrink-0 border-b border-amber-300 bg-amber-50 px-3 py-1 text-[11.5px] text-amber-800">{t('preview.truncated')}</div>
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
              className={clsx(
                'shrink-0 rounded px-2 py-0.5 text-[11.5px] transition',
                i === active ? 'bg-cyan-soft text-foreground' : 'text-muted hover:bg-foreground/[0.06] hover:text-foreground',
              )}
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
// text isn't selectable, but it's read-only browsing.
const PptxView: React.FC<{ url: string; className?: string }> = ({ url, className }) => {
  const { t } = useTranslation();
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const viewerRef = React.useRef<{ goToSlide: (i: number, c?: HTMLCanvasElement | null) => Promise<unknown>; destroy: () => void } | null>(null);
  const { status, bytes } = useFileBytes(url);
  const [count, setCount] = React.useState(0);
  const [index, setIndex] = React.useState(0);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
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
        await viewer.render(canvasRef.current); // canonical first paint (renders the current slide, 0)
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

  if (status === 'error' || failed) return <Centered className={className}>{t('preview.failed')}</Centered>;
  return (
    <div className={clsx('flex h-full min-h-0 flex-col bg-[#0c0c0f]', className)}>
      <div className="grid min-h-0 flex-1 place-items-center overflow-auto p-4">
        {status === 'loading' && (
          <div className="col-start-1 row-start-1 text-[12px] text-muted">
            <Loader2 className="size-5 animate-spin" />
          </div>
        )}
        <canvas ref={canvasRef} className="col-start-1 row-start-1 max-h-full max-w-full" />
      </div>
      {count > 1 && (
        <div className="flex shrink-0 items-center justify-center gap-3 border-t border-border bg-surface-2 px-2 py-1.5 text-[12px] text-muted">
          <button type="button" disabled={index <= 0} onClick={() => go(index - 1)} className="grid size-6 place-items-center rounded transition hover:bg-foreground/10 hover:text-foreground disabled:opacity-30">
            <ChevronLeft className="size-4" />
          </button>
          <span className="tabular-nums">
            {index + 1} / {count}
          </span>
          <button type="button" disabled={index >= count - 1} onClick={() => go(index + 1)} className="grid size-6 place-items-center rounded transition hover:bg-foreground/10 hover:text-foreground disabled:opacity-30">
            <ChevronRight className="size-4" />
          </button>
        </div>
      )}
    </div>
  );
};

// ── Text-derived kinds (svg / html / markdown / json / csv / code) ─────────────
type TextKind = 'svg' | 'html' | 'markdown' | 'json' | 'csv' | 'code';

const TextPreview: React.FC<{ source: PreviewSource; kind: TextKind; onText?: (text: string) => void; className?: string }> = ({ source, kind, onText, className }) => {
  const { t } = useTranslation();
  const onTextRef = React.useRef(onText);
  onTextRef.current = onText;
  const [state, setState] = React.useState<{ phase: 'loading' | 'ready' | 'error' | 'toolarge'; text: string }>(() =>
    source.text != null ? { phase: 'ready', text: source.text } : { phase: 'loading', text: '' },
  );

  React.useEffect(() => {
    if (source.text != null) {
      setState({ phase: 'ready', text: source.text });
      onTextRef.current?.(source.text);
      return;
    }
    const url = source.url;
    if (!url) {
      setState({ phase: 'error', text: '' });
      return;
    }
    let alive = true;
    setState({ phase: 'loading', text: '' });
    (async () => {
      try {
        if (source.size != null && source.size > PREVIEW_MAX_BYTES) {
          if (alive) setState({ phase: 'toolarge', text: '' });
          return;
        }
        const res = await apiFetch(url, { headers: { Accept: '*/*' } });
        if (!alive) return;
        if (!res.ok) {
          setState({ phase: 'error', text: '' });
          return;
        }
        // Refuse a huge body by Content-Length before reading it in, then a final guard for the
        // (rare) chunked / no-Content-Length case.
        const len = Number(res.headers.get('content-length'));
        if (Number.isFinite(len) && len > PREVIEW_MAX_BYTES) {
          setState({ phase: 'toolarge', text: '' });
          return;
        }
        const text = await res.text();
        if (!alive) return;
        if (text.length > PREVIEW_MAX_BYTES) {
          setState({ phase: 'toolarge', text: '' });
          return;
        }
        setState({ phase: 'ready', text });
        onTextRef.current?.(text);
      } catch {
        if (alive) setState({ phase: 'error', text: '' });
      }
    })();
    return () => {
      alive = false;
    };
  }, [source.url, source.text, source.size]);

  if (state.phase === 'loading') return <Spinner className={className} />;
  if (state.phase === 'toolarge') return <Centered className={className}>{t('preview.tooLarge')}</Centered>;
  if (state.phase === 'error') return <Centered className={className}>{t('preview.failed')}</Centered>;

  const text = state.text;
  if (kind === 'markdown')
    return (
      <div className={clsx('h-full min-h-0 overflow-auto bg-surface', className)}>
        <Markdown content={text} interactive={false} className="vr-fileview-md mx-auto max-w-3xl px-6 py-5" />
      </div>
    );
  if (kind === 'svg') return <ImageBody src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(text)}`} name={source.name} className={className} />;
  if (kind === 'html') return <HtmlView html={text} className={className} />;
  if (kind === 'json') return <JsonBlock text={text} className={className} />;
  if (kind === 'csv') return <CsvTable text={text} className={className} />;
  return <CodeBlock code={text} lang={codeLanguage(source.name, source.ext)} className={className} />;
};

// Scroll container shared by the text renderers (their inner markup owns its own padding via the
// .vr-fileview-* rules, so this only provides the sized, scrollable box).
const TextScroll: React.FC<{ className?: string; children: React.ReactNode }> = ({ className, children }) => (
  <div className={clsx('vr-fileview-body h-full min-h-0 overflow-auto bg-surface', className)}>{children}</div>
);

// Code / source: highlight asynchronously (Shiki + grammar, both dynamic-imported), falling back to
// plain escaped text while pending, when the file is too big to tokenize, or if highlighting fails.
const CodeBlock: React.FC<{ code: string; lang: string; className?: string }> = ({ code, lang, className }) => {
  const { resolvedTheme } = useTheme();
  const [html, setHtml] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (code.length > CODE_HIGHLIGHT_MAX_BYTES) return; // too big — keep the plain <pre> fallback
    let alive = true;
    setHtml(null);
    import('@/lib/highlighter')
      .then(({ highlightCode }) => highlightCode(code, lang, resolvedTheme === 'light' ? 'github-light' : 'github-dark'))
      .then((out) => alive && setHtml(out))
      .catch(() => alive && setHtml(null));
    return () => {
      alive = false;
    };
  }, [code, lang, resolvedTheme]);
  // Shiki escapes the code text, so the HTML is safe to inject.
  if (html) return <TextScroll className={className}><div className="vr-fileview-code" dangerouslySetInnerHTML={{ __html: html }} /></TextScroll>;
  return (
    <TextScroll className={className}>
      <pre className="vr-fileview-pre">{code}</pre>
    </TextScroll>
  );
};

const CsvTable: React.FC<{ text: string; className?: string }> = ({ text, className }) => {
  const { t } = useTranslation();
  const [parsed, setParsed] = React.useState<{ rows: string[][]; more: boolean; cols: number } | null>(null);
  React.useEffect(() => {
    let alive = true;
    setParsed(null);
    (async () => {
      try {
        const Papa = (await import('papaparse')).default;
        if (!alive) return;
        // Don't trim: leading whitespace / an empty first cell is data. ``preview`` stops Papa after
        // the rows we show (+1 to detect "there's more"), so a giant file isn't fully materialized.
        const out = Papa.parse<string[]>(text, { skipEmptyLines: true, preview: CSV_MAX_ROWS + 1 });
        const all = (out.data || []) as string[][];
        const shown = all.slice(0, CSV_MAX_ROWS);
        const cols = shown.reduce((max, r) => Math.max(max, r.length), 0); // widest row, not the first
        if (alive) setParsed({ rows: shown, more: all.length > CSV_MAX_ROWS, cols });
      } catch {
        if (alive) setParsed({ rows: [], more: false, cols: 0 });
      }
    })();
    return () => {
      alive = false;
    };
  }, [text]);

  if (!parsed) return <Spinner className={className} />;
  const { rows, more, cols } = parsed;
  if (rows.length === 0 || cols === 0)
    return (
      <TextScroll className={className}>
        <pre className="vr-fileview-pre">{text}</pre>
      </TextScroll>
    );
  const [head, ...bodyRows] = rows;
  const shownCols = Math.min(cols, CSV_MAX_COLS); // cap a pathologically wide row too
  const colIdx = Array.from({ length: shownCols }, (_, i) => i);
  const colsTruncated = cols > shownCols;
  return (
    <TextScroll className={className}>
      <div className="vr-fileview-csv">
        <table className="vr-fileview-table">
          <thead>
            <tr>{colIdx.map((ci) => <th key={ci}>{head[ci] ?? ''}</th>)}</tr>
          </thead>
          <tbody>
            {bodyRows.map((r, ri) => (
              <tr key={ri}>{colIdx.map((ci) => <td key={ci}>{r[ci] ?? ''}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {(more || colsTruncated) && (
        <div className="vr-fileview-note">
          {[more ? t('preview.csvTruncated', { count: rows.length }) : null, colsTruncated ? t('preview.csvColsTruncated', { shown: shownCols, total: cols }) : null]
            .filter(Boolean)
            .join(' · ')}
        </div>
      )}
    </TextScroll>
  );
};

// Count nodes in a parsed JSON value, short-circuiting past the limit. Iterative to avoid deep recursion.
function jsonNodeCount(root: unknown, limit: number): number {
  let count = 0;
  const stack: unknown[] = [root];
  while (stack.length > 0) {
    const v = stack.pop();
    count += 1;
    if (count > limit) return count;
    if (v !== null && typeof v === 'object') {
      const children = Array.isArray(v) ? v : Object.values(v as Record<string, unknown>);
      for (let i = 0; i < children.length; i += 1) stack.push(children[i]);
    }
  }
  return count;
}

const JsonBlock: React.FC<{ text: string; className?: string }> = ({ text, className }) => {
  const parsed = React.useMemo<{ ok: boolean; value: unknown }>(() => {
    try {
      return { ok: true, value: JSON.parse(text) };
    } catch {
      return { ok: false, value: null };
    }
  }, [text]);
  const isObject = parsed.ok && parsed.value !== null && typeof parsed.value === 'object';
  // The interactive tree mounts every node into the DOM. Fall back to highlighted source for a
  // primitive root (JsonView wants an object/array), invalid JSON, or anything too big — by bytes OR
  // node count, since compact arrays stay under the byte cap but still explode the DOM.
  const tooManyNodes = React.useMemo(
    () => (isObject ? jsonNodeCount(parsed.value, JSON_TREE_MAX_NODES) > JSON_TREE_MAX_NODES : false),
    [isObject, parsed.value],
  );
  if (!isObject || text.length > JSON_TREE_MAX_BYTES || tooManyNodes) return <CodeBlock code={text} lang="json" className={className} />;
  return (
    <TextScroll className={className}>
      <React.Suspense fallback={<div className="p-4 text-[12px] text-muted">…</div>}>
        <PreviewJson value={parsed.value as object} />
      </React.Suspense>
    </TextScroll>
  );
};
