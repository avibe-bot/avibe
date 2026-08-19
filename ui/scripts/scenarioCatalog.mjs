/**
 * Resolving a scenario catalog's UI evidence against vitest's own collection.
 *
 * A row that cites a test claims one executable case carries it. For a pytest
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
 * Every catalog row whose evidence is a UI case, whatever its legend looks like.
 *
 * The row set is the rows that *cite* a UI file, never the rows a legend marks as
 * needing a test. `status_legend` maps a status to a description string in every
 * catalog here but one, so reading `test_required` off it selected the rows of
 * that single catalog and passed the other three in silence — the same failure
 * this gate exists to prevent, one level up. A citation is the row's own claim,
 * exists in every catalog, and no legend shape can switch it off.
 *
 * `cited` is null for a row that names a file and no case in it.
 */
export const uiEvidenceRows = (catalog) =>
  (catalog.scenarios ?? [])
    .filter((row) => typeof row.test === 'string' && isUiEvidence(row.test))
    .map((row) => {
      const [file, cited = null] = row.test.split('::');
      return { id: row.id, file, cited };
    });

/** A collected case's own name, without the `describe` path vitest prefixes to it. */
const caseName = (fullName) => fullName.split(' > ').at(-1);

/**
 * The names a citation may be written against: the case's own, and its full path
 * with vitest's separator flattened to the space the catalogs write instead.
 *
 * Two conventions are in use — a catalog ID that the case name begins with, and
 * the case's readable full name — and both are a prefix of one of these. One
 * matching rule covers them because that is what they have in common, rather
 * than a rule per catalog, which is a policy each new catalog could contradict.
 */
const citableNames = (entry) => [caseName(entry.name), entry.name.replaceAll(' > ', ' ')];

const sameFile = (collectedFile, file) => {
  const normalized = collectedFile.replaceAll('\\', '/');
  return normalized === file || normalized.endsWith(`/${file}`);
};

/**
 * What is wrong with these rows, given everything vitest collected — nothing if
 * every citation resolves to a case the suite would execute.
 *
 * Zero is the interesting count: a case that is commented out, skipped, named at
 * run time by `it.each`, sitting in an unreachable branch, or merely quoted in a
 * string is absent from a collection, so it cannot stand as a row's evidence.
 *
 * `ids` is the whole catalog's ID set, which is what makes a borrowed citation
 * legible: a row naming a *sibling row's* ID resolves to a real running case and
 * still has no evidence of its own.
 */
export const resolveUiEvidence = (rows, collected, ids = new Set(rows.map((row) => row.id))) => {
  const problems = [];
  for (const row of rows) {
    const where = row.catalog ? `${row.catalog} ${row.id}` : row.id;
    const inFile = collected.filter((entry) => sameFile(entry.file, row.file));
    if (row.cited === null) {
      // The row cites a file and names no case inside it, so the only claim there
      // is to check is the one it makes: that vitest executes that file. Which
      // case carries the row is the catalog's to say, and this one does not say.
      if (inFile.length === 0) {
        problems.push(`${where}: vitest collects no case from ${row.file}, so the file it cites is not executable evidence`);
      }
      continue;
    }
    if (row.cited !== row.id && ids.has(row.cited)) {
      problems.push(
        `${where} cites \`${row.cited}\`, which is another row's ID; a row resolves through evidence named for itself`,
      );
      continue;
    }
    const matches = inFile.filter((entry) => citableNames(entry).some((name) => name.startsWith(row.cited)));
    if (matches.length !== 1) {
      problems.push(
        `${where}: vitest collects ${matches.length} cases named \`${row.cited}…\` in ${row.file}, expected exactly one — `
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
