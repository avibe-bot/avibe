#!/usr/bin/env node
/**
 * Gate: every scenario-catalog row evidenced by a UI case resolves to exactly one
 * case vitest collects, in the file the row cites.
 *
 * This runs from `npm test`, which is the UI suite's own CI step, because that is
 * where the collector lives. The Python catalog checker owns everything it can
 * answer exactly — the row's shape, its file, and the ID being greppable inside
 * that file — and defers executability here rather than approximating it.
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

const main = () => {
  const rows = catalogPaths().flatMap((file) => {
    const catalog = yaml.load(fs.readFileSync(file, 'utf8'));
    return uiEvidenceRows(catalog).map((row) => ({ ...row, catalog: path.relative(REPO_ROOT, file) }));
  });
  if (rows.length === 0) {
    console.log('scenario catalogs: no rows are evidenced by a UI case');
    return;
  }

  const files = [...new Set(rows.map((row) => path.relative(UI_ROOT, path.join(REPO_ROOT, row.file))))];
  const problems = resolveUiEvidence(rows, collectCases({ files }));
  if (problems.length > 0) {
    console.error(`\nUI-evidenced scenario rows that do not resolve to a collected vitest case:\n`);
    for (const problem of problems) console.error(`  - ${problem}`);
    console.error('');
    process.exitCode = 1;
    return;
  }
  console.log(`scenario catalogs: ${rows.length} UI-evidenced rows resolve to a collected vitest case`);
};

main();
