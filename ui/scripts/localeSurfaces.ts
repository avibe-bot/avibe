// The Web UI's copy, split by surface (docs/plans/web-ui-first-load.md).
//
// `src/i18n/en.json` and `zh.json` stay the one place copy is written. When the
// Web UI is built, each language becomes one chunk per part:
//
// - `first`: what the first screen (the home and a conversation) can draw, and
//   everything not listed below. index.html preloads it, and the first render
//   waits for it (src/i18n/app.ts).
// - one per surface (SURFACES: Settings with the Model Hub, Agents, Skills,
//   Harness, the setup wizard): the copy of the routed pages' keys
//   (SURFACE_ROOTS) that only that surface's pages draw. Each page's loader
//   fetches it with the page (`withCopy`, src/i18n/surfaceCopy.ts), so no page
//   draws before its copy is here. Copy more than one surface draws stays in
//   `first`.
//
// `src/i18n/surfaces.json` lists the subtrees each surface holds. A key not
// listed stays in `first`, so new copy is never missing; it rides the first
// screen until the list is refreshed with `npm run i18n:surfaces`, which derives
// it from the built module graph (`vite build --mode i18n-surfaces`).
//
// Each build checks both halves. Every loader loads each surface page with its
// own surface's copy. And no module that can run without a surface's copy (the
// first screen's, or another surface's) names one of its keys, or a key prefix
// that reaches one, or it would draw a raw key. Either failure stops the build
// and names the module; refresh the list (or draw the copy where it belongs).
// Copy whose keys no module names (keys the code assembles at run time) is
// never listed.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';

type Copy = { [key: string]: string | string[] | Copy };
/** Which subtrees of the catalog each surface holds (`src/i18n/surfaces.json`). */
export type SurfaceList = Record<string, string[]>;

const UI = join(dirname(fileURLToPath(import.meta.url)), '..');
const I18N = join(UI, 'src', 'i18n');
const LIST = join(I18N, 'surfaces.json');
const PART = /^virtual:avibe-copy\/(en|zh)\/(\w+)$/;
const LOADERS_MODULE = 'virtual:avibe-copy/loaders';
const SRC = join(UI, 'src') + '/';
const MAIN = 'src/main.tsx';
const FIRST = 'first';

/**
 * Each surface, by the lazy pages that draw it. A page loads together with its
 * surface's copy: `withCopy('<surface>', () => import('<page>'))` in one of
 * PAGE_LOADERS (src/i18n/surfaceCopy.ts), which each build checks, so nothing
 * from a page draws before its copy is here.
 */
export const SURFACES: Record<string, string[]> = {
  settings: [
    'src/components/settings/settingsRoutePages.ts',
    'src/components/settings/models/SettingsModelsPage.tsx',
  ],
  agents: ['src/components/workbench/AgentsPage.tsx'],
  skills: ['src/components/workbench/SkillsPage.tsx'],
  harness: ['src/components/workbench/HarnessPage.tsx'],
  setup: ['src/components/Wizard.tsx', 'src/components/onboarding/SetupRemoteAccess.tsx'],
};
const PAGE_LOADERS = ['src/routePages.ts', 'src/App.tsx'];
/**
 * The catalog's top-level keys whose copy may move off the first screen: the
 * routed pages'. Never `models`: the server chooses those keys
 * (src/components/settings/models/serverCopy.ts), and no module names them.
 */
const SURFACE_ROOTS = [
  'settings', 'onboarding', 'agents', 'harness', 'mcpHub', 'bots', 'skills', 'channelList',
  'larkConfig', 'telegramConfig', 'discordConfig', 'slackConfig', 'wechatConfig', 'permissions',
  'userList', 'logs', 'bindCode', 'globalPrompts', 'projectSettings', 'agentsMd', 'agentDetection',
  'remoteAccess',
];

type ModuleGraph = { getModuleInfo: (id: string) => { importedIds: readonly string[]; dynamicallyImportedIds: readonly string[] } | null };

const pageSurface = new Map(Object.entries(SURFACES).flatMap(([surface, pages]) => pages.map((page) => [join(UI, page), surface] as const)));

/**
 * Which copy each module can run with: `first` for every module the Web UI can
 * evaluate before any page loads its copy (from its main module, every import
 * except a page its loader loads), and each surface for the modules its pages
 * import. A module that runs with only one surface draws only that copy.
 */
function moduleOwners(graph: ModuleGraph, main: string): Map<string, Set<string>> {
  const loaders = new Set(PAGE_LOADERS.map((path) => join(UI, path)));
  const owners = new Map<string, Set<string>>();
  const walk = (roots: string[], owner: string) => {
    const seen = new Set<string>();
    const pending = [...roots];
    while (pending.length) {
      const id = pending.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      let set = owners.get(id);
      if (!set) owners.set(id, (set = new Set()));
      set.add(owner);
      const info = graph.getModuleInfo(id);
      if (!info) continue;
      pending.push(...info.importedIds);
      pending.push(...info.dynamicallyImportedIds.filter((target) => !(loaders.has(id) && pageSurface.has(target))));
    }
  };
  walk([main], FIRST);
  for (const [surface, pages] of Object.entries(SURFACES)) walk(pages.map((page) => join(UI, page)), surface);
  return owners;
}

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
 * The one copy split into its surfaces. Every string lands in exactly one, at the
 * same path, so `first` and the listed surfaces together are the whole catalog.
 * A listed path one language lacks (a plural form only the other one has) moves
 * nothing in that language.
 */
export function splitCopy(copy: Copy, list: SurfaceList): Record<string, Copy> {
  const parts: Record<string, Copy> = { [FIRST]: structuredClone(copy) };
  for (const [surface, paths] of Object.entries(list)) {
    const part: Copy = (parts[surface] = {});
    for (const path of paths) {
      const keys = path.split('.');
      let from: Copy = parts[FIRST];
      let to: Copy = part;
      const parents = keys.slice(0, -1);
      const leaf = keys[keys.length - 1];
      let found = true;
      for (const key of parents) {
        const next = from[key];
        if (!isObject(next)) {
          found = false;
          break;
        }
        from = next;
      }
      if (!found || !(leaf in from)) continue;
      for (const key of parents) to = (to[key] ??= {}) as Copy;
      to[leaf] = from[leaf];
      delete from[leaf];
    }
  }
  return parts;
}

/** Both catalogs' keys in one tree, for deciding where each path goes. */
function mergeCopy(a: Copy, b: Copy): Copy {
  const merged: Copy = { ...a };
  for (const [key, value] of Object.entries(b)) {
    const mine = merged[key];
    merged[key] = isObject(mine) && isObject(value) ? mergeCopy(mine, value) : (mine ?? value);
  }
  return merged;
}

const has = (copy: Copy, path: string): boolean => {
  let node: unknown = copy;
  for (const key of path.split('.')) {
    if (!isObject(node) || !(key in node)) return false;
    node = node[key];
  }
  return true;
};

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

/**
 * The subtrees each surface supports: copy whose every name comes only from
 * modules that run with that one surface. Copy the first screen can reach, or
 * that more than one surface draws, or that no module names (a key assembled at
 * run time) stays with the first screen.
 */
export function deriveList(copy: Copy, nameOwners: Map<string, Set<string>>): SurfaceList {
  const list: SurfaceList = Object.fromEntries(Object.keys(SURFACES).map((surface) => [surface, []]));
  const ownersOf = (path: string) => {
    const owners = new Set<string>();
    for (const [name, set] of nameOwners) if (reaches(name, path)) set.forEach((owner) => owners.add(owner));
    return owners;
  };
  const walk = (node: Copy, prefix: string) => {
    for (const [key, value] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (!prefix && !SURFACE_ROOTS.includes(key)) continue;
      const owners = ownersOf(path);
      if (owners.size === 1 && !owners.has(FIRST)) {
        list[[...owners][0]].push(path);
        continue;
      }
      // Split a subtree only where its keys are plain names: a flat dotted key
      // ("vendor.hint") would make a dotted path ambiguous, and a subtree the
      // code takes whole (`returnObjects`) must stay in one surface.
      if (owners.size === 0 || !isObject(value) || nameOwners.has(path) || Object.keys(value).some((child) => child.includes('.'))) continue;
      walk(value, path);
    }
  };
  walk(copy, '');
  return list;
}

/** The parts a page loader may load: every surface with copy of its own. */
const loadersModule = (list: SurfaceList) => {
  const surfaces = Object.keys(list).filter((surface) => list[surface].length);
  const parts = [FIRST, ...surfaces];
  // Keyed by i18next namespace: the first screen's copy is `translation`.
  const language = (code: string) => `${code}: {${parts.map((part) => `${part === FIRST ? 'translation' : part}: () => import('virtual:avibe-copy/${code}/${part}')`).join(', ')}}`;
  return `export const SURFACES = ${JSON.stringify(surfaces)};\nexport default {${language('en')}, ${language('zh')}};`;
};

/** `withCopy('<surface>', () => import('<page>'))`: how a loader loads a page. */
const WITH_COPY = /withCopy\(\s*'(\w+)',\s*\(\)\s*=>\s*import\(\s*'([^']+)'\s*\)\s*\)/g;
const IMPORT = /\bimport\(\s*'([^']+)'\s*\)/g;

/**
 * `virtual:avibe-copy/<language>/<part>`: one language's copy for the first
 * screen or one surface, and `virtual:avibe-copy/loaders`, a loader for each.
 */
export function localeSurfaces(): Plugin[] {
  return [copyParts(), pageLoaders()];
}

/** A loader loads each page with its own surface's copy, and in no other way. */
function pageLoaders(): Plugin {
  const loaders = new Set(PAGE_LOADERS.map((path) => join(UI, path)));
  return {
    name: 'avibe-locale-surfaces-loaders',
    transform: {
      order: 'pre',
      async handler(code, id) {
        if (!loaders.has(id)) return undefined;
        const wrapped = new Map<string, string>();
        for (const [, surface, specifier] of code.matchAll(WITH_COPY)) wrapped.set(specifier, surface);
        for (const [, specifier] of code.matchAll(IMPORT)) {
          const target = (await this.resolve(specifier, id))?.id;
          const surface = target && pageSurface.get(target);
          if (surface && wrapped.get(specifier) !== surface) {
            this.error(`i18n surfaces: ${id.slice(UI.length + 1)} loads ${specifier} without its copy; write withCopy('${surface}', () => import('${specifier}'))`);
          }
        }
        return undefined;
      },
    },
  };
}

function copyParts(): Plugin {
  let building = false;
  let writing = false;
  // Key literals and prefixes per source module, read as each is built.
  const names = new Map<string, Set<string>>();
  return {
    name: 'avibe-locale-surfaces',
    resolveId: (id) => (PART.test(id) || id === LOADERS_MODULE ? `\0${id}` : undefined),
    load(id) {
      if (!id.startsWith('\0')) return undefined;
      const virtual = id.slice(1);
      if (virtual === LOADERS_MODULE) {
        this.addWatchFile(LIST);
        return loadersModule(readList());
      }
      const match = virtual.match(PART);
      if (!match) return undefined;
      this.addWatchFile(join(I18N, `${match[1]}.json`));
      this.addWatchFile(LIST);
      const part = splitCopy(readCopy(match[1]), readList())[match[2]];
      if (!part) this.error(`i18n surfaces: no copy surface named ${match[2]}`);
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
      const owners = moduleOwners(this, main);
      const nameOwners = new Map<string, Set<string>>();
      for (const [id, set] of names) {
        const moduleOwner = owners.get(id);
        if (!moduleOwner) continue;
        for (const name of set) {
          let owner = nameOwners.get(name);
          if (!owner) nameOwners.set(name, (owner = new Set()));
          moduleOwner.forEach((o) => owner.add(o));
        }
      }
      if (writing) {
        const list = deriveList(mergeCopy(readCopy('en'), readCopy('zh')), nameOwners);
        writeFileSync(LIST, `${JSON.stringify(list, null, 2)}\n`);
        console.log(`i18n surfaces: wrote ${Object.entries(list).map(([surface, paths]) => `${surface} ${paths.length}`).join(', ')} subtrees to src/i18n/surfaces.json`);
        return;
      }
      const listed = Object.entries(readList()).flatMap(([surface, paths]) => paths.map((path) => [surface, path] as const));
      const catalog = mergeCopy(readCopy('en'), readCopy('zh'));
      const conflicts: string[] = listed.filter(([, path]) => !has(catalog, path)).map(([surface, path]) => `${path} (${surface}) is in no catalog`);
      for (const [id, set] of names) {
        const moduleOwner = owners.get(id);
        if (!moduleOwner) continue;
        for (const name of set) {
          const hit = listed.find(([surface, path]) => reaches(name, path) && !(moduleOwner.size === 1 && moduleOwner.has(surface)));
          if (hit) conflicts.push(`${name} (${id.slice(UI.length + 1)}, runs with ${[...moduleOwner].join(', ')}) is ${hit[0]} copy (${hit[1]})`);
        }
      }
      if (conflicts.length) {
        this.error(
          `i18n surfaces: a module can draw copy that loads only with another page:\n  ${conflicts.slice(0, 20).join('\n  ')}`
          + '\nRun `npm run i18n:surfaces` to move it back to the first screen\'s copy (scripts/localeSurfaces.ts).',
        );
      }
    },
  };
}
