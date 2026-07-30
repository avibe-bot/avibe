export type LocalFileLinkTarget = {
  path: string;
  line?: number;
  column?: number;
  endColumn?: number;
};

// Top-level route namespaces declared in App.tsx. These remain same-origin
// browser links even though their leading slash also looks like a POSIX path.
const APPLICATION_ROUTE_ROOTS = new Set([
  '/setup',
  '/inbox',
  '/search',
  '/agents',
  '/skills',
  '/harness',
  '/vaults',
  '/projects',
  '/more',
  '/apps',
  '/chat',
  '/admin',
  '/dashboard',
  '/groups',
  '/channels',
  '/users',
  '/logs',
  '/settings',
  '/remote-access',
  '/doctor',
]);

function isApplicationRouteHref(href: string): boolean {
  const pathname = href.split(/[?#]/, 1)[0];
  const nextSlash = pathname.indexOf('/', 1);
  const root = nextSlash === -1 ? pathname : pathname.slice(0, nextSlash);
  return APPLICATION_ROUTE_ROOTS.has(root);
}

/** Windows drive and UNC paths are valid local destinations, not URL schemes.
 * Accept encoded separators because Markdown destinations commonly escape
 * backslashes before the URL sanitizer sees them. */
export function isAbsoluteWindowsFileHref(href: string): boolean {
  return /^[A-Za-z]:(?:[\\/]|%(?:2f|5c))/i.test(href) || /^(?:\\|%5c){2}/i.test(href);
}

function decodePath(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

function splitSourcePosition(path: string): LocalFileLinkTarget {
  const match = path.match(/:(\d+)(?::(\d+))?$/);
  if (!match) return { path };

  const line = Number.parseInt(match[1], 10);
  const sourceColumn = match[2] ? Number.parseInt(match[2], 10) : 1;
  if (line < 1 || sourceColumn < 1) return { path };

  // Source links use human-facing 1-based columns; Editor reveal targets use
  // 0-based offsets and convert to Monaco coordinates at the final boundary.
  const column = sourceColumn - 1;

  return {
    path: path.slice(0, match.index),
    line,
    column,
    endColumn: column,
  };
}

function isAbsoluteWorkdir(path: string): boolean {
  return path.startsWith('/') || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith('\\\\');
}

function joinWorkdir(workdir: string, relativePath: string): string {
  const windowsPath = /^[A-Za-z]:[\\/]/.test(workdir) || workdir.startsWith('\\\\');
  const separator = windowsPath ? '\\' : '/';
  const base = windowsPath ? workdir.replace(/[\\/]+$/, '') : workdir.replace(/\/+$/, '');
  const tail = windowsPath ? relativePath.replace(/[\\/]+/g, separator) : relativePath;
  return base ? `${base}${separator}${tail}` : `${separator}${tail}`;
}

/** Resolve a Markdown destination that represents a local file.
 *
 * Absolute POSIX paths open as-is. `./` destinations are relative to the
 * Agent Session's immutable workdir snapshot, not the browser URL. A leading
 * `//` remains a protocol-relative web URL. Common Codex `:line[:column]`
 * suffixes are kept as an editor reveal target rather than part of the path.
 */
export function resolveLocalFileLink(href: string, workdir?: string | null): LocalFileLinkTarget | null {
  const absolutePosix =
    href.startsWith('/') && !href.startsWith('//') && href !== '/' && !isApplicationRouteHref(href);
  const absoluteWindows = isAbsoluteWindowsFileHref(href);
  const relative = href.startsWith('./') && href !== './';
  if (!absolutePosix && !absoluteWindows && !relative) return null;

  // Parse only literal source suffixes. An escaped colon belongs to the
  // filename, so decoding the whole href before this step would be ambiguous.
  const target = splitSourcePosition(href);
  const decodedPath = decodePath(target.path);
  if (!decodedPath) return null;
  if (absolutePosix || absoluteWindows) return { ...target, path: decodedPath };
  if (!workdir || !isAbsoluteWorkdir(workdir)) return null;

  return { ...target, path: joinWorkdir(workdir, decodedPath.slice(2)) };
}
