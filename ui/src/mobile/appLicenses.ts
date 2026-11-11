/**
 * The App's Open-source licenses data. `ios/scripts/app_licenses.py` writes
 * `licenses.json` next to `mobile.html` from the notices of the JavaScript this
 * bundle ships (`ui/scripts/bundleNotices.mjs`) and, once the iroh release pin
 * is published, the published native support notices; this page only reads it.
 */

export interface AppLicenseComponent {
  name: string;
  version: string;
  /** Bundled JavaScript, or the native binary's notices. */
  origin: 'web' | 'native';
  /** Declared authors, for canonical texts that name no copyright holder. */
  authors?: string[];
}
/** One distinct license text and every component it covers. */
export interface AppLicenseGroup {
  title: string;
  text: string;
  components: AppLicenseComponent[];
}
/** MPL-2.0 crates that share one version and one upstream source. */
export interface AppSourceOffer {
  version: string;
  upstream: string;
  crates: { name: string; registry: string }[];
}
export interface AppLicenses {
  schema_version: 1;
  native: { state: 'pending' | 'published'; release_tag: string };
  web: { sha256: string; packages: number };
  components: number;
  sources: AppSourceOffer[];
  groups: AppLicenseGroup[];
}

const LICENSES_URL = './licenses.json';

const isLicenses = (value: unknown): value is AppLicenses => {
  if (!value || typeof value !== 'object') return false;
  const data = value as Partial<AppLicenses>;
  return (
    data.schema_version === 1 &&
    Array.isArray(data.sources) &&
    Array.isArray(data.groups) &&
    data.groups.every(
      (group) =>
        typeof group?.title === 'string' &&
        typeof group.text === 'string' &&
        Array.isArray(group.components),
    ) &&
    data.sources.every(
      (source) =>
        typeof source?.upstream === 'string' &&
        typeof source.version === 'string' &&
        Array.isArray(source.crates) &&
        source.crates.length > 0,
    )
  );
};

/** The bundled data, or null when it is missing or unreadable (the build verifies it is present). */
export async function loadAppLicenses(): Promise<AppLicenses | null> {
  try {
    const response = await fetch(LICENSES_URL);
    if (!response.ok) return null;
    const data: unknown = await response.json();
    return isLicenses(data) && data.groups.length > 0 ? data : null;
  } catch {
    return null;
  }
}

export type LicenseBlock =
  | { kind: 'heading'; text: string }
  | { kind: 'paragraph'; text: string }
  /** Kept line by line: lists, indentation, copyright lines, or deliberate short lines. */
  | { kind: 'lines'; text: string };

const UNDERLINE = /^\s*(={3,}|-{3,})\s*$/;
const LIST_ITEM = /^\s*(?:[-*•+]|\(?[0-9a-zA-Z]{1,3}[.)]|[0-9]+(?:\.[0-9]+)+\.?)\s/;
const ATTRIBUTION = /^\s*(?:copyright\b|©|\(c\)\s)/i;

/** Lines that a plain paragraph would lose the layout of. */
function structured(lines: string[]): boolean {
  if (lines.length < 2) return false;
  if (lines.some((line) => LIST_ITEM.test(line) || ATTRIBUTION.test(line) || /^\s/.test(line))) {
    return true;
  }
  if (lines.some((line) => /\S {3,}\S|\t|\|/.test(line))) return true;
  const longest = Math.max(...lines.map((line) => line.length));
  // A hard-wrapped paragraph fills its lines; a short line before the last is a deliberate break.
  return lines.slice(0, -1).some((line) => line.length < longest * 0.5);
}

/**
 * Display blocks for a license text. Hard wraps inside an ordinary paragraph
 * become spaces and a blank line ends a paragraph. A setext underline
 * (`----`, `====`) marks the line above as a heading and is not shown. Anything
 * with list, indentation, table, or attribution structure keeps its lines.
 * Every word of the text is kept; grouping uses the original text, not this.
 */
export function licenseBlocks(text: string): LicenseBlock[] {
  const blocks: LicenseBlock[] = [];
  const flush = (lines: string[]) => {
    if (!lines.length) return;
    if (lines.length === 1 && /^#{1,6}\s+\S/.test(lines[0])) {
      blocks.push({ kind: 'heading', text: lines[0].replace(/^#{1,6}\s+/, '').trim() });
    } else if (structured(lines)) {
      blocks.push({ kind: 'lines', text: lines.join('\n') });
    } else {
      blocks.push({ kind: 'paragraph', text: lines.map((line) => line.trim()).join(' ') });
    }
  };
  for (const block of text.replace(/\r\n?/g, '\n').split(/\n[ \t]*\n/)) {
    const lines = block.split('\n').filter((line) => line.trim() !== '');
    let pending: string[] = [];
    lines.forEach((line, index) => {
      const next = lines[index + 1];
      if (next !== undefined && UNDERLINE.test(next) && !UNDERLINE.test(line) && line.trim()) {
        flush(pending);
        pending = [];
        blocks.push({ kind: 'heading', text: line.trim() });
      } else if (UNDERLINE.test(line) && index > 0 && !UNDERLINE.test(lines[index - 1])) {
        // The underline of the heading pushed above.
      } else {
        pending.push(line);
      }
    });
    flush(pending);
  }
  return blocks;
}
