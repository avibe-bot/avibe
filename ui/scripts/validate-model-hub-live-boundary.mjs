import { readdir, readFile, stat } from 'node:fs/promises';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import * as ts from 'typescript';

const uiRoot = fileURLToPath(new URL('../', import.meta.url));
export const sourceRoot = join(uiRoot, 'src');
export const liveEntry = join(
  sourceRoot,
  'components/settings/models/modelsApi.ts',
);
export const mockOnlyRoot = join(
  sourceRoot,
  'components/settings/models/mock-only',
);
const distRoot = join(uiRoot, 'dist');
const corpusMarker = 'model-hub-mock-corpus-v1';
const textExtensions = new Set(['.html', '.js', '.json', '.map']);
const sourceExtensions = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.json'];

export const filesUnder = async (directory) => {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(path) : [resolve(path)];
  }));
  return nested.flat();
};

const isFile = async (path) => {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
};

const resolveLocalImport = async (specifier, importer, root) => {
  const clean = specifier.replace(/[?#].*$/, '');
  let base;
  if (clean.startsWith('./') || clean.startsWith('../')) {
    base = resolve(dirname(importer), clean);
  } else if (clean.startsWith('@/')) {
    base = resolve(root, clean.slice(2));
  } else {
    return null;
  }

  const candidates = [
    base,
    ...sourceExtensions.map((extension) => `${base}${extension}`),
    ...sourceExtensions.map((extension) => join(base, `index${extension}`)),
  ];
  for (const candidate of candidates) {
    if (await isFile(candidate)) return resolve(candidate);
  }
  throw new Error(`Cannot resolve ${specifier} imported by ${importer}`);
};

export const resolveModuleGraph = async (entry, root = sourceRoot) => {
  const start = resolve(entry);
  const reachable = new Set();
  const parent = new Map();
  const pending = [start];

  while (pending.length > 0) {
    const path = pending.pop();
    if (reachable.has(path)) continue;
    reachable.add(path);
    if (extname(path) === '.json') continue;

    const source = await readFile(path, 'utf8');
    const imports = ts.preProcessFile(source, true, true).importedFiles;
    for (const imported of imports) {
      const resolved = await resolveLocalImport(imported.fileName, path, root);
      if (resolved === null || reachable.has(resolved)) continue;
      if (!parent.has(resolved)) parent.set(resolved, path);
      pending.push(resolved);
    }
  }

  return { reachable, parent };
};

const importChain = (path, parent) => {
  const chain = [path];
  while (parent.has(chain.at(-1))) chain.push(parent.get(chain.at(-1)));
  return chain.reverse();
};

export const findMockOnlyReachability = async ({
  entry = liveEntry,
  root = sourceRoot,
  forbiddenRoot = mockOnlyRoot,
} = {}) => {
  const forbidden = new Set(await filesUnder(forbiddenRoot));
  const { reachable, parent } = await resolveModuleGraph(entry, root);
  return [...forbidden]
    .filter((path) => reachable.has(path))
    .map((path) => ({ path, chain: importChain(path, parent) }));
};

export const assertModelHubModuleBoundary = async (options) => {
  const leaked = await findMockOnlyReachability(options);
  if (leaked.length === 0) return;
  const root = options?.root ?? sourceRoot;
  const paths = leaked.map(({ chain }) =>
    chain.map((path) => relative(root, path)).join(' -> '));
  throw new Error(
    `Model Hub mock-only module reached the live import graph:\n${paths.join('\n')}`,
  );
};

const assertCorpusAbsentFromDist = async () => {
  const leaked = [];
  for (const path of await filesUnder(distRoot)) {
    if (!textExtensions.has(extname(path))) continue;
    if ((await readFile(path, 'utf8')).includes(corpusMarker)) leaked.push(path);
  }
  if (leaked.length > 0) {
    throw new Error(
      `Model Hub mock corpus reached the live build:\n${leaked.join('\n')}`,
    );
  }
};

export const validateModelHubLiveBoundary = async () => {
  await assertModelHubModuleBoundary();
  await assertCorpusAbsentFromDist();
};

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await validateModelHubLiveBoundary();
  console.log('Model Hub live boundary passed: mock-only modules are unreachable.');
}
