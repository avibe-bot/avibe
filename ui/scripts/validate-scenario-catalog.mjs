#!/usr/bin/env node
/**
 * Gate: every UI citation a scenario catalog makes names exactly one case vitest
 * collects, in the file the citation points at.
 *
 * The check lives here as `checkCatalogs`, and two callers share it: this file's
 * CLI (`npm run validate:catalog`) and one case in `scenarioCatalog.test.mjs`,
 * which is how it reaches CI. It was briefly chained onto `npm test` as
 * `vitest run && npm run validate:catalog` instead, and that is a defect with a
 * quiet cost: npm appends `--` arguments to the *end* of a composite script, so
 * `npm test -- UsageTab.test.tsx` became
 * `vitest run && npm run validate:catalog UsageTab.test.tsx` — vitest ran all 231
 * files unfiltered and the gate silently ignored a path. Forwarding through a
 * wrapper would keep the hazard and hide it; a test case has no argument to
 * misroute, and the suite runner already owns "everything that must hold".
 *
 * The Python catalog checker owns everything it can answer exactly — the row's
 * shape, its file, and the ID being greppable inside that file — and defers
 * executability here rather than approximating it.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import yaml from 'js-yaml';

import { collectCases, resolveUiEvidence, uiEvidenceRows } from './scenarioCatalog.mjs';

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url));
const REPO_ROOT = path.resolve(UI_ROOT, '..');
const SCENARIO_ROOT = path.join(REPO_ROOT, 'tests', 'scenarios');

/**
 * Every capability catalog in the checkout.
 *
 * Discovered rather than listed: a catalog that starts citing UI evidence is
 * gated by existing, and no list has to be remembered. A checkout without the
 * directory at all is a failure, not an empty pass — this gate cannot report
 * "nothing to check" for the one input it exists to read.
 */
const catalogPaths = () => {
  if (!fs.existsSync(SCENARIO_ROOT)) {
    throw new Error(`${path.relative(REPO_ROOT, SCENARIO_ROOT)} is missing; run this from a full checkout`);
  }
  return fs
    .readdirSync(SCENARIO_ROOT, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(SCENARIO_ROOT, entry.name, 'catalog.yaml'))
    .filter((file) => fs.existsSync(file));
};

/**
 * Every citation in the checkout, and what is wrong with it — `problems` empty
 * when each names one collected case.
 *
 * Returns rather than exits so a test can assert on it: the CLI is one caller of
 * this check, not its owner.
 */
export const checkCatalogs = () => {
  const catalogs = catalogPaths().map((file) => {
    const catalog = yaml.load(fs.readFileSync(file, 'utf8'));
    const name = path.relative(REPO_ROOT, file);
    return {
      // The whole ID set, not just the UI rows': a UI row may borrow the ID of a
      // row evidenced by pytest, and that is the same empty citation.
      ids: new Set((catalog.scenarios ?? []).map((row) => row.id)),
      rows: uiEvidenceRows(catalog).map((row) => ({ ...row, catalog: name })),
    };
  });
  const rows = catalogs.flatMap((catalog) => catalog.rows);
  if (rows.length === 0) return { rows, problems: [] };

  const files = [...new Set(rows.map((row) => path.relative(UI_ROOT, path.join(REPO_ROOT, row.file))))];
  // One collection for every catalog: `list` costs a vitest startup, and the
  // question each row asks is answered by the same collected set.
  const collected = collectCases({ files });
  return {
    rows,
    problems: catalogs.flatMap((catalog) => resolveUiEvidence(catalog.rows, collected, catalog.ids)),
  };
};

/** How the gate reads for a human, in a terminal or in a CI log. */
export const report = ({ rows, problems }) => {
  if (rows.length === 0) return 'scenario catalogs: no row cites a UI case';
  if (problems.length === 0) {
    return `scenario catalogs: ${rows.length} UI citations across `
      + `${new Set(rows.map((row) => row.id)).size} rows each name one collected vitest case`;
  }
  return [
    '\nScenario-catalog UI citations that do not name a collected vitest case:\n',
    ...problems.map((problem) => `  - ${problem}`),
    '',
  ].join('\n');
};

// Only when run as a command, so importing the check does not execute it.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = checkCatalogs();
  if (result.problems.length > 0) {
    console.error(report(result));
    process.exitCode = 1;
  } else {
    console.log(report(result));
  }
}
