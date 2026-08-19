/**
 * Resolving a scenario catalog's UI evidence against vitest's own collection.
 *
 * A row marked `covered` claims one executable case carries its ID. For a pytest
 * row `ast.parse` answers that exactly; for a vitest row nothing on the Python
 * side can, and the scan that tried read a live declaration out of five shapes
 * that execute nothing — a regex literal after a keyword, JSX text, a
 * `describe.skip` body, an `if (false)` body, and its own division heuristic.
 * Those are one class with no last member: telling a regex from division needs
 * the parser's state, and telling a skipped or unreachable block from a live one
 * needs the run. So the question goes to the only thing that answers it
 * definitively, and already runs in CI on the same commit: vitest's collection.
 *
 * The gate is `validate-scenario-catalog.mjs`; this module is the part with no
 * subprocess in it, so the resolution rules are testable without one.
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url));

/** vitest's CLI entry, invoked rather than imported so `list` behaves as it does in CI. */
const VITEST_BIN = path.join(UI_ROOT, 'node_modules', 'vitest', 'vitest.mjs');

/** Suffixes the catalog may cite as UI evidence — every one a file vitest collects. */
const UI_SUFFIXES = new Set(['.ts', '.tsx', '.mts', '.mjs']);

/** The file half of a `path::name` catalog reference. */
const citedPath = (testRef) => testRef.split('::')[0];

export const isUiEvidence = (testRef) => UI_SUFFIXES.has(path.extname(citedPath(testRef)));

/**
 * Every catalog row whose declared status requires a test and whose test is a UI case.
 *
 * Which statuses require one is read from the catalog's own legend, so this side
 * cannot drift from the Python checker into a second, disagreeing policy.
 */
export const uiEvidenceRows = (catalog) => {
  const legend = catalog.status_legend ?? {};
  return (catalog.scenarios ?? [])
    .filter((row) => legend[row.status]?.test_required && typeof row.test === 'string' && isUiEvidence(row.test))
    .map((row) => {
      const [file, cited] = row.test.split('::');
      return { id: row.id, file, cited };
    });
};

/** A collected case's own name, without the `describe` path vitest prefixes to it. */
const caseName = (fullName) => fullName.split(' > ').at(-1);

const sameFile = (collectedFile, file) => {
  const normalized = collectedFile.replaceAll('\\', '/');
  return normalized === file || normalized.endsWith(`/${file}`);
};

/**
 * What is wrong with these rows, given everything vitest collected — nothing if
 * each resolves to exactly one collected case named for the row itself.
 *
 * Zero is the interesting count: a case that is commented out, skipped, named at
 * run time by `it.each`, sitting in an unreachable branch, or merely quoted in a
 * string is absent from a collection, so it cannot stand as a row's evidence.
 */
export const resolveUiEvidence = (rows, collected) => {
  const problems = [];
  for (const row of rows) {
    if (row.cited !== row.id) {
      problems.push(
        `${row.id} cites \`${row.cited}\` as its evidence; a UI row resolves through a case named for the row itself`,
      );
      continue;
    }
    const prefix = `${row.id}:`;
    const matches = collected.filter(
      (entry) => sameFile(entry.file, row.file) && caseName(entry.name).startsWith(prefix),
    );
    if (matches.length !== 1) {
      problems.push(
        `${row.id}: vitest collects ${matches.length} cases named \`${prefix} …\` in ${row.file}, expected exactly one — `
          + 'a commented-out, skipped, parameterized, unreachable or merely quoted case is not executable evidence',
      );
    }
  }
  return problems;
};

/**
 * Every case vitest collects for `files`, as `{ name, file }`.
 *
 * `list` collects without running, so this is what the suite would execute rather
 * than what its source looks like. Filters keep it to the cited files: the gate
 * only asks about those, and collecting the whole suite twice per `npm test`
 * would be the slowest possible way to learn the same thing.
 */
export const collectCases = ({ root = UI_ROOT, files = [], globals = false } = {}) => {
  if (!fs.existsSync(VITEST_BIN)) {
    throw new Error(`vitest is not installed at ${VITEST_BIN}; run \`npm ci\` in ui/ before validating the catalog`);
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'vitest-list-'));
  const output = path.join(directory, 'collected.json');
  try {
    execFileSync(
      process.execPath,
      [
        VITEST_BIN,
        'list',
        `--json=${output}`,
        '--root',
        root,
        '--dir',
        root,
        ...(globals ? ['--globals'] : []),
        ...files,
      ],
      { cwd: root, stdio: ['ignore', 'ignore', 'inherit'] },
    );
    return JSON.parse(fs.readFileSync(output, 'utf8'));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
};
