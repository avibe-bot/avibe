// Notices for the third-party code the mobile App actually bundles.
//
// A Vite plugin for `vite.mobile.config.ts` only. At generateBundle it reads
// every emitted chunk's module IDs and every asset's original files, maps each
// to the installed package that owns it, and emits `licenses-web.json` with
// each package's own license and notice files, byte for byte.
// `ios/scripts/app_licenses.py` merges it with the native notices. Packages
// that do not reach the bundle never appear; a bundled package without any
// notice file fails the build rather than getting an invented text.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';

export const WEB_NOTICES = 'licenses-web.json';
const NOTICE_FILE = /^(licen[cs]e|copying|copyright|notice|unlicense)([._-]|$)/i;
const CODE_FILE = /\.(c|h|js|mjs|cjs|ts|tsx|py|rs)$/i;

/** Virtual helper modules and the package whose code they are. Unknown ones fail. */
const VIRTUAL_OWNERS = [
  [/^\0?vite\//, 'vite'],
  [/^\0?(?:rolldown\/runtime|rolldown:runtime)/, 'rolldown'],
  [/^\0?(?:plugin-vue|react-refresh|@vitejs\/plugin-react)/, '@vitejs/plugin-react'],
];

const stripQuery = (id) => id.replace(/[?#].*$/, '');

/** The nearest package.json above a file inside node_modules, with a name and version. */
export function owningPackage(file) {
  let directory = dirname(file);
  while (directory.includes(`${sep}node_modules${sep}`) || directory.endsWith(`${sep}node_modules`)) {
    const manifest = join(directory, 'package.json');
    if (existsSync(manifest)) {
      const data = JSON.parse(readFileSync(manifest, 'utf8'));
      if (typeof data.name === 'string' && typeof data.version === 'string') {
        return { root: directory, name: data.name, version: data.version, license: data.license ?? null };
      }
    }
    const parent = dirname(directory);
    if (parent === directory) break;
    directory = parent;
  }
  return null;
}

/** Where one bundled module or asset comes from. Throws for anything unaccounted for. */
export function classify(id, { repoRoot, resolveVirtual = owningVirtual }) {
  if (id.startsWith('\0') || !isAbsolute(stripQuery(id))) {
    const owner = resolveVirtual(id);
    if (!owner) throw new Error(`bundled virtual module has no known owner: ${JSON.stringify(id)}`);
    return { kind: 'virtual', owner };
  }
  const file = stripQuery(id);
  if (file.split(sep).includes('node_modules')) {
    const owner = owningPackage(file);
    if (!owner) throw new Error(`bundled module has no owning package.json: ${file}`);
    return { kind: 'package', owner };
  }
  if (!relative(repoRoot, file).startsWith('..')) return { kind: 'local' };
  throw new Error(`bundled module is outside the repository and node_modules: ${file}`);
}

export function owningVirtual(id) {
  const name = id.replace(/^\0/, '');
  const match = VIRTUAL_OWNERS.find(([pattern]) => pattern.test(name));
  return match ? match[1] : null;
}

/** A package's own license and notice files at its root, as shipped. */
export function noticeFiles(root) {
  return readdirSync(root)
    .filter((name) => NOTICE_FILE.test(name) && !CODE_FILE.test(name) && statSync(join(root, name)).isFile())
    .sort()
    .map((name) => {
      const text = readFileSync(join(root, name), 'utf8');
      if (!text.trim()) throw new Error(`empty notice file: ${join(root, name)}`);
      return { name, text };
    });
}

/** The notices document for a set of bundled module and asset IDs. */
export function collectNotices(ids, { repoRoot, resolvePackage }) {
  const packages = new Map();
  const local = new Set();
  const virtual = new Map();
  for (const id of [...new Set(ids)].sort()) {
    const where = classify(id, { repoRoot });
    if (where.kind === 'local') {
      local.add(relative(repoRoot, stripQuery(id)).split(sep).join('/'));
      continue;
    }
    const owner = where.kind === 'virtual' ? resolvePackage(where.owner) : where.owner;
    if (where.kind === 'virtual') virtual.set(id.replace(/^\0/, ''), `${owner.name}@${owner.version}`);
    const key = `${owner.name}@${owner.version}`;
    const entry = packages.get(key) ?? { ...owner, modules: 0 };
    entry.modules += 1;
    packages.set(key, entry);
  }
  const missing = [];
  const result = [...packages.values()]
    .sort((a, b) => (a.name === b.name ? a.version.localeCompare(b.version) : a.name.localeCompare(b.name)))
    .map(({ root, name, version, license, modules }) => {
      const files = noticeFiles(root);
      if (!files.length) missing.push(`${name}@${version} (${root})`);
      return { name, version, license, modules, files };
    });
  if (missing.length) {
    throw new Error(
      'bundled packages ship no license or notice file; the App cannot carry their notices. '
      + 'Replace or remove the dependency, or bring the reviewed notice upstream: '
      + missing.join(', '),
    );
  }
  return {
    schema_version: 1,
    packages: result,
    local_modules: local.size,
    virtual_modules: Object.fromEntries([...virtual].sort()),
  };
}

/** Resolve a tool package (for virtual helpers) from the UI's installed graph. */
const installedPackage = (uiRoot) => (name) => {
  const root = join(uiRoot, 'node_modules', ...name.split('/'));
  const data = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  return { root, name: data.name, version: data.version, license: data.license ?? null };
};

/**
 * The plugin, and a PostCSS plugin that records the files each stylesheet
 * inlined (`@import "tailwindcss"` and the like): those reach the bundle
 * without being modules of its graph. Run it after every other PostCSS plugin.
 */
export function bundleNotices({ repoRoot, uiRoot }) {
  const inlined = new Map();
  const postcss = {
    postcssPlugin: 'avibe-bundle-notices',
    OnceExit(_root, { result }) {
      const from = result.opts.from;
      if (!from) return;
      const files = inlined.get(from) ?? new Set();
      for (const message of result.messages) {
        // Inlined third-party files only; repository files Tailwind scans for class names add no code.
        if (message.type === 'dependency' && typeof message.file === 'string'
          && message.file.split(sep).includes('node_modules')) files.add(message.file);
      }
      inlined.set(from, files);
    },
  };
  return {
    postcss,
    vite: {
      name: 'avibe-bundle-notices',
      apply: 'build',
      generateBundle(_options, bundle) {
        const ids = [];
        for (const output of Object.values(bundle)) {
          if (output.type === 'chunk') ids.push(...output.moduleIds);
          else ids.push(...(output.originalFileNames ?? []).map((name) => (isAbsolute(name) ? name : join(uiRoot, name))));
        }
        for (const id of [...ids]) {
          for (const file of inlined.get(stripQuery(id)) ?? []) ids.push(file);
        }
        const notices = collectNotices(ids, { repoRoot, resolvePackage: installedPackage(uiRoot) });
        this.emitFile({ type: 'asset', fileName: WEB_NOTICES, source: `${JSON.stringify(notices, null, 1)}\n` });
      },
    },
  };
}
