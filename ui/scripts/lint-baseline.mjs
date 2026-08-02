// The lint gate. Runs the same ESLint pass as ``eslint .``, then measures the
// result against ``eslint-baseline.json`` — an explicit, per-file, per-rule
// ledger of the violations that predate the cleanup and are not being fixed in
// it (each rule's rationale lives in the ledger itself).
//
// The ledger tracks two things, because a rule can be silenced two ways:
//   ``violations`` — errors ESLint reported.
//   ``suppressions`` — messages an inline ``eslint-disable`` comment hid. Left
//     unfrozen, adding one of those comments would be a free way past the gate,
//     and a suppressed rule also stops the React Compiler analysing that
//     component, so the unmeasured area would grow silently.
//
// What this gate rejects, exactly:
//   - a ``(file, rule)`` pair the ledger never recorded;
//   - a recorded pair violated more often than the ledger allows;
//   - a recorded pair violated less often than the ledger allows, until the
//     ledger is updated (``npm run lint:baseline``) — unrecorded slack would
//     become budget for new violations, so improvements have to be booked;
//   - all three of the above independently for inline suppressions;
//   - a rule in the ledger that nobody wrote a rationale for;
//   - a rule-less fatal problem: a parse failure, an unresolvable config, a
//     plugin that threw. No ledger entry could ever cover one, and a file that
//     does not parse reports no violations at all, which a tally reads as clean.
//
// What it does not reject, said plainly so nobody relies on more: the counter is
// keyed on ``(file, rule)``, not on a site. Deleting one baselined violation and
// introducing another of the same rule in the same file leaves the count
// unchanged and passes. That is the accepted residual of a count ratchet — the
// same trade-off ESLint's own ``suppressions-service`` makes — and it is bounded
// on three sides: the pair must already carry recorded debt, the count cannot
// grow, and once the real debt is paid down the stale check forces the allowance
// down with it.
//
// This file also cannot tell that ESLint was asked to look at less. Configured
// lint scope and rule severity are pinned separately, at their owner, by
// ``scripts/eslintConventions.test.mjs``.
//
// Why a ledger instead of ``eslint-disable`` comments or a relaxed config: both
// of those hide the debt at the site, so nobody can see how much there is or
// whether it is shrinking. One sorted JSON file can be read, diffed, and
// pointed at in review.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ESLint } from 'eslint';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const BASELINE_FILE = path.join(UI_ROOT, 'eslint-baseline.json');

/**
 * Compare a fresh lint tally against the recorded one.
 *
 * Both arguments are ``{ [file]: { [rule]: count } }``. The lookup is per
 * (file, rule) — a file appearing in the baseline buys tolerance for the rules
 * recorded against it, nothing more. It is a count, not an identity: within one
 * already-recorded pair, which sites are violating is not compared. See the
 * residual noted in the file header.
 *
 * @returns `unclassified` (rule/file pair the ledger never recorded),
 *   `expanded` (recorded pair violated more often than allowed), and `stale`
 *   (recorded pair now violated less often, including not at all). Pure.
 */
export function compareToBaseline(baseline, current) {
  const unclassified = [];
  const expanded = [];
  const stale = [];

  for (const [file, rules] of Object.entries(current)) {
    for (const [rule, count] of Object.entries(rules)) {
      const allowed = baseline[file]?.[rule] ?? 0;
      if (allowed === 0) unclassified.push({ file, rule, count });
      else if (count > allowed) expanded.push({ file, rule, count, allowed });
    }
  }

  for (const [file, rules] of Object.entries(baseline)) {
    for (const [rule, allowed] of Object.entries(rules)) {
      const count = current[file]?.[rule] ?? 0;
      if (count < allowed) stale.push({ file, rule, count, allowed });
    }
  }

  return { unclassified, expanded, stale };
}

/**
 * Rule ids carried by a ledger with no entry in ``rationale``.
 *
 * A rule nobody had to explain is how an unrelated error class slips into the
 * ledger and stops being visible. Adding one costs a sentence. Pure.
 */
export function missingRationales(ledgers, rationale) {
  const rules = new Set();
  for (const ledger of ledgers) {
    for (const byRule of Object.values(ledger)) {
      for (const rule of Object.keys(byRule)) {
        if (!rationale?.[rule]) rules.add(rule);
      }
    }
  }
  return [...rules].sort();
}

/**
 * Key a (file, rule) pair.
 *
 * NUL cannot occur in a path or a rule id, so the join is unambiguous where a
 * space or a slash would not be. It is written as an escape rather than typed
 * as a literal byte: a raw NUL makes this whole file binary to git, and a gate
 * everyone is asked to trust has to stay visible in a diff. Pure.
 */
export const pairKey = (file, rule) => `${file}\0${rule}`;

/**
 * Problems ESLint reported with no rule id: a parse failure, an unresolvable
 * config, a plugin that threw.
 *
 * These can never be baselined, because there is no (file, rule) key to record
 * them under. They also cannot be caught by comparing tallies — a file that
 * fails to parse reports no violations at all, which a tally reads as clean.
 * So they are collected separately and fail the gate on their own. Pure.
 */
export function fatalProblems(results) {
  const problems = [];
  for (const result of results) {
    for (const message of result.messages ?? []) {
      if (message.ruleId || message.severity !== 2) continue;
      problems.push({ filePath: result.filePath, line: message.line ?? 0, message: message.message });
    }
  }
  return problems;
}

/** Tally messages into ``{ [file]: { [rule]: count } }`` with sorted keys. */
function tally(results, pick) {
  const byFile = {};
  for (const result of results) {
    const file = path.relative(UI_ROOT, result.filePath).split(path.sep).join('/');
    for (const message of pick(result)) {
      // Rule-less problems have no key to be tallied under; `fatalProblems`
      // owns them, and `reportLines` fails the gate on them unconditionally.
      if (!message.ruleId) continue;
      byFile[file] ??= {};
      byFile[file][message.ruleId] = (byFile[file][message.ruleId] ?? 0) + 1;
    }
  }
  return sortDeep(byFile);
}

function sortDeep(byFile) {
  return Object.fromEntries(
    Object.entries(byFile)
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([file, rules]) => [file, Object.fromEntries(Object.entries(rules).sort(([a], [b]) => (a < b ? -1 : 1)))]),
  );
}

const errorsOf = (result) => result.messages.filter((message) => message.severity === 2);
const suppressedOf = (result) => result.suppressedMessages ?? [];

function describe({ unclassified, expanded, stale }, label) {
  const lines = [];
  for (const { file, rule, count } of unclassified) {
    lines.push(`  NEW       ${file}  ${rule}  ${count} ${label} (not in the baseline)`);
  }
  for (const { file, rule, count, allowed } of expanded) {
    lines.push(`  EXPANDED  ${file}  ${rule}  ${count} ${label}, baseline allows ${allowed}`);
  }
  for (const { file, rule, count, allowed } of stale) {
    lines.push(`  STALE     ${file}  ${rule}  ${count} ${label}, baseline still records ${allowed}`);
  }
  return lines;
}

/**
 * Every reason this run should fail, as printable lines. Empty means pass.
 *
 * The gate's verdict lives here, in one pure function, so "a fatal error fails
 * even when every baselined tally matches" is a property a test can hold onto
 * rather than a branch buried in the IO path. Pure.
 */
export function reportLines({ violationDrift, suppressionDrift, unexplained, fatals }) {
  return [
    ...fatals.map(({ filePath, line, message }) => `  FATAL     ${filePath}:${line}  ${message}`),
    ...describe(violationDrift, 'errors'),
    ...describe(suppressionDrift, 'suppressed messages'),
    ...unexplained.map((rule) => `  UNEXPLAINED  ${rule} is in the baseline with no "rationale" entry`),
  ];
}

const total = (byFile) =>
  Object.values(byFile).reduce((sum, rules) => sum + Object.values(rules).reduce((a, b) => a + b, 0), 0);

async function main() {
  const update = process.argv.includes('--update');
  const eslint = new ESLint();
  const results = await eslint.lintFiles(['.']);

  const violations = tally(results, errorsOf);
  const suppressions = tally(results, suppressedOf);

  if (update) {
    const previous = fs.existsSync(BASELINE_FILE) ? JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf8')) : {};
    const next = { ...previous, violations, suppressions };
    fs.writeFileSync(BASELINE_FILE, `${JSON.stringify(next, null, 2)}\n`);
    console.log(
      `Wrote eslint-baseline.json: ${total(violations)} violations, ${total(suppressions)} suppressions.`,
    );
    return;
  }

  if (!fs.existsSync(BASELINE_FILE)) {
    throw new Error(`Missing ${path.relative(UI_ROOT, BASELINE_FILE)}. Run "npm run lint:baseline" to create it.`);
  }
  const baseline = JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf8'));

  const violationDrift = compareToBaseline(baseline.violations ?? {}, violations);
  const lines = reportLines({
    violationDrift,
    suppressionDrift: compareToBaseline(baseline.suppressions ?? {}, suppressions),
    unexplained: missingRationales([baseline.violations ?? {}, baseline.suppressions ?? {}], baseline.rationale),
    fatals: fatalProblems(results).map((problem) => ({
      ...problem,
      filePath: path.relative(UI_ROOT, problem.filePath).split(path.sep).join('/'),
    })),
  });

  if (lines.length > 0) {
    // Show the offending code for anything that is new or has grown, so the
    // failure reads like a normal lint failure rather than a bookkeeping one.
    const offenders = new Set(
      [...violationDrift.unclassified, ...violationDrift.expanded].map(({ file, rule }) => pairKey(file, rule)),
    );
    const focused = results
      .map((result) => {
        const file = path.relative(UI_ROOT, result.filePath).split(path.sep).join('/');
        const messages = errorsOf(result).filter((message) => offenders.has(pairKey(file, message.ruleId)));
        return { ...result, messages, warningCount: 0, suppressedMessages: [] };
      })
      .filter((result) => result.messages.length > 0);
    if (focused.length > 0) {
      const formatter = await eslint.loadFormatter('stylish');
      console.log(await formatter.format(focused));
    }

    console.error('Lint baseline check failed:');
    console.error(lines.join('\n'));
    console.error(
      '\nFix the reported problems. If a violation is genuinely intentional legacy debt,\n' +
        'raising the baseline needs a reviewer to agree in the PR — it is not a routine step.\n' +
        'If the drift is an improvement (STALE), record it with "npm run lint:baseline".',
    );
    process.exitCode = 1;
    return;
  }

  console.log(
    `Lint baseline check passed: ${total(violations)} baselined violations, ` +
      `${total(suppressions)} baselined suppressions, no fatal problems, ` +
      'and no drift in any (file, rule) pair.',
  );
}

// ``import.meta.main`` is Node 24+; this checkout still supports Node 20.
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  await main();
}
