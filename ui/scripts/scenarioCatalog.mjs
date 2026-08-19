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
 * Every UI citation the catalog's rows make, whatever key it sits in.
 *
 * Two questions this has been asked wrong twice, and the answer to both is that
 * the row's own claim decides, never a schema position:
 *
 * *Which rows?* The ones that **cite** a UI file — never the ones a legend marks
 * as needing a test. `status_legend` maps a status to a description string in
 * every catalog here but one, so reading `test_required` off it selected that
 * single catalog's rows and passed the other three in silence.
 *
 * *Which citations?* Every one, not `test` alone. A row states UI evidence in
 * two places — `test` names the case that owns the row, and `ui_contract` names
 * the UI half of a row owned by a pytest scenario — and reading only the first
 * left five fully-written citations unasked, which is the legend hole again in
 * another key. It cost four of those five: three named cases #1401 deleted on
 * 2026-08-14, unnoticed because nothing read them.
 *
 * `related_tests` and `canonical_tests` stay unread, and that is the same rule
 * rather than an exception to it: they name a file and no case, so they cannot
 * evidence a row, and they do not claim to — "related" is a pointer, not a
 * coverage claim.
 *
 * `cited` is null for a citation that names a file and no case in it, which
 * `resolveUiEvidence` reports rather than resolves.
 */
export const uiEvidenceRows = (catalog) =>
  (catalog.scenarios ?? []).flatMap((row) => {
    const citations = [];
    if (typeof row.test === 'string' && isUiEvidence(row.test)) {
      const [file, cited = null] = row.test.split('::');
      citations.push({ id: row.id, file, cited });
    }
    const contract = row.ui_contract;
    if (contract && typeof contract.test === 'string' && isUiEvidence(contract.test)) {
      citations.push({
        id: row.id,
        file: contract.test,
        cited: typeof contract.case === 'string' ? contract.case : null,
        // A parameterized case is named at run time, so the catalog writes the
        // template and the substitutions it means separately. Both are terms of
        // one citation; see `citedTerms`.
        inputs: (contract.inputs ?? []).map(String),
      });
    }
    return citations;
  });

/** A collected case's own name, without the `describe` path vitest prefixes to it. */
const caseName = (fullName) => fullName.split(' > ').at(-1);

/**
 * The names a citation may be written against: the case's own, and its full path
 * with vitest's separator flattened to the space the catalogs write instead.
 *
 * Two conventions are in use — a catalog ID the case name carries, and the
 * case's readable full name — and one containment rule covers both, rather than
 * a rule per catalog, which is a policy each new catalog could contradict.
 * Containment and not a prefix because an ID is not always first: one case can
 * carry two scenarios (`[MEMORY-LIST-004][MEMORY-LIST-006] browses …`), and each
 * of them cites it by its own ID. What bounds the looseness is the count below —
 * exactly one collected case may match — so a citation loose enough to reach two
 * cases fails for the same reason as one that reaches none.
 */
const citableNames = (entry) => [caseName(entry.name), entry.name.replaceAll(' > ', ' ')];

/**
 * The terms one citation names, all of which the collected name must contain.
 *
 * A parameterized case has no name until vitest substitutes its row, so the
 * catalog writes the template and its `inputs` in separate keys. Taking the
 * template's literal head plus each input keeps this out of the business of
 * emulating a format string: `%` opens a substitution, everything before the
 * first one is literal, and the inputs are what the substitutions were. The
 * literal tail is dropped rather than parsed, which costs nothing because the
 * count is what decides — for `accepts the declared failure %s with result %s`
 * with `[memory_repair_failed, timed_out]`, one collected case carries all
 * three terms and the other seven carry two.
 */
const citedTerms = (row) => [row.cited.split('%')[0], ...(row.inputs ?? [])];

const sameFile = (collectedFile, file) => {
  const normalized = collectedFile.replaceAll('\\', '/');
  return normalized === file || normalized.endsWith(`/${file}`);
};

/**
 * What is wrong with these citations, given everything vitest collected — nothing
 * if each names exactly one case the suite would execute.
 *
 * Zero is the interesting count: a case that is commented out, skipped, sitting
 * in an unreachable branch, or merely quoted in a string is absent from a
 * collection, so it cannot stand as a row's evidence.
 *
 * A citation must **name a case**, which is the whole rule and the reason there
 * is no file-only shape left. Accepting a bare file made the gate ask whether the
 * file runs, and a file runs for reasons that have nothing to do with the citing
 * row: `MEMORY-LIST-006` and `MEMORY-LIST-007` both named `MemorySearchPanel`
 * and nothing else, so deleting either one's case left the other keeping the
 * file collected and both rows green. That is this gate's own failure — a row
 * reading `covered` with nothing executable tied to *it* — so the file is
 * reported rather than resolved, and the row states which case carries it.
 *
 * `ids` is the whole catalog's ID set, which is what makes a borrowed citation
 * legible: a row naming a *sibling row's* ID resolves to a real running case and
 * still has no evidence of its own.
 */
export const resolveUiEvidence = (rows, collected, ids = new Set(rows.map((row) => row.id))) => {
  const problems = [];
  for (const row of rows) {
    const where = row.catalog ? `${row.catalog} ${row.id}` : row.id;
    if (row.cited === null) {
      problems.push(
        `${where} cites ${row.file} and no case in it; a file is evidence for no row in particular — `
          + 'name the case that carries this one, since any other row citing the same file keeps it collected',
      );
      continue;
    }
    if (row.cited !== row.id && ids.has(row.cited)) {
      problems.push(
        `${where} cites \`${row.cited}\`, which is another row's ID; a row resolves through evidence named for itself`,
      );
      continue;
    }
    const terms = citedTerms(row);
    const matches = collected
      .filter((entry) => sameFile(entry.file, row.file))
      .filter((entry) => citableNames(entry).some((name) => terms.every((term) => name.includes(term))));
    if (matches.length !== 1) {
      problems.push(
        `${where}: vitest collects ${matches.length} cases named \`${terms.join('` + `')}\` in ${row.file}, expected exactly one — `
          + 'a commented-out, skipped, unreachable or merely quoted case is not executable evidence, and a name '
          + 'reaching several cases identifies none of them',
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
