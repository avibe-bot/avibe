// Single source of truth for "can we preview this file, and how". Used to gate
// the FileCard preview ("eye") icon and to pick the renderer + syntax-highlight
// language in the FileViewer. Keep the language ids here in sync with the
// grammar loaders in ``highlighter.ts``.

export type PreviewKind = 'markdown' | 'text' | 'json' | 'csv' | 'code' | 'source';

// Refuse to preview files larger than this (fetch the bytes into the page) —
// offer download instead. CSV is additionally capped by row count.
export const PREVIEW_MAX_BYTES = 1024 * 1024;
export const CSV_MAX_ROWS = 500;
// The interactive JSON tree mounts every node into the DOM (collapsed only sets
// the visual state), so above this size we render highlighted JSON source
// instead to avoid freezing the main thread.
export const JSON_TREE_MAX_BYTES = 256 * 1024;
// CSV column cap: a pathologically wide row (e.g. one line with 100k fields)
// would otherwise mount that many cells per row. Extra columns are truncated.
export const CSV_MAX_COLS = 50;
// Above this, skip Shiki and render plain text — tokenizing hundreds of KB on
// the main thread freezes the UI (a large/minified code file or big JSON).
export const CODE_HIGHLIGHT_MAX_BYTES = 200 * 1024;
// Node-count cap for the interactive JSON tree: compact JSON (e.g. a big array
// of numbers) can stay under the byte cap yet hold tens of thousands of nodes,
// which the tree would all mount. Above this we render highlighted source.
export const JSON_TREE_MAX_NODES = 4000;

const MARKDOWN_EXT = new Set(['md', 'markdown', 'mdx', 'mkd', 'mdown']);
const TEXT_EXT = new Set(['txt', 'text', 'log']);
const CSV_EXT = new Set(['csv', 'tsv']);

// Markup rendered as highlighted SOURCE only — never executed (XSS / the server
// already forces these to attachment).
const SOURCE_LANG: Record<string, string> = {
  html: 'html', htm: 'html', xml: 'xml', svg: 'xml', vue: 'vue', svelte: 'svelte',
};

// ext (lowercase, no dot) → Shiki language id.
const CODE_LANG: Record<string, string> = {
  ts: 'typescript', mts: 'typescript', cts: 'typescript', tsx: 'tsx',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx',
  py: 'python', pyi: 'python', rb: 'ruby', go: 'go', rs: 'rust',
  java: 'java', kt: 'kotlin', kts: 'kotlin', swift: 'swift',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp',
  cs: 'csharp', php: 'php',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  sql: 'sql', css: 'css', scss: 'scss', sass: 'scss', less: 'less',
  lua: 'lua', r: 'r', dart: 'dart', scala: 'scala', sc: 'scala', pl: 'perl', pm: 'perl',
  diff: 'diff', patch: 'diff',
  yaml: 'yaml', yml: 'yaml', toml: 'toml',
  ini: 'ini', conf: 'ini', cfg: 'ini', properties: 'ini', env: 'ini',
};

// Files whose meaningful name has no useful extension.
const NAME_LANG: Record<string, string> = {
  dockerfile: 'docker', makefile: 'make', '.gitignore': 'ini', '.gitattributes': 'ini',
  '.env': 'ini', '.bashrc': 'bash', '.zshrc': 'bash', '.profile': 'bash',
};

function baseName(name: string): string {
  return (name || '').split(/[\\/]/).pop() || '';
}

function extOf(name: string): string {
  const b = baseName(name);
  const i = b.lastIndexOf('.');
  return i > 0 ? b.slice(i + 1).toLowerCase() : '';
}

// The extension to classify by. The server-supplied ext wins when present: for chat media the `name`
// can be an arbitrary Markdown label (`[report.pdf](…/report.docx)`) while serverExt/mime are the real
// type — so trusting the label's suffix would route to the wrong renderer. Falls back to the name's
// own extension when no serverExt is given (File Browser / editor, which pass real filenames).
function effectiveExt(name: string, serverExt?: string | null): string {
  return (serverExt || '').replace(/^\.+/, '').toLowerCase() || extOf(name);
}

export function previewKind(name: string, mime?: string | null, serverExt?: string | null): PreviewKind | null {
  const b = baseName(name).toLowerCase();
  const ext = effectiveExt(name, serverExt);
  const m = (mime || '').split(';')[0].trim().toLowerCase();

  if (MARKDOWN_EXT.has(ext)) return 'markdown';
  if (ext === 'json' || ext === 'jsonc' || ext === 'json5' || m === 'application/json') return 'json';
  if (CSV_EXT.has(ext) || m === 'text/csv' || m === 'text/tab-separated-values') return 'csv';
  if (ext in SOURCE_LANG) return 'source';
  if (ext in CODE_LANG || b in NAME_LANG) return 'code';
  if (TEXT_EXT.has(ext) || m.startsWith('text/')) return 'text';
  return null;
}

// Whether a directory entry / file should open in the in-app editor rather than download: a
// regular file (NOT a symlink — the backend refuses to write through one — or a directory), with a
// previewable/text kind, within the size cap. Shared by the File Browser and the editor explorer
// so both gate opens identically.
export function isEditableFile(entry: { kind: string; size: number | null; name: string }): boolean {
  return entry.kind === 'file' && previewKind(entry.name) != null && (entry.size == null || entry.size <= PREVIEW_MAX_BYTES);
}

// Content-aware editability, decided AFTER fetching `/api/files/meta` (which sniffs file content).
// A regular file within the size cap opens in the editor when it's a known text/code type by name
// (`previewKind`) OR the backend sniffed its content as text (`meta.text`) — so extensionless /
// unknown-type text files (LICENSE, README, a `notes` file) edit instead of downloading, while true
// binaries (no text kind, sniffed non-text) still download. The name-only `isEditableFile` stays the
// cheap pre-fetch guess for listings; this is the authoritative open decision.
export function isEditableMeta(meta: { kind: string; size: number | null; name: string; text?: boolean }): boolean {
  if (meta.kind !== 'file') return false;
  if (meta.size != null && meta.size > PREVIEW_MAX_BYTES) return false;
  return previewKind(meta.name) != null || meta.text === true;
}

// Raster image extensions an <img> tag can render directly. SVG is handled separately because it's
// ALSO editable text (it keeps its 'source' previewKind), so it can be both edited and rendered.
const RASTER_IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico', 'apng']);

// Mirrors the backend content cap (core/file_browser_service.MAX_FILE_BYTES): `/api/files/content`
// refuses anything larger with 413, so an oversized image can't be rendered — it must download.
export const IMAGE_PREVIEW_MAX_BYTES = 25 * 1024 * 1024;

export type ImageKind = 'raster' | 'svg';

// Whether a file can be rendered as an image preview, and how. Deliberately separate from
// ``previewKind`` (which classifies EDITABLE text): images have no text kind, and SVG is both an
// image (renderable here) and source (editable in Monaco), so ``previewKind('x.svg') === 'source'``
// must stay true while this still reports it as an image.
export function imageKind(name: string, mime?: string | null, serverExt?: string | null): ImageKind | null {
  const ext = effectiveExt(name, serverExt);
  const m = (mime || '').split(';')[0].trim().toLowerCase();
  if (ext === 'svg' || m === 'image/svg+xml') return 'svg';
  if (RASTER_IMAGE_EXT.has(ext) || (m.startsWith('image/') && m !== 'image/svg+xml')) return 'raster';
  return null;
}

// Rich documents we can preview client-side (read-only): Office files rendered by lazy-loaded libs
// (docx-preview / SheetJS / PptxViewJS) and PDF via the browser's built-in viewer. Kept separate
// from previewKind/imageKind. The parsers pull the whole file into memory, so it's gated by the
// content cap; an oversized doc (which /api/files/content would reject anyway) falls to download.
export type DocPreviewKind = 'docx' | 'xlsx' | 'pptx' | 'pdf';
// NB: csv is intentionally NOT here — it renders as a CSV table (papaparse), lighter than loading
// SheetJS, and matches the chat viewer. ``previewRenderKind`` maps csv → 'csv'.
const DOC_EXT: Record<string, DocPreviewKind> = {
  docx: 'docx',
  xlsx: 'xlsx',
  xlsm: 'xlsx',
  xls: 'xlsx',
  pptx: 'pptx',
  pdf: 'pdf',
};
export const DOC_PREVIEW_MAX_BYTES = IMAGE_PREVIEW_MAX_BYTES; // 25 MB, matches the backend content cap

// Office/PDF content types → kind, so a descriptive-label chat link (whose name carries no real
// suffix) still classifies via the server-supplied content-type. Limited to formats our renderers
// actually read: the OOXML types + PDF, plus legacy .xls (SheetJS reads BIFF). Legacy binary .doc
// (application/msword) and .ppt (application/vnd.ms-powerpoint) are intentionally absent — docx-preview
// and PptxViewJS only load OOXML, so advertising them would open a broken preview instead of download.
const OFFICE_MIME: Record<string, DocPreviewKind> = {
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
  'application/vnd.ms-excel': 'xlsx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  'application/pdf': 'pdf',
};

export function docPreviewKind(name: string, mime?: string | null, serverExt?: string | null): DocPreviewKind | null {
  const ext = effectiveExt(name, serverExt);
  if (ext in DOC_EXT) return DOC_EXT[ext];
  const m = (mime || '').split(';')[0].trim().toLowerCase();
  return OFFICE_MIME[m] ?? null;
}

// ── Unified preview dispatch ────────────────────────────────────────────────
// HOW a file renders in the shared <FilePreview> kernel (one classifier for the File Browser, the
// editor preview, and the chat viewer). Combines imageKind (raster/svg) + html + docPreviewKind
// (office/pdf) + previewKind (text). 'code' is the catch-all for highlightable text — including
// non-HTML markup (xml/vue/svelte) and plain text (highlights to nothing). Order matters: images and
// HTML are decided before the text classifier so an .svg renders as an image, not edited source.
export type PreviewRenderKind =
  | 'image'
  | 'svg'
  | 'html'
  | 'pdf'
  | 'docx'
  | 'xlsx'
  | 'pptx'
  | 'markdown'
  | 'json'
  | 'csv'
  | 'code';

const HTML_EXT = new Set(['html', 'htm']);

export function previewRenderKind(name: string, mime?: string | null, serverExt?: string | null): PreviewRenderKind | null {
  const img = imageKind(name, mime, serverExt);
  if (img === 'raster') return 'image';
  if (img === 'svg') return 'svg';
  const ext = effectiveExt(name, serverExt);
  const m = (mime || '').split(';')[0].trim().toLowerCase();
  if (HTML_EXT.has(ext) || m === 'text/html') return 'html';
  const doc = docPreviewKind(name, mime, serverExt);
  if (doc) return doc;
  const k = previewKind(name, mime, serverExt);
  if (k === 'markdown') return 'markdown';
  if (k === 'json') return 'json';
  if (k === 'csv') return 'csv';
  if (k === 'code' || k === 'source' || k === 'text') return 'code';
  return null;
}

// The renderable EDITABLE-text kinds the in-editor Source ⇄ Preview toggle offers: Markdown, SVG, and
// HTML all have a meaningful rendered form while staying editable in Monaco. Everything else (code,
// json, csv, plain text) has no distinct render, so the toggle is hidden.
export type EditorPreviewKind = 'markdown' | 'svg' | 'html';
export function editorPreviewKind(name: string): EditorPreviewKind | null {
  if (previewKind(name) === 'markdown') return 'markdown';
  if (imageKind(name) === 'svg') return 'svg';
  if (HTML_EXT.has(extOf(name))) return 'html';
  return null;
}

// What opens in the File Browser's read-only PREVIEW overlay (vs. the editor): a non-editable rich
// file — raster image, PDF, or Office doc — within the content cap. Editable text (incl. svg / html /
// markdown / code / json / csv) opens in the editor instead, which carries its own preview toggle.
export type PreviewOverlayKind = 'image' | 'pdf' | 'docx' | 'xlsx' | 'pptx';
export function previewOverlayKind(entry: { kind: string; name: string; size: number | null }): PreviewOverlayKind | null {
  if (entry.kind !== 'file') return null;
  if (entry.size != null && entry.size > DOC_PREVIEW_MAX_BYTES) return null;
  const rk = previewRenderKind(entry.name);
  return rk === 'image' || rk === 'pdf' || rk === 'docx' || rk === 'xlsx' || rk === 'pptx' ? rk : null;
}

// Shiki language id for the 'code'/'source' kinds; 'text' (no highlight) otherwise.
export function codeLanguage(name: string, serverExt?: string | null): string {
  const b = baseName(name).toLowerCase();
  const ext = effectiveExt(name, serverExt);
  return SOURCE_LANG[ext] || CODE_LANG[ext] || NAME_LANG[b] || 'text';
}

// Human byte size. Shared with FileCard so the card label and the viewer header
// format sizes identically.
export function formatBytes(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}
