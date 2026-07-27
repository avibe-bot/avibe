import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

// The Docker ``ui-builder`` stage runs ``npm run build`` with only ``ui/`` copied into the
// image, so any import that escapes ``ui/`` — the shared message-type catalog at the
// repository root, for one — fails to resolve there unless the Dockerfile also places that
// file at the path the import expects. No CI job builds that image, so nothing else in the
// pipeline catches the breakage: this test is the guard.

const UI_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const DOCKERFILE = new URL('../../../Dockerfile', import.meta.url);

// Stage 1 builds from ``/app/ui``, so the repository root maps to ``/app`` in that image.
const STAGE_WORKDIR = '/app/ui';
const IMAGE_REPO_ROOT = '/app';

const RELATIVE_SPECIFIER = /(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]/g;

const withinUi = (target: string): boolean => target.startsWith(UI_ROOT);

const sourceFiles = async (dir: string): Promise<string[]> => {
  const entries = await readdir(dir, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sourceFiles(full);
      return /\.tsx?$/.test(entry.name) ? [full] : [];
    }),
  );
  return nested.flat();
};

/** Repository-relative paths that ``ui/src`` imports from outside ``ui/``. */
const escapingImports = async (): Promise<string[]> => {
  const found = new Set<string>();
  for (const file of await sourceFiles(path.join(UI_ROOT, 'src'))) {
    const source = await readFile(file, 'utf8');
    for (const [, specifier] of source.matchAll(RELATIVE_SPECIFIER)) {
      const resolved = path.resolve(path.dirname(file), specifier);
      if (!withinUi(resolved)) found.add(path.relative(REPO_ROOT, resolved));
    }
  }
  return [...found].sort();
};

type ContextCopy = { source: string; destination: string };

/** ``COPY`` instructions in the ``ui-builder`` stage that read from the build context. */
const uiBuilderCopies = async (): Promise<ContextCopy[]> => {
  const dockerfile = await readFile(DOCKERFILE, 'utf8');
  const stage = dockerfile
    .split(/^FROM /m)
    .find((block) => /^\S+\s+AS\s+ui-builder\b/i.test(block));
  if (!stage) throw new Error('Dockerfile no longer declares a ui-builder stage');
  return [...stage.matchAll(/^COPY\s+(?!--from)(.+)$/gm)].flatMap(([, args]) => {
    const parts = args.trim().split(/\s+/);
    const destination = parts.pop();
    if (!destination) return [];
    return parts.map((source) => ({ source: source.replace(/\/$/, ''), destination }));
  });
};

/** Where *target* ends up inside the image, or undefined when no ``COPY`` provides it. */
const imagePathOf = (target: string, copies: ContextCopy[]): string | undefined => {
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
};

describe('imports that escape ui/', () => {
  it('are provided to the Docker UI builder at the path the import resolves to', async () => {
    const escaping = await escapingImports();
    // Guard against a vacuous pass if the scan above ever stops finding anything.
    expect(escaping.length).toBeGreaterThan(0);

    const copies = await uiBuilderCopies();
    for (const target of escaping) {
      expect(imagePathOf(target, copies), `${target} is missing from the ui-builder stage`).toBe(
        path.posix.join(IMAGE_REPO_ROOT, target),
      );
    }
  });
});
