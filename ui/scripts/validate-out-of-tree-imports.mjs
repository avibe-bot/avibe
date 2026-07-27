// The Docker ``ui-builder`` stage runs ``npm run build`` with only ``ui/`` copied into the
// image, so any import that escapes ``ui/`` — the shared message-type catalog at the
// repository root, for one — fails to resolve there unless the Dockerfile also places that
// file at the path the import expects. No CI job builds that image, so nothing else in the
// pipeline catches the breakage: this check is the guard, and it runs as part of
// ``npm run build`` so every existing build path enforces it.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const DOCKERFILE = fileURLToPath(new URL('../../Dockerfile', import.meta.url));

// Stage 1 builds from ``/app/ui``, so the repository root maps to ``/app`` in that image.
const STAGE_WORKDIR = '/app/ui';
const IMAGE_REPO_ROOT = '/app';

const RELATIVE_SPECIFIER = /(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]/g;

const sourceFiles = (dir) =>
  fs
    .readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sourceFiles(full);
      return /\.tsx?$/.test(entry.name) ? [full] : [];
    });

/** Repository-relative paths that ``ui/src`` imports from outside ``ui/``. */
function escapingImports() {
  const found = new Set();
  for (const file of sourceFiles(path.join(UI_ROOT, 'src'))) {
    const source = fs.readFileSync(file, 'utf8');
    for (const [, specifier] of source.matchAll(RELATIVE_SPECIFIER)) {
      const resolved = path.resolve(path.dirname(file), specifier);
      if (!resolved.startsWith(UI_ROOT)) found.add(path.relative(REPO_ROOT, resolved));
    }
  }
  return [...found].sort();
}

/** ``COPY`` instructions in the ``ui-builder`` stage that read from the build context. */
function uiBuilderCopies() {
  const stage = fs
    .readFileSync(DOCKERFILE, 'utf8')
    .split(/^FROM /m)
    .find((block) => /^\S+\s+AS\s+ui-builder\b/i.test(block));
  if (!stage) throw new Error('Dockerfile no longer declares a ui-builder stage');
  return [...stage.matchAll(/^COPY\s+(?!--from)(.+)$/gm)].flatMap(([, args]) => {
    const parts = args.trim().split(/\s+/);
    const destination = parts.pop();
    if (!destination) return [];
    return parts.map((source) => ({ source: source.replace(/\/$/, ''), destination }));
  });
}

/** Where *target* ends up inside the image, or undefined when no ``COPY`` provides it. */
function imagePathOf(target, copies) {
  for (const { source, destination } of copies) {
    const isSameFile = target === source;
    if (!isSameFile && !target.startsWith(`${source}/`)) continue;
    const base = destination.startsWith('/')
      ? destination
      : path.posix.join(STAGE_WORKDIR, destination);
    // Docker copies a directory's *contents* into the destination.
    return isSameFile ? base : path.posix.join(base, path.posix.relative(source, target));
  }
  return undefined;
}

if (!fs.existsSync(DOCKERFILE)) {
  // Expected inside the ``ui-builder`` stage itself, which copies only ``ui/``. A missing
  // catalog there fails the build anyway, at the import that needs it.
  console.log('Out-of-tree import validation skipped: no Dockerfile in this checkout.');
  process.exit(0);
}

const escaping = escapingImports();
// Guard against a vacuous pass if the scan above ever stops finding anything.
if (escaping.length === 0) {
  throw new Error(
    'No imports escaping ui/ were found — the scan is no longer testing anything. ' +
      'Delete this check if the last such import is gone, otherwise fix the scan.',
  );
}

const copies = uiBuilderCopies();
for (const target of escaping) {
  const expected = path.posix.join(IMAGE_REPO_ROOT, target);
  const actual = imagePathOf(target, copies);
  if (actual !== expected) {
    throw new Error(
      `ui/src imports ${target} from outside ui/, but the Dockerfile's ui-builder stage ` +
        `puts it at ${actual ?? 'no path at all'} instead of ${expected}. ` +
        `Add "COPY ${target} ${expected}" to that stage.`,
    );
  }
}

console.log(
  `Out-of-tree import validation passed: ${escaping.join(', ')} reachable in the ui-builder stage.`,
);
