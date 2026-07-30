export type LocalFileLinkTarget = {
  path: string;
  line?: number;
  column?: number;
  endColumn?: number;
};

function decodeHref(href: string): string {
  try {
    return decodeURIComponent(href);
  } catch {
    return href;
  }
}

function splitSourcePosition(path: string): LocalFileLinkTarget {
  const match = path.match(/:(\d+)(?::(\d+))?$/);
  if (!match) return { path };

  const line = Number.parseInt(match[1], 10);
  const column = match[2] ? Number.parseInt(match[2], 10) : 1;
  if (line < 1 || column < 1) return { path };

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
  const base = workdir.replace(/[\\/]+$/, '');
  const tail = relativePath.replace(/[\\/]+/g, separator);
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
  const decoded = decodeHref(href);
  const absolute = decoded.startsWith('/') && !decoded.startsWith('//') && decoded !== '/';
  const relative = decoded.startsWith('./') && decoded !== './';
  if (!absolute && !relative) return null;

  const target = splitSourcePosition(decoded);
  if (!target.path) return null;
  if (absolute) return target;
  if (!workdir || !isAbsoluteWorkdir(workdir)) return null;

  return { ...target, path: joinWorkdir(workdir, target.path.slice(2)) };
}
