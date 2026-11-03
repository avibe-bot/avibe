// The full emoji set a Bot's avatar picker offers (docs/plans/bots.md D5), built
// from Emojibase (CLDR names and keywords) when the UI is built, never fetched at
// run time: Avibe is local-first, and a CDN would see who opens the picker.
//
// `import('virtual:emoji-catalog')` yields one entry per emoji, with its English
// and Chinese names and keywords together, so a search in either language finds
// it whatever the UI language. The raw Emojibase files for both languages are
// about 1.5 MB; the entries keep only what the picker reads (about 200 kB, 90 kB
// gzipped), in a chunk of their own that loads when the full set first opens.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import type { Plugin } from 'vite';

const EMOJI_CATALOG_MODULE = 'virtual:emoji-catalog';
const RESOLVED_ID = `\0${EMOJI_CATALOG_MODULE}`;

/** See `src/lib/emojiCatalog.d.ts`, which the picker reads it through. */
type CatalogEntry = [
  emoji: string,
  group: number,
  version: number,
  flag: 0 | 1,
  en: string[],
  zh: string[],
  skins?: string[],
];

type EmojibaseEmoji = {
  hexcode: string;
  emoji: string;
  label: string;
  tags?: string[];
  group?: number;
  subgroup?: number;
  version: number;
  skins?: { emoji: string; tone?: number | number[] }[];
};

type EmojibaseMessages = {
  groups: { key: string; order: number }[];
  subgroups: { key: string; order: number }[];
};

const require = createRequire(import.meta.url);
const readJson = <T>(path: string): T => JSON.parse(readFileSync(require.resolve(path), 'utf8')) as T;

/** Every emoji Emojibase lists in a group, in its order, except the components
 *  (skin-tone and hair swatches, which are not emoji on their own). */
function buildEmojiCatalog(): CatalogEntry[] {
  const messages = readJson<EmojibaseMessages>('emojibase-data/en/messages.json');
  const component = messages.groups.find((group) => group.key === 'component')?.order;
  // Flags are a separate check: Windows draws them as two letters.
  const flagSubgroups = new Set(
    messages.subgroups.filter((subgroup) => subgroup.key.endsWith('-flag')).map((subgroup) => subgroup.order),
  );
  const zh = new Map(readJson<EmojibaseEmoji[]>('emojibase-data/zh/data.json').map((emoji) => [emoji.hexcode, emoji]));
  const entries: CatalogEntry[] = [];
  for (const emoji of readJson<EmojibaseEmoji[]>('emojibase-data/en/data.json')) {
    if (emoji.group === undefined || emoji.group === component) continue;
    const chinese = zh.get(emoji.hexcode);
    // One tone each, light to dark; mixed-tone pairs are left to the base emoji.
    const skins = (emoji.skins ?? [])
      .filter((skin) => typeof skin.tone === 'number')
      .sort((a, b) => (a.tone as number) - (b.tone as number))
      .map((skin) => skin.emoji);
    const entry: CatalogEntry = [
      emoji.emoji,
      emoji.group,
      emoji.version,
      emoji.subgroup !== undefined && flagSubgroups.has(emoji.subgroup) ? 1 : 0,
      [emoji.label, ...(emoji.tags ?? [])],
      chinese ? [chinese.label, ...(chinese.tags ?? [])] : [],
    ];
    if (skins.length === 5) entry.push(skins);
    entries.push(entry);
  }
  return entries;
}

export function emojiCatalog(): Plugin {
  return {
    name: 'avibe-emoji-catalog',
    resolveId: (id) => (id === EMOJI_CATALOG_MODULE ? RESOLVED_ID : undefined),
    load(id) {
      if (id !== RESOLVED_ID) return undefined;
      // JSON.parse of one string literal parses faster than the same object literal.
      return `export default JSON.parse(${JSON.stringify(JSON.stringify(buildEmojiCatalog()))});`;
    },
  };
}
