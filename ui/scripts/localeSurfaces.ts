// The Web UI's copy, split by surface (docs/plans/web-ui-first-load.md).
//
// `src/i18n/en.json` and `zh.json` stay the one place copy is written. When the
// Web UI is built, each language becomes two chunks:
//
// - `first`: what the first screen (the home and a conversation) can draw, and
//   everything not listed below. index.html preloads it, and the first render
//   waits for it (src/i18n/app.ts).
// - `settings`: the part of the catalog's `settings` copy only the Settings
//   pages (with the Model Hub, SETTINGS_PAGES) draw. Their loaders fetch it with
//   the page (src/i18n/settingsCopy.ts), so none of them draws before it is here.
//   Copy of other routed pages is still the first screen's; it can move the same
//   way (SURFACE_ROOTS).
//
// `src/i18n/surfaces.json` lists the subtrees that go to `settings`. A key not
// listed stays in `first`, so new copy is never missing; it rides the first
// screen until the list is refreshed with `npm run i18n:surfaces`, which derives
// it from the built module graph (`vite build --mode i18n-surfaces`).
//
// Each build checks the list against that graph: no module the Web UI can run
// without loading a Settings page may name a listed key, or a key prefix that
// reaches one, or it would draw a raw key. Such a build fails and names the key
// and module; refresh the list (or draw the copy where it belongs). Copy whose
// keys no module names (keys the code assembles at run time) is never listed.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';

type Copy = { [key: string]: string | string[] | Copy };
export type CopySurface = 'first' | 'settings';
export type SurfaceList = { settings: string[] };

const UI = join(dirname(fileURLToPath(import.meta.url)), '..');
const I18N = join(UI, 'src', 'i18n');
const LIST = join(I18N, 'surfaces.json');
const MODULE = /^virtual:avibe-copy\/(en|zh)\/(first|settings)$/;
const SRC = join(UI, 'src') + '/';
const MAIN = 'src/main.tsx';
/**
 * The modules that draw the Settings copy, and the only modules that may load
 * them: each loads one together with that copy (`loadSettingsCopy`), so nothing
 * from it draws before the copy is here.
 */
const SETTINGS_PAGES = [
  'src/components/settings/settingsRoutePages.ts',
  'src/components/settings/models/SettingsModelsPage.tsx',
];
const SETTINGS_PAGE_LOADERS = ['src/routePages.ts', 'src/App.tsx'];

type ModuleGraph = { getModuleInfo: (id: string) => { importedIds: readonly string[]; dynamicallyImportedIds: readonly string[] } | null };

/**
 * Every module the Web UI can evaluate without loading the Settings copy: from
 * its main module, every import, except a Settings page loaded by its loader.
 */
function firstScreenModules(graph: ModuleGraph, main: string): Set<string> {
  const pages = new Set(SETTINGS_PAGES.map((path) => join(UI, path)));
  const loaders = new Set(SETTINGS_PAGE_LOADERS.map((path) => join(UI, path)));
  const seen = new Set<string>();
  const pending = [main];
  while (pending.length) {
    const id = pending.pop()!;
    if (seen.has(id)) continue;
    seen.add(id);
    const info = graph.getModuleInfo(id);
    if (!info) continue;
    pending.push(...info.importedIds);
    pending.push(...info.dynamicallyImportedIds.filter((target) => !(loaders.has(id) && pages.has(target))));
  }
  return seen;
}
/**
 * The catalog's top-level keys that may move to the Settings copy. Only
 * `settings` today; the other pages' copy is still the first screen's. Never
 * `models`: the server chooses those keys (src/components/settings/models/
 * serverCopy.ts), and no module names them.
 */
const SURFACE_ROOTS = ['settings'];

const isObject = (value: unknown): value is Copy => !!value && typeof value === 'object' && !Array.isArray(value);
const PLURAL = /_(?:zero|one|two|few|many|other)$|_ordinal_(?:zero|one|two|few|many|other)$/;

/** Whether a key, or key prefix, the code names can reach copy at `path`. */
function reaches(name: string, path: string): boolean {
  return name === path
    || path.startsWith(`${name}.`)
    || name.startsWith(`${path}.`)
    || path.replace(PLURAL, '') === name;
}

/**
 * The one copy split into the two surfaces. Every string lands in exactly one,
 * at the same path, so `first` and `settings` together are the whole catalog.
 */
export function splitCopy(copy: Copy, list: SurfaceList): Record<CopySurface, Copy> {
  const first: Copy = structuredClone(copy);
  const settings: Copy = {};
  for (const path of list.settings) {
    const keys = path.split('.');
    let from: Copy = first;
    let to: Copy = settings;
    for (const key of keys.slice(0, -1)) {
      const next = from[key];
      if (!isObject(next)) throw new Error(`i18n surfaces: ${path} is not in the catalog`);
      from = next;
      to = (to[key] ??= {}) as Copy;
    }
    const leaf = keys[keys.length - 1];
    if (!(leaf in from)) throw new Error(`i18n surfaces: ${path} is not in the catalog`);
    to[leaf] = from[leaf];
    delete from[leaf];
  }
  return { first, settings };
}

const readCopy = (language: string): Copy => JSON.parse(readFileSync(join(I18N, `${language}.json`), 'utf8')) as Copy;
const readList = (): SurfaceList => JSON.parse(readFileSync(LIST, 'utf8')) as SurfaceList;

/** Key literals and key prefixes (a literal ending in `.` before `${` or `+`) in built code. */
function namesIn(code: string): Set<string> {
  const names = new Set<string>();
  for (const match of code.matchAll(/["'`]([A-Za-z]\w*(?:\.[\w-]+)*)(\.?)(?:\$\{|["'`])/g)) {
    // A bare word ("settings", a route or an id) names no copy; a prefix such as
    // `devices.${x}` names the whole subtree.
    if (match[1].includes('.') || match[2] === '.') names.add(match[1]);
  }
  return names;
}

/** The subtrees only gated chunks name: the `settings` list a build supports. */
export function deriveList(copy: Copy, firstNames: Set<string>, allNames: Set<string>): SurfaceList {
  const settings: string[] = [];
  const any = (names: Set<string>, path: string) => [...names].some((name) => reaches(name, path));
  const walk = (node: Copy, prefix: string) => {
    for (const [key, value] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (!prefix && !SURFACE_ROOTS.includes(key)) continue;
      if (!any(firstNames, path)) {
        // Copy no chunk names may be reached by a key the code assembles at
        // run time; it stays with the first screen.
        if (any(allNames, path)) settings.push(path);
        continue;
      }
      // Split a subtree only where its keys are plain names: a flat dotted key
      // ("vendor.hint") would make a dotted path ambiguous, and a subtree the
      // code takes whole (`returnObjects`) must stay in one surface.
      if (!isObject(value) || allNames.has(path) || Object.keys(value).some((child) => child.includes('.'))) continue;
      walk(value, path);
    }
  };
  walk(copy, '');
  return { settings };
}

/** `virtual:avibe-copy/<language>/<surface>`: one language's copy for one surface. */
export function localeSurfaces(): Plugin {
  let building = false;
  let writing = false;
  // Key literals and prefixes per source module, read as each is built.
  const names = new Map<string, Set<string>>();
  return {
    name: 'avibe-locale-surfaces',
    resolveId: (id) => (MODULE.test(id) ? `\0${id}` : undefined),
    load(id) {
      const match = id.startsWith('\0') ? id.slice(1).match(MODULE) : null;
      if (!match) return undefined;
      this.addWatchFile(join(I18N, `${match[1]}.json`));
      this.addWatchFile(LIST);
      const part = splitCopy(readCopy(match[1]), readList())[match[2] as CopySurface];
      // JSON.parse of one string literal parses faster than the same object literal.
      return `export default JSON.parse(${JSON.stringify(JSON.stringify(part))});`;
    },
    transform: {
      order: 'post',
      handler(code, id) {
        if (building && id.startsWith(SRC) && /\.[cm]?[jt]sx?$/.test(id)) names.set(id, namesIn(code));
        return undefined;
      },
    },
    configResolved(config) {
      building = config.command === 'build';
      writing = config.mode === 'i18n-surfaces';
    },
    generateBundle() {
      const main = join(UI, MAIN);
      // Only the Web UI's page loads copy by surface.
      if (!this.getModuleInfo(main)) return;
      const ungated = firstScreenModules(this, main);
      const firstNames = new Set([...ungated].flatMap((id) => [...(names.get(id) ?? [])]));
      if (writing) {
        const allNames = new Set([...names.values()].flatMap((set) => [...set]));
        const list = deriveList(readCopy('en'), firstNames, allNames);
        if (JSON.stringify(list) !== JSON.stringify(deriveList(readCopy('zh'), firstNames, allNames))) {
          this.error('i18n surfaces: en.json and zh.json have different keys');
        }
        writeFileSync(LIST, `${JSON.stringify(list, null, 2)}\n`);
        console.log(`i18n surfaces: wrote ${list.settings.length} Settings subtrees to src/i18n/surfaces.json`);
        return;
      }
      const listed = readList().settings;
      const conflicts: string[] = [];
      for (const id of ungated) {
        for (const name of names.get(id) ?? []) {
          const path = listed.find((candidate) => reaches(name, candidate));
          if (path) conflicts.push(`${name} (${id.slice(UI.length + 1)}) is Settings copy (${path})`);
        }
      }
      if (conflicts.length) {
        this.error(
          `i18n surfaces: the first screen can draw copy that loads only with Settings:\n  ${conflicts.slice(0, 20).join('\n  ')}`
          + '\nRun `npm run i18n:surfaces` to move it back to the first screen\'s copy (scripts/localeSurfaces.ts).',
        );
      }
    },
  };
}
