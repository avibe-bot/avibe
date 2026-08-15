import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { extname, join } from 'node:path';

const distRoot = fileURLToPath(new URL('../dist/', import.meta.url));
const corpusMarker = 'model-hub-mock-corpus-v1';
const textExtensions = new Set(['.html', '.js', '.json', '.map']);

const filesUnder = async (directory) => {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(path) : [path];
  }));
  return nested.flat();
};

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

console.log('Model Hub live boundary passed: mock corpus is absent from dist.');
