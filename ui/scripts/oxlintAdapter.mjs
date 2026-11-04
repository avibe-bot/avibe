// Oxlint CLI adapter for the debt gate. Only public, pinned CLI interfaces are
// used: the package intentionally does not expose a general Node lint API.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { parseSync } from 'oxc-parser';
import {
  CONFIG_FILE, EXPECTED_RULES, OXLINT_VERSION, coverageGaps, intendedFiles,
  inlineConfigViolations, ledgerRuleId, policyDifferences,
} from './lintPolicy.mjs';

const require = createRequire(import.meta.url);
const packageFile = require.resolve('oxlint/package.json');
const cli = path.join(path.dirname(packageFile), 'bin/oxlint');
const flags = ['--disable-nested-config', '--no-ignore'];

function execute(root, args, accepted = [0, 1]) {
  const result = spawnSync(process.execPath, [cli, ...flags, ...args], {
    cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
    // Never accept an inherited color setting in machine-readable output.
    env: { ...process.env, NO_COLOR: '1', FORCE_COLOR: '0' },
  });
  if (result.error) throw result.error;
  if (result.signal || !accepted.includes(result.status)) {
    throw new Error(`Oxlint failed (${result.signal ?? result.status}): ${result.stderr || result.stdout}`);
  }
  return result;
}

function diagnostic(raw, root) {
  const match = /^([^()]+)\(([^()]+)\)$/u.exec(raw.code ?? '');
  const candidate = match ? ledgerRuleId(`${match[1]}/${match[2]}`) : null;
  // Parser/plugin/infrastructure errors are never debt, even when the engine
  // attaches a namespaced diagnostic code to them.
  const id = candidate && Object.hasOwn(EXPECTED_RULES, candidate) ? candidate : null;
  const label = raw.labels?.[0]?.span ?? {};
  return {
    filePath: path.resolve(root, raw.filename || '.'),
    ruleId: id,
    severity: raw.severity === 'error' ? 2 : 1,
    message: raw.message,
    line: label.line ?? 0,
    column: label.column ?? 0,
    offset: label.offset ?? 0,
    length: label.length ?? 0,
  };
}

// CreateProcess on Windows accepts at most 32767 UTF-16 code units. Bound
// commands on every platform so this path is exercised in Linux/macOS CI too.
// Twice each argument's length plus quotes is a conservative escaping bound.
const quotedLength = (argument) => argument.length * 2 + 3;
function* fileBatches(files, args) {
  const fixed = [process.execPath, cli, ...flags, ...args].reduce((sum, argument) => sum + quotedLength(argument), 0);
  const limit = 28000;
  let batch = [];
  let length = fixed;
  for (const file of files) {
    const size = quotedLength(file);
    if (fixed + size > limit) throw new Error(`Lint path exceeds the command-line budget: ${file}`);
    if (length + size > limit) {
      yield batch;
      batch = [];
      length = fixed;
    }
    batch.push(file);
    length += size;
  }
  if (batch.length) yield batch;
}

function run(root, files, configFile) {
  const args = ['--config', configFile, '--format', 'json'];
  const messages = [];
  for (const batch of fileBatches(files, args)) {
    const result = execute(root, [...args, ...batch]);
    let output;
    try { output = JSON.parse(result.stdout); }
    catch { throw new Error(`Oxlint did not return complete JSON: ${result.stderr || result.stdout}`); }
    if (!Array.isArray(output.diagnostics) || output.number_of_files !== batch.length) {
      throw new Error(`Oxlint measured ${output.number_of_files} files; expected ${batch.length}`);
    }
    const found = output.diagnostics.map((item) => diagnostic(item, root));
    if (result.status !== 0 && !found.some((item) => item.severity === 2)) {
      throw new Error(`Oxlint failed without an error diagnostic: ${result.stderr}`);
    }
    messages.push(...found);
  }
  return messages;
}

function suppressionFree(code, comments) {
  let next = code;
  for (const comment of [...comments].reverse()) {
    if (!/^\s*(?:eslint|oxlint)-(?:disable(?:-next-line|-line)?|enable)(?:\s|$)/u.test(comment.value)) continue;
    // Change only the directive keyword, keeping every byte/line offset. This
    // never touches a string, regex, template, or an actual application token.
    const original = next.slice(comment.start, comment.end);
    const replacement = original.replace(/\b(eslint|oxlint)(?=-(?:disable|enable))/u, (word) => `${word.slice(0, -1)}_`);
    next = next.slice(0, comment.start) + replacement + next.slice(comment.end);
  }
  return next;
}

const messageKey = (item) => JSON.stringify([item.ruleId, item.severity, item.offset, item.length]);
function suppressedMessages(normal, unsuppressed) {
  const remaining = new Map();
  for (const item of normal.filter((item) => item.ruleId)) {
    const key = messageKey(item);
    remaining.set(key, (remaining.get(key) ?? 0) + 1);
  }
  const suppressed = [];
  for (const item of unsuppressed.filter((item) => item.ruleId)) {
    const key = messageKey(item);
    const count = remaining.get(key) ?? 0;
    if (count) remaining.set(key, count - 1);
    else suppressed.push(item);
  }
  if ([...remaining.values()].some(Boolean)) {
    throw new Error('Removing disable directives changed an existing diagnostic; suppression measurement is not trustworthy');
  }
  return suppressed;
}

function absolutePlugins(config, root) {
  const resolve = createRequire(path.join(root, CONFIG_FILE));
  return {
    ...config,
    jsPlugins: config.jsPlugins.map((entry) => {
      const specifier = typeof entry === 'string' ? entry : entry.specifier;
      const absolute = specifier.startsWith('.') ? path.resolve(root, specifier) : resolve.resolve(specifier);
      return typeof entry === 'string' ? { name: specifier.replace(/^eslint-plugin-/u, ''), specifier: absolute }
        : { ...entry, specifier: absolute };
    }),
  };
}

/** Measure the real engine, its complete policy, and both visible/hidden debt. */
export function measureLint(root) {
  const configFile = path.join(root, CONFIG_FILE);
  const config = JSON.parse(fs.readFileSync(configFile, 'utf8'));
  const intended = intendedFiles(root);
  const differences = policyDifferences(config);
  const version = JSON.parse(fs.readFileSync(packageFile, 'utf8')).version;
  if (version !== OXLINT_VERSION) differences.push(`oxlint version: expected ${OXLINT_VERSION}, resolved ${version}`);
  // A second suppression ledger is not part of this gate's contract.
  if (fs.existsSync(path.join(root, 'oxlint-suppressions.json'))) differences.push('an untracked Oxlint suppression ledger exists');
  const measurement = {
    intended, fatals: [], coverage: coverageGaps({ intended, lintedFiles: [] }),
    policyGroups: differences.length ? [{ files: intended, differences }] : [],
    inlineViolations: [],
  };
  if (differences.length || !intended.length) return { results: [], measurement };

  // Explicit file input plus --no-ignore prevents .gitignore/.eslintignore from
  // narrowing the domain. Verify the engine's own discovery, then its actual
  // parsed-file count; an unreadable/skipped file cannot masquerade as clean.
  const discoveryArgs = ['--config', configFile, '--debug', 'files'];
  const linted = [...fileBatches(intended, discoveryArgs)].flatMap((batch) =>
    execute(root, [...discoveryArgs, ...batch], [0]).stdout.trim().split(/\r?\n/u).filter(Boolean))
    .map((file) => path.relative(root, path.resolve(root, file)).split(path.sep).join('/'));
  measurement.coverage = coverageGaps({ intended, lintedFiles: linted });
  if (measurement.coverage.missing.length || measurement.coverage.unwalked.length) return { results: [], measurement };

  const shadows = new Map();
  // The old gate also parsed JS/MJS/CJS without applying the TS rules. Keep
  // fatal syntax detection there, without widening the rule-bearing domain.
  const parsedFiles = intendedFiles(root, { extensions: ['.ts', '.tsx', '.js', '.mjs', '.cjs'] });
  for (const file of parsedFiles) {
    const code = fs.readFileSync(path.join(root, file), 'utf8');
    const parsed = parseSync(file, code);
    for (const error of parsed.errors) {
      const offset = error.labels?.[0]?.start;
      const line = typeof offset === 'number' ? code.slice(0, offset).split(/\r\n|[\n\r\u2028\u2029]/u).length : 0;
      measurement.fatals.push({ filePath: file, line, message: error.message });
    }
    if (!intended.includes(file)) continue;
    for (const violation of inlineConfigViolations(parsed.comments, code)) measurement.inlineViolations.push({ file, ...violation });
    const next = suppressionFree(code, parsed.comments);
    if (next !== code) shadows.set(file, next);
  }
  if (measurement.fatals.length || measurement.inlineViolations.length) return { results: [], measurement };

  const messages = run(root, intended, configFile);
  const byFile = new Map(intended.map((file) => [file, { filePath: path.join(root, file), messages: [], suppressedMessages: [] }]));
  for (const item of messages) {
    const relative = path.relative(root, item.filePath).split(path.sep).join('/');
    const entry = byFile.get(relative);
    if (!entry) throw new Error(`Oxlint reported a diagnostic outside the measured domain: ${relative}`);
    entry.messages.push(item);
    if (!item.ruleId && item.severity === 2) measurement.fatals.push({ ...item, filePath: relative });
  }

  if (shadows.size && !measurement.fatals.length) {
    const shadowRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'avibe-lint-suppressions-'));
    try {
      for (const [file, code] of shadows) {
        const dest = path.join(shadowRoot, file);
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        fs.writeFileSync(dest, code);
      }
      const shadowConfig = path.join(shadowRoot, CONFIG_FILE);
      fs.writeFileSync(shadowConfig, JSON.stringify(absolutePlugins(config, root)));
      const raw = run(shadowRoot, [...shadows.keys()], shadowConfig);
      for (const item of raw) {
        if (!item.ruleId && item.severity === 2) throw new Error(`Suppression audit failed: ${item.message}`);
      }
      for (const file of shadows.keys()) {
        const entry = byFile.get(file);
        const unsuppressed = raw.filter((item) => path.relative(shadowRoot, item.filePath).split(path.sep).join('/') === file)
          .map((item) => ({ ...item, filePath: entry.filePath }));
        entry.suppressedMessages = suppressedMessages(entry.messages, unsuppressed);
      }
    } finally {
      fs.rmSync(shadowRoot, { recursive: true, force: true });
    }
  }
  return { results: [...byFile.values()], measurement };
}
